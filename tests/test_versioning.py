import tempfile
import unittest
from datetime import date
from pathlib import Path

from services.versioning import (
    bump_version,
    latest_changelog_version,
    parse_changelog,
    prepare_release,
    write_release,
)


class VersioningTests(unittest.TestCase):
    def test_semver_bumps(self):
        self.assertEqual(bump_version("1.2.3", "patch"), "1.2.4")
        self.assertEqual(bump_version("1.2.3", "minor"), "1.3.0")
        self.assertEqual(bump_version("1.2.3", "major"), "2.0.0")

    def test_parse_changelog(self):
        releases = parse_changelog(
            "# 版本更新紀錄\n\n## [1.2.0] - 2026-08-10\n\n"
            "### Added\n\n- 新增版本頁\n\n### Fixed\n\n- 修正版本號\n"
        )
        self.assertEqual(releases[0]["version"], "1.2.0")
        self.assertEqual(releases[0]["sections"][0]["label"], "新增")
        self.assertEqual(releases[0]["sections"][1]["items"], ["修正版本號"])

    def test_latest_changelog_version(self):
        self.assertEqual(
            latest_changelog_version(
                "# 版本更新紀錄\n\n## [1.2.0] - 2026-08-10\n\n## [1.1.0] - 2026-07-28\n"
            ),
            "1.2.0",
        )
        self.assertIsNone(latest_changelog_version("# 版本更新紀錄\n"))

    def test_prepare_and_write_release(self):
        result = prepare_release(
            "1.1.0\n",
            "# 版本更新紀錄\n\n## [1.1.0] - 2026-07-28\n",
            "minor",
            {"Added": ["版本頁"], "Fixed": [], "Changed": [], "Removed": [], "Security": []},
            date(2026, 8, 10),
        )
        self.assertEqual(result.version, "1.2.0")
        self.assertLess(result.changelog.index("[1.2.0]"), result.changelog.index("[1.1.0]"))

        with tempfile.TemporaryDirectory() as tmp:
            version_path = Path(tmp) / "VERSION"
            changelog_path = Path(tmp) / "CHANGELOG.md"
            write_release(version_path, changelog_path, result)
            self.assertEqual(version_path.read_text(), "1.2.0\n")
            self.assertIn("- 版本頁", changelog_path.read_text())


if __name__ == "__main__":
    unittest.main()
