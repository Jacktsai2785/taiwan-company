"""Semantic version and changelog helpers used by the app and release script."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RELEASE_RE = re.compile(r"^## \[(?P<version>[^]]+)] - (?P<date>\d{4}-\d{2}-\d{2})$")
SECTION_LABELS = {
    "Added": "新增",
    "Changed": "調整",
    "Fixed": "修正",
    "Removed": "移除",
    "Security": "安全性",
}


def validate_version(version: str) -> str:
    version = version.strip()
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"無效版本號：{version!r}，應為 MAJOR.MINOR.PATCH")
    return version


def bump_version(version: str, part: str) -> str:
    major, minor, patch = map(int, validate_version(version).split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("升版類型只能是 major、minor 或 patch")


def parse_changelog(markdown: str) -> list[dict]:
    """Parse the Keep a Changelog subset used by this project."""
    releases: list[dict] = []
    release: dict | None = None
    section: dict | None = None

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        match = RELEASE_RE.fullmatch(line)
        if match:
            release = {
                "version": match.group("version"),
                "date": match.group("date"),
                "sections": [],
            }
            releases.append(release)
            section = None
            continue
        if line.startswith("### ") and release is not None:
            section_type = line[4:].strip()
            section = {
                "type": section_type.lower(),
                "label": SECTION_LABELS.get(section_type, section_type),
                "items": [],
            }
            release["sections"].append(section)
            continue
        if line.startswith("- ") and section is not None:
            section["items"].append(line[2:].strip())
            continue
        if line and section is not None and section["items"] and not line.startswith(("#", ">")):
            section["items"][-1] += " " + line

    return releases


def latest_changelog_version(changelog_text: str) -> str | None:
    """Version of the topmost CHANGELOG release section, or None if empty."""
    releases = parse_changelog(changelog_text)
    return releases[0]["version"] if releases else None


@dataclass(frozen=True)
class ReleaseResult:
    previous_version: str
    version: str
    changelog: str


def prepare_release(
    version_text: str,
    changelog_text: str,
    part: str,
    entries: dict[str, list[str]],
    release_date: date | None = None,
) -> ReleaseResult:
    """Return the next version and changelog without touching the filesystem."""
    previous = validate_version(version_text)
    next_version = bump_version(previous, part)
    cleaned = {
        heading: [item.strip() for item in items if item.strip()]
        for heading, items in entries.items()
        if heading in SECTION_LABELS
    }
    cleaned = {heading: items for heading, items in cleaned.items() if items}
    if not cleaned:
        raise ValueError("至少需要一筆版本更新內容")

    lines = [f"## [{next_version}] - {(release_date or date.today()).isoformat()}", ""]
    for heading in SECTION_LABELS:
        items = cleaned.get(heading, [])
        if not items:
            continue
        lines.extend([f"### {heading}", ""])
        lines.extend(f"- {item}" for item in items)
        lines.append("")

    marker = re.search(r"^## \[", changelog_text, flags=re.MULTILINE)
    if marker:
        updated = changelog_text[: marker.start()].rstrip() + "\n\n" + "\n".join(lines) + changelog_text[marker.start():]
    else:
        updated = changelog_text.rstrip() + "\n\n" + "\n".join(lines)
    return ReleaseResult(previous, next_version, updated.rstrip() + "\n")


def write_release(version_path: Path, changelog_path: Path, result: ReleaseResult) -> None:
    """Replace both files after validation and restore them if either replace fails."""
    version_tmp = version_path.with_suffix(".tmp")
    changelog_tmp = changelog_path.with_suffix(".tmp")
    previous_version = version_path.read_text(encoding="utf-8") if version_path.exists() else None
    previous_changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else None
    version_tmp.write_text(result.version + "\n", encoding="utf-8")
    changelog_tmp.write_text(result.changelog, encoding="utf-8")
    try:
        changelog_tmp.replace(changelog_path)
        version_tmp.replace(version_path)
    except Exception:
        if previous_changelog is not None:
            changelog_path.write_text(previous_changelog, encoding="utf-8")
        if previous_version is not None:
            version_path.write_text(previous_version, encoding="utf-8")
        raise
    finally:
        version_tmp.unlink(missing_ok=True)
        changelog_tmp.unlink(missing_ok=True)
