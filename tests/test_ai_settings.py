import unittest
from unittest.mock import patch

from services import ai_deps, ai_settings, claude_client


class AiSettingsTests(unittest.TestCase):
    @patch("services.ai_settings.data_store.get_config", return_value={"ai_engine": "codex"})
    @patch.dict("os.environ", {"AI_ENGINE": "claude"})
    def test_persisted_platform_setting_beats_environment(self, _get_config):
        self.assertEqual(ai_settings.get_engine(), "codex")

    @patch("services.ai_deps.ai_settings.get_engine", return_value="gemini")
    def test_request_cannot_override_platform_setting(self, _get_engine):
        self.assertEqual(ai_deps._resolve("claude"), {"engine": "gemini"})

    @patch("services.ai_settings.data_store.save_ai_engine")
    def test_setting_is_validated_and_persisted(self, save):
        self.assertEqual(ai_settings.set_engine(" CODEX "), "codex")
        save.assert_called_once_with("codex")

    @patch("services.claude_client.model_for_engine", wraps=claude_client.model_for_engine)
    @patch("services.ai_settings.get_engine", return_value="codex")
    def test_client_default_uses_platform_setting(self, _get_engine, _model):
        self.assertEqual(claude_client._normalize_engine(""), "codex")


if __name__ == "__main__":
    unittest.main()
