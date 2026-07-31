import unittest

from services.report_generator import _claude_only_model


class ClaudeOnlyModelTests(unittest.TestCase):
    def test_keeps_claude_model_aliases_for_claude(self):
        self.assertEqual(_claude_only_model("claude", "opus"), "opus")
        self.assertEqual(
            _claude_only_model("anthropic", "claude-sonnet-4-6"),
            "claude-sonnet-4-6",
        )

    def test_other_engines_use_their_own_default_model(self):
        for engine in ("codex", "gemini", "ollama"):
            with self.subTest(engine=engine):
                self.assertEqual(_claude_only_model(engine, "claude-sonnet-4-6"), "")


if __name__ == "__main__":
    unittest.main()
