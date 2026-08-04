"""
Local-only AI engine layer. No cloud API keys.

Engines (selected per-request via `engine=`):
  - claude  : local `claude` CLI (default)         — multimodal via Read tool
  - codex   : local `codex exec` CLI (OpenAI GPT)  — images via --image
  - gemini  : local `gemini -p` CLI (Google)       — files/images via @path
  - ollama  : local OpenAI-compatible endpoint      — images via vision model

Engines without native support for a given file type fall back to local text
extraction (file_parser + tesseract OCR) so behaviour stays predictable.
"""
import base64
import logging
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 全平台 AI 併發閘：限制「同時在跑的 CLI 子行程」總數（所有引擎、所有功能共用）。
# 沒有這道閘時，產業地圖生成、每日 digest、enrich、公司抽取可各自同時起一個 CLI，
# 並行搶同一份 5 小時滾動額度；report_generator 的 _CLAUDE_LOCK 只罩 DD memo，
# 罩不到其他路徑。放在 _run_cli 這層 = 一處守住所有呼叫點。
# 預設 2：允許一個長任務（480s 地圖/深度生成）與一個短任務並行，不互相餓死。
_MAX_CONCURRENT_CLI = max(1, int(os.getenv("AI_MAX_CONCURRENCY", "2") or 2))
_CLI_GATE = threading.BoundedSemaphore(_MAX_CONCURRENT_CLI)

DEFAULT_ENGINE = (os.getenv("AI_ENGINE", "claude") or "claude").strip().lower()

MODEL_CLAUDE = os.getenv("CLAUDE_MODEL", "")
MODEL_CODEX  = os.getenv("CODEX_MODEL", "")
MODEL_GEMINI = os.getenv("GEMINI_MODEL", "")

OLLAMA_BASE_URL     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "")

KNOWN_ENGINES = ("claude", "codex", "gemini", "ollama")

# stderr/stdout fragments that mean "the CLI ran but you're not authenticated".
_AUTH_HINTS = (
    "auth method", "not logged in", "please login", "please log in", "run `login`",
    "unauthorized", "authenticate", "credentials", "api_key", "api key",
    "use_vertexai", "use_gca", "login first", "登入", "未登入", "授權",
)

# 訊息片段代表「CLI 有跑，但撞到額度/流量上限」，跟真正的生成失敗要分開處理
# （額度上限應該等待重試，不是判定失敗）。同一組關鍵字曾各自散落在
# scripts/regen_summaries.sh 的 is_limit() 與呼叫端的例外訊息比對裡；
# 這裡收斂成單一分類函式，SSE 事件直接帶 code，呼叫端不用再重新猜測。
_USAGE_LIMIT_HINTS = (
    "limit reached", "usage limit", "rate limit", "will reset", "resets",
    "額度", "流量上限", "用量上限", "請稍後再試", "too many requests",
    "overloaded", "429", "529",
)


def classify_ai_error(exc: BaseException | str) -> str:
    """把 AI 呼叫例外分類成機器可讀的 code，取代到處手動 grep 錯誤文字。
    目前只分出 "ai_usage_limit"；其餘回傳空字串（一般失敗，呼叫端照舊處理）。"""
    text = str(exc).lower()
    if any(h.lower() in text for h in _USAGE_LIMIT_HINTS):
        return "ai_usage_limit"
    return ""

_IMAGE_MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp"}

# 平台定案純地端 CLI 登入，不用雲端 API Key（見 CLAUDE.md）。若 .env 或環境殘留
# 這些變數，CLI 會優先用它們做 API 認證、蓋過本機登入 session，一壞就是全部
# 引擎跟著壞且訊息長得像「未登入」，很難查。子行程一律拿掉，斷絕殘留變數的影響。
_CLOUD_API_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")
_CLI_ENV = {k: v for k, v in os.environ.items() if k not in _CLOUD_API_KEY_VARS}


def _normalize_engine(engine: str) -> str:
    if engine:
        eng = engine.strip().lower()
    else:
        try:
            from services import ai_settings
            eng = ai_settings.get_engine()
        except Exception:
            eng = DEFAULT_ENGINE
    # Backwards-compat for any stale provider strings.
    eng = {"anthropic": "claude", "local": "claude", "openai": "codex"}.get(eng, eng)
    if eng not in KNOWN_ENGINES:
        raise RuntimeError(
            f"未知的 AI 引擎「{engine}」。可用引擎：{', '.join(KNOWN_ENGINES)}。請點 ⚙ 重新選擇。"
        )
    return eng


def model_for_engine(engine: str) -> str:
    """Return the configured model label used for run audit metadata."""
    eng = _normalize_engine(engine)
    return {
        "claude": MODEL_CLAUDE or "default",
        "codex": MODEL_CODEX or "default",
        "gemini": MODEL_GEMINI or "default",
        "ollama": OLLAMA_MODEL or "default",
    }[eng]


# ── Shared subprocess runner (process-group kill on timeout) ────────────────────

def _run_cli(cmd: list[str], timeout: int, label: str) -> str:
    """Run a CLI in its own process group; killpg on timeout so child processes
    holding the pipe can't hang us forever. Serialized through _CLI_GATE so at
    most _MAX_CONCURRENT_CLI engine processes run at once platform-wide."""
    with _CLI_GATE:
        return _run_cli_unlocked(cmd, timeout, label)


def _run_cli_unlocked(cmd: list[str], timeout: int, label: str) -> str:
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True, env=_CLI_ENV,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"找不到 {label} 指令。請先安裝並登入 {label}，或點 ⚙ 改選其他引擎。"
        )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise RuntimeError(f"{label} 執行超時（>{timeout}s），已強制終止。")
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = (stderr or stdout)
        if any(h in detail.lower() for h in _AUTH_HINTS):
            raise RuntimeError(
                f"{label} 尚未登入授權。請先在本機完成 {label} 登入（如 gemini / codex / claude 各自的登入流程），"
                f"或點 ⚙ 改選其他引擎。"
            )
        raise RuntimeError(f"{label} 錯誤 (exit {proc.returncode}):\n{detail[:400]}")
    if stdout.lower().startswith("execution error") or stdout.lower() == "error":
        raise RuntimeError(f"{label} 執行錯誤：{stdout[:200]}")
    return stdout


# ── claude CLI ──────────────────────────────────────────────────────────────────

_CLI_PATH: str | None = None


def _find_cli() -> str:
    import shutil
    env_path = os.getenv("CLAUDE_CLI_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    bun_base = Path.home() / ".bun" / "install" / "cache" / "@anthropic-ai"
    for pkg_name in ["claude-agent-sdk-win32-x64", "claude-code-win32-x64"]:
        pkg_dir = bun_base / pkg_name
        if pkg_dir.exists():
            for v in sorted(pkg_dir.iterdir(), reverse=True):
                candidate = v / "claude.exe"
                if candidate.exists():
                    return str(candidate)
    gstack_nm = Path.home() / ".claude" / "skills" / "gstack" / "node_modules"
    for name in ("claude.exe", "claude"):
        for candidate in gstack_nm.rglob(name):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    raise RuntimeError("找不到 claude CLI。請先安裝並登入 claude，或點 ⚙ 改選其他引擎。")


def _cli() -> str:
    global _CLI_PATH
    if _CLI_PATH is None:
        _CLI_PATH = _find_cli()
    return _CLI_PATH


def _ask_claude(prompt: str, timeout: int, allowed_tools: list[str] | None,
                max_turns: int, model: str) -> str:
    cmd = [_cli(), "-p", prompt, "--output-format", "text", "--max-turns", str(max_turns)]
    _model = model or MODEL_CLAUDE
    if _model:
        cmd += ["--model", _model]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    return _run_cli(cmd, timeout, "claude CLI")


def _ask_claude_files(prompt: str, file_paths: list[str], timeout: int, model: str) -> str:
    if not file_paths:
        return _ask_claude(prompt, timeout, None, 12, model)
    dirs = sorted({str(Path(p).parent) for p in file_paths})
    file_list = "\n".join(f"- {p}" for p in file_paths)
    full_prompt = (
        "請先用 Read tool 逐一讀取以下檔案（PDF 與圖片皆可直接讀取），讀完全部後再依指示回答。\n\n"
        f"檔案清單：\n{file_list}\n\n{prompt}"
    )
    cmd = [_cli(), "-p", full_prompt, "--output-format", "text",
           "--allowedTools", "Read", "--max-turns", "30"]
    _model = model or MODEL_CLAUDE
    if _model:
        cmd += ["--model", _model]
    for d in dirs:
        cmd += ["--add-dir", d]
    return _run_cli(cmd, timeout, "claude CLI")


# ── codex CLI (OpenAI GPT) ──────────────────────────────────────────────────────

def _codex_bin() -> str:
    import shutil
    env_path = os.getenv("CODEX_CLI_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    return shutil.which("codex") or "codex"


def _ask_codex(prompt: str, timeout: int, model: str, images: list[str] | None = None) -> str:
    cmd = [_codex_bin(), "exec"]
    _model = model or MODEL_CODEX
    if _model:
        cmd += ["-m", _model]
    for img in images or []:
        cmd += ["--image", img]
    cmd += [prompt]
    return _run_cli(cmd, timeout, "codex CLI")


# ── gemini CLI (Google) ─────────────────────────────────────────────────────────

def _gemini_bin() -> str:
    import shutil
    env_path = os.getenv("GEMINI_CLI_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    return shutil.which("gemini") or "gemini"


def _ask_gemini(prompt: str, timeout: int, model: str, file_paths: list[str] | None = None) -> str:
    # gemini CLI reads files referenced with @<path> inside the prompt.
    refs = " ".join(f"@{p}" for p in (file_paths or []))
    full_prompt = f"{refs} {prompt}".strip() if refs else prompt
    cmd = [_gemini_bin(), "-p", full_prompt]
    _model = model or MODEL_GEMINI
    if _model:
        cmd += ["-m", _model]
    return _run_cli(cmd, timeout, "gemini CLI")


# ── ollama (local OpenAI-compatible endpoint) ───────────────────────────────────

def _ollama_chat(messages: list[dict[str, Any]], timeout: int, model: str) -> str:
    import httpx
    url = f"{OLLAMA_BASE_URL}/v1/chat/completions"
    body = {"model": model or OLLAMA_MODEL, "messages": messages}
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.post(url, json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except httpx.ConnectError:
        raise RuntimeError(
            f"無法連線到 Ollama（{OLLAMA_BASE_URL}）。請先啟動 Ollama，或點 ⚙ 改選其他引擎。"
        )
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise RuntimeError(f"Ollama 呼叫失敗：{e}")


def _ask_ollama(prompt: str, timeout: int, model: str) -> str:
    return _ollama_chat([{"role": "user", "content": prompt}], timeout, model)


def _ask_ollama_image(prompt: str, image_content: bytes, suffix: str, timeout: int) -> str:
    if not OLLAMA_VISION_MODEL:
        # No vision model configured → fall back to local OCR text.
        return _ask_with_local_extraction_bytes(prompt, image_content, suffix, "ollama", timeout)
    img_b64 = base64.b64encode(image_content).decode()
    mime = _IMAGE_MEDIA.get(suffix.lstrip(".").lower(), "image/png")
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
        {"type": "text", "text": prompt},
    ]}]
    return _ollama_chat(messages, timeout, OLLAMA_VISION_MODEL)


# ── Local text-extraction fallback (file_parser + OCR) ──────────────────────────

def _ask_with_local_extraction(prompt: str, file_paths: list[str], engine: str, timeout: int) -> str:
    """Extract text/OCR from each file locally, inline it, then run a plain text
    completion on the selected engine. Used when an engine lacks native support."""
    from . import file_parser
    chunks: list[str] = []
    for p in file_paths:
        try:
            text = file_parser.extract_text(Path(p).name, Path(p).read_bytes())
        except OSError:
            continue
        if text and not text.startswith("["):
            chunks.append(f"── 檔案：{Path(p).name} ──\n{text}")
    combined = "\n\n".join(chunks)
    full_prompt = f"{prompt}\n\n以下是上傳檔案的文字內容：\n\n{combined}" if combined else prompt
    return _ask_text(full_prompt, timeout, None, 12, "", engine)


def _ask_with_local_extraction_bytes(prompt: str, content: bytes, suffix: str,
                                     engine: str, timeout: int) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(content); tmp.flush(); tmp.close()
        return _ask_with_local_extraction(prompt, [tmp.name], engine, timeout)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── Public API ──────────────────────────────────────────────────────────────────

def _ask_text(prompt: str, timeout: int, allowed_tools: list[str] | None,
              max_turns: int, model: str, engine: str) -> str:
    if engine == "claude":
        return _ask_claude(prompt, timeout, allowed_tools, max_turns, model)
    if engine == "codex":
        return _ask_codex(prompt, timeout, model)
    if engine == "gemini":
        return _ask_gemini(prompt, timeout, model)
    if engine == "ollama":
        return _ask_ollama(prompt, timeout, model)
    raise RuntimeError(f"未支援的引擎：{engine}")


def ask(
    prompt: str,
    timeout: int = 120,
    allowed_tools: list[str] | None = None,
    engine: str = "",
    max_turns: int = 20,
    model: str = "",
) -> str:
    return _ask_text(prompt, timeout, allowed_tools, max_turns, model, _normalize_engine(engine))


def ask_with_image(
    prompt: str,
    image_content: bytes,
    suffix: str,
    timeout: int = 120,
    engine: str = "",
) -> str:
    eng = _normalize_engine(engine)
    if eng == "claude":
        return _ask_claude_image(prompt, image_content, suffix, timeout)
    if eng == "codex":
        return _ask_codex_image(prompt, image_content, suffix, timeout)
    if eng == "gemini":
        return _ask_gemini_image(prompt, image_content, suffix, timeout)
    if eng == "ollama":
        return _ask_ollama_image(prompt, image_content, suffix, timeout)
    raise RuntimeError(f"未支援的引擎：{eng}")


def ask_with_files(
    prompt: str,
    file_paths: list[str],
    timeout: int = 300,
    engine: str = "",
    model: str = "",
) -> str:
    eng = _normalize_engine(engine)
    if eng == "claude":
        return _ask_claude_files(prompt, file_paths, timeout, model)
    if eng == "gemini":
        return _ask_gemini(prompt, timeout, model, file_paths=file_paths)
    if eng == "codex":
        # codex reads images natively; non-image files go through local extraction.
        images = [p for p in file_paths if Path(p).suffix.lstrip(".").lower() in _IMAGE_MEDIA]
        others = [p for p in file_paths if p not in images]
        if others:
            return _ask_with_local_extraction(prompt, file_paths, eng, timeout)
        return _ask_codex(prompt, timeout, model, images=images)
    if eng == "ollama":
        return _ask_with_local_extraction(prompt, file_paths, eng, timeout)
    raise RuntimeError(f"未支援的引擎：{eng}")


# ── Image helpers per engine ────────────────────────────────────────────────────

def _ask_claude_image(prompt: str, image_content: bytes, suffix: str, timeout: int) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(image_content); tmp.flush(); tmp.close()
        full_prompt = f"請先用 Read tool 讀取以下圖片，然後回答問題。\n\n圖片路徑：{tmp.name}\n\n{prompt}"
        cmd = [_cli(), "-p", full_prompt, "--output-format", "text",
               "--allowedTools", "Read", "--add-dir", str(Path(tmp.name).parent)]
        return _run_cli(cmd, timeout, "claude CLI")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _ask_codex_image(prompt: str, image_content: bytes, suffix: str, timeout: int) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(image_content); tmp.flush(); tmp.close()
        return _ask_codex(prompt, timeout, "", images=[tmp.name])
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _ask_gemini_image(prompt: str, image_content: bytes, suffix: str, timeout: int) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(image_content); tmp.flush(); tmp.close()
        return _ask_gemini(prompt, timeout, "", file_paths=[tmp.name])
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
