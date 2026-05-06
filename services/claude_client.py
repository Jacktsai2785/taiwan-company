"""
Wrapper for Claude / OpenAI / Gemini calls.
Priority: per-request api_key → ANTHROPIC_API_KEY env → local Claude CLI
Supported providers: anthropic (default), openai, gemini
"""
import base64
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODEL_ANTHROPIC = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MODEL_OPENAI    = os.getenv("OPENAI_MODEL",  "gpt-4o")
MODEL_GEMINI    = os.getenv("GEMINI_MODEL",  "gemini-2.0-flash")


def _friendly_http_error(provider_name: str, exc) -> RuntimeError:
    """Convert provider HTTP errors into messages users can act on."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text[:200]
        if status in (401, 403):
            return RuntimeError(f"{provider_name} API Key 無效或無權限。請點 ⚙ 重新輸入正確的 Key。")
        if status == 429:
            return RuntimeError(f"{provider_name} 已達流量上限。請稍後再試，或前往該服務確認額度。")
        if status == 400:
            return RuntimeError(f"{provider_name} 拒絕請求（{detail}）。請確認 Key 與模型可用性。")
        return RuntimeError(f"{provider_name} 回傳錯誤 {status}：{detail}")
    if isinstance(exc, httpx.TimeoutException):
        return RuntimeError(f"{provider_name} 回應逾時，請稍後再試。")
    return RuntimeError(f"{provider_name} 呼叫失敗：{exc}")

# ── Anthropic API ──────────────────────────────────────────────────────────────

def _ask_anthropic(
    prompt: str,
    timeout: int = 120,
    allowed_tools: list[str] | None = None,
    api_key: str = "",
) -> str:
    import anthropic
    try:
        return _ask_anthropic_inner(prompt, timeout, allowed_tools, api_key)
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude API Key 無效。請點 ⚙ 重新輸入正確的 Key。")
    except anthropic.PermissionDeniedError:
        raise RuntimeError("Claude API Key 無權限存取此模型。")
    except anthropic.RateLimitError:
        raise RuntimeError("Claude API 已達流量上限。請稍後再試。")
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API 錯誤：{e}")


def _ask_anthropic_inner(
    prompt: str,
    timeout: int,
    allowed_tools: list[str] | None,
    api_key: str,
) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    use_web = bool(allowed_tools and any(t in {"WebSearch", "WebFetch"} for t in allowed_tools))
    tools: list[dict[str, Any]] = [{"type": "web_search_20250305"}] if use_web else []
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for _round in range(10):
        kwargs: dict[str, Any] = {"model": MODEL_ANTHROPIC, "max_tokens": 8096, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        response = client.messages.create(**kwargs)
        log.debug("Anthropic round %d stop=%s", _round, response.stop_reason)

        if response.stop_reason == "end_turn":
            return _text_from_anthropic(response)

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in response.content if getattr(b, "type", None) == "tool_use"
            ]
            if results:
                messages.append({"role": "user", "content": results})
            continue

        return _text_from_anthropic(response)

    raise RuntimeError("Claude API exceeded max tool rounds")


def _text_from_anthropic(response) -> str:
    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()


def _ask_anthropic_image(
    prompt: str, image_content: bytes, suffix: str, api_key: str = ""
) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    suffix_clean = suffix.lstrip(".").lower()
    _MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "gif": "image/gif", "webp": "image/webp"}
    media_type = _MEDIA.get(suffix_clean)
    if media_type is None:
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.open(io.BytesIO(image_content)).save(buf, format="PNG")
        image_content, media_type = buf.getvalue(), "image/png"

    img_data = base64.standard_b64encode(image_content).decode()
    response = client.messages.create(
        model=MODEL_ANTHROPIC, max_tokens=2048,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return _text_from_anthropic(response)


# ── OpenAI API ─────────────────────────────────────────────────────────────────

def _ask_openai(
    prompt: str, allowed_tools: list[str] | None = None, api_key: str = ""
) -> str:
    import httpx
    use_web = bool(allowed_tools and any(t in {"WebSearch", "WebFetch"} for t in allowed_tools))
    model = "gpt-4o-search-preview" if use_web else MODEL_OPENAI
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8096,
    }
    try:
        with httpx.Client(timeout=300) as c:
            resp = c.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise _friendly_http_error("OpenAI", e)


def _ask_openai_image(
    prompt: str, image_content: bytes, suffix: str, api_key: str = ""
) -> str:
    import httpx
    suffix_clean = suffix.lstrip(".").lower()
    img_type = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(suffix_clean, "jpeg")
    img_b64 = base64.b64encode(image_content).decode()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "model": MODEL_OPENAI,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/{img_type};base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 2048,
    }
    try:
        with httpx.Client(timeout=120) as c:
            resp = c.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise _friendly_http_error("OpenAI", e)


# ── Gemini API ─────────────────────────────────────────────────────────────────

def _ask_gemini(
    prompt: str, allowed_tools: list[str] | None = None, api_key: str = ""
) -> str:
    import httpx
    use_web = bool(allowed_tools and any(t in {"WebSearch", "WebFetch"} for t in allowed_tools))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_GEMINI}:generateContent?key={api_key}"
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8096},
    }
    if use_web:
        body["tools"] = [{"google_search": {}}]
    try:
        with httpx.Client(timeout=300) as c:
            resp = c.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return _extract_gemini_text(data)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise _friendly_http_error("Gemini", e)


def _extract_gemini_text(data: dict) -> str:
    """Walk Gemini response and return concatenated text. Surfaces a clear
    error when the model produced no text (blocked, finish_reason=SAFETY, etc.)."""
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback", {})
        block_reason = feedback.get("blockReason", "")
        if block_reason:
            raise RuntimeError(f"Gemini 拒絕生成內容（{block_reason}）。請更換提問或檢查 Key 權限。")
        raise RuntimeError("Gemini 沒有回傳任何內容。")
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        finish = cand.get("finishReason", "")
        raise RuntimeError(f"Gemini 回傳空內容（finish_reason={finish or 'unknown'}）。")
    return text


def _ask_gemini_image(
    prompt: str, image_content: bytes, suffix: str, api_key: str = ""
) -> str:
    import httpx
    suffix_clean = suffix.lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(suffix_clean, "image/jpeg")
    img_b64 = base64.b64encode(image_content).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_GEMINI}:generateContent?key={api_key}"
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime, "data": img_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"maxOutputTokens": 2048},
    }
    try:
        with httpx.Client(timeout=120) as c:
            resp = c.post(url, json=body)
            resp.raise_for_status()
            return _extract_gemini_text(resp.json())
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise _friendly_http_error("Gemini", e)


# ── Local CLI fallback ─────────────────────────────────────────────────────────

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
    for candidate in gstack_nm.rglob("claude.exe"):
        return str(candidate)
    raise FileNotFoundError(
        "找不到 Claude CLI。請設定 API Key 或安裝 Claude Desktop。"
    )


def _cli() -> str:
    global _CLI_PATH
    if _CLI_PATH is None:
        _CLI_PATH = _find_cli()
    return _CLI_PATH


def _ask_cli(prompt: str, timeout: int = 120, allowed_tools: list[str] | None = None) -> str:
    cmd = [_cli(), "-p", prompt, "--output-format", "text"]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude CLI 錯誤 (exit {result.returncode}):\n{stderr[:400]}")
    if stdout.lower().startswith("execution error") or stdout.lower() == "error":
        raise RuntimeError(f"Claude CLI 執行錯誤：{stdout[:200]}")
    return stdout


def _ask_cli_image(prompt: str, image_content: bytes, suffix: str, timeout: int = 120) -> str:
    import tempfile
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(image_content); tmp.flush(); tmp.close()
        full_prompt = f"請先用 Read tool 讀取以下圖片，然後回答問題。\n\n圖片路徑：{tmp.name}\n\n{prompt}"
        cmd = [_cli(), "-p", full_prompt, "--output-format", "text",
               "--allowedTools", "Read", "--add-dir", str(Path(tmp.name).parent)]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Claude CLI 圖片辨識錯誤 (exit {result.returncode}):\n{stderr[:400]}")
        if stdout.lower().startswith("execution error") or stdout.lower() == "error":
            raise RuntimeError(f"Claude CLI 執行錯誤：{stdout[:200]}")
        return stdout
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# ── Public API ─────────────────────────────────────────────────────────────────

_NO_AI_HINT = (
    "尚未設定 AI。請點右上角 ⚙ 設定 → 選擇 AI 提供者並輸入 API Key"
    "（建議 Gemini，可至 aistudio.google.com 免費申請）。"
)


def _try_local_cli(call):
    """Wrap _ask_cli/_ask_cli_image to convert missing-binary errors into a
    user-friendly message (so cloud users see clear guidance instead of raw
    FileNotFoundError)."""
    try:
        return call()
    except FileNotFoundError:
        raise RuntimeError(_NO_AI_HINT)


def ask(
    prompt: str,
    timeout: int = 120,
    allowed_tools: list[str] | None = None,
    api_key: str = "",
    provider: str = "anthropic",
) -> str:
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        provider = "anthropic"
    if not api_key:
        return _try_local_cli(lambda: _ask_cli(prompt, timeout, allowed_tools))
    if provider == "openai":
        return _ask_openai(prompt, allowed_tools, api_key)
    if provider == "gemini":
        return _ask_gemini(prompt, allowed_tools, api_key)
    return _ask_anthropic(prompt, timeout, allowed_tools, api_key)


def ask_with_image(
    prompt: str,
    image_content: bytes,
    suffix: str,
    timeout: int = 120,
    api_key: str = "",
    provider: str = "anthropic",
) -> str:
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        provider = "anthropic"
    if not api_key:
        return _try_local_cli(lambda: _ask_cli_image(prompt, image_content, suffix, timeout))
    if provider == "openai":
        return _ask_openai_image(prompt, image_content, suffix, api_key)
    if provider == "gemini":
        return _ask_gemini_image(prompt, image_content, suffix, api_key)
    return _ask_anthropic_image(prompt, image_content, suffix, api_key)
