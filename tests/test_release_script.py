import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.release as release  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class ReleaseScriptDuplicateGuardTests(unittest.TestCase):
    """scripts/release.py must refuse to bump again while the changelog's
    topmost entry already covers the current (uncommitted) VERSION, per the
    rule in CLAUDE.md/AGENTS.md's release workflow section."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "VERSION").write_text("1.1.0\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text(
            "# 版本更新紀錄\n\n## [1.1.0] - 2026-07-28\n\n### Added\n\n- 初版\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "VERSION", "CHANGELOG.md")
        _git(self.repo, "commit", "-q", "-m", "init")
        self._root_patch = patch.object(release, "ROOT", self.repo)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        self._tmp.cleanup()

    def _run(self, argv):
        with patch.object(sys, "argv", ["release.py", *argv]):
            return release.main()

    def test_first_release_after_a_commit_succeeds(self):
        self.assertEqual(self._run(["patch", "--fixed", "修正 A"]), 0)
        self.assertEqual((self.repo / "VERSION").read_text().strip(), "1.1.1")

    def test_rerunning_before_commit_is_rejected(self):
        self._run(["patch", "--fixed", "修正 A"])
        self.assertEqual((self.repo / "VERSION").read_text().strip(), "1.1.1")

        with self.assertRaises(SystemExit):
            self._run(["patch", "--fixed", "修正 B（重複執行）"])

        # Rejected before touching either file a second time.
        self.assertEqual((self.repo / "VERSION").read_text().strip(), "1.1.1")
        self.assertEqual(
            (self.repo / "CHANGELOG.md").read_text().count("## ["), 2
        )

    def test_dry_run_bypasses_the_guard(self):
        self._run(["patch", "--fixed", "修正 A"])
        # A second dry-run must not raise even though the prior release is
        # still uncommitted — it never writes anything.
        self.assertEqual(self._run(["patch", "--fixed", "預覽", "--dry-run"]), 0)
        self.assertEqual((self.repo / "VERSION").read_text().strip(), "1.1.1")


if __name__ == "__main__":
    unittest.main()
