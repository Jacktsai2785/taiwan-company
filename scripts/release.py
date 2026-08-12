#!/usr/bin/env python3
"""Bump VERSION and prepend a structured CHANGELOG entry.

Example:
  .venv/bin/python scripts/release.py patch --fixed "修正公司視窗顯示"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.versioning import (  # noqa: E402
    latest_changelog_version,
    prepare_release,
    write_release,
)


def _git_paths_dirty(paths: list[Path]) -> bool:
    """True if any of the given paths has uncommitted changes (tracked or not).

    Returns False (fail-open) if this isn't a Git checkout or `git` is missing —
    the duplicate-release guard is a convenience check, not a hard requirement.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *(str(p) for p in paths)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(result.stdout.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="建立一筆產品版本")
    parser.add_argument("part", choices=("major", "minor", "patch"), help="升版幅度")
    parser.add_argument("--added", action="append", default=[], help="新增項目，可重複")
    parser.add_argument("--changed", action="append", default=[], help="調整項目，可重複")
    parser.add_argument("--fixed", action="append", default=[], help="修正項目，可重複")
    parser.add_argument("--removed", action="append", default=[], help="移除項目，可重複")
    parser.add_argument("--security", action="append", default=[], help="安全性項目，可重複")
    parser.add_argument("--dry-run", action="store_true", help="只預覽，不寫入檔案")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    version_path = ROOT / "VERSION"
    changelog_path = ROOT / "CHANGELOG.md"
    entries = {
        "Added": args.added,
        "Changed": args.changed,
        "Fixed": args.fixed,
        "Removed": args.removed,
        "Security": args.security,
    }
    version_text = version_path.read_text(encoding="utf-8")
    changelog_text = changelog_path.read_text(encoding="utf-8")

    current_version = version_text.strip()
    if (
        not args.dry_run
        and latest_changelog_version(changelog_text) == current_version
        and _git_paths_dirty([version_path, changelog_path])
    ):
        build_parser().error(
            f"CHANGELOG.md 已有版本 {current_version} 的未提交紀錄；"
            "請直接編輯 CHANGELOG.md 補齊該版本內容，不要再次升版。"
        )

    try:
        result = prepare_release(
            version_text,
            changelog_text,
            args.part,
            entries,
        )
    except ValueError as exc:
        build_parser().error(str(exc))

    if args.dry_run:
        print(f"{result.previous_version} -> {result.version}")
        return 0
    write_release(version_path, changelog_path, result)
    print(f"已建立版本 {result.version}（原 {result.previous_version}）")
    print("請檢查 VERSION 與 CHANGELOG.md 後再 commit。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
