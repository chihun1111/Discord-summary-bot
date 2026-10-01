from unittest.mock import patch
import os
import unittest
from chatbot.config import Config, DEFAULT_MODEL

class ConfigTests(unittest.TestCase):
    def load(self, extra):
        values = {"DISCORD_TOKEN":"dummy-token", "DISCORD_GUILD_ID":"1", "INDEX_CHANNEL_IDS":"2"}
        values.update(extra)
        with patch.dict(os.environ, values, clear=True), patch('chatbot.config.load_dotenv'):
            return Config.load()

    def test_gemini_values_and_old_provider_ignored(self):
        config=self.load({"OPENAI_API_KEY":"wrong", "OPENAI_MODEL":"wrong", "OPENAI_BASE_URL":"http://wrong"})
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertFalse(config.allow_external_llm)
        config=self.load({"GEMINI_API_KEY":"  right ", "GEMINI_MODEL":" custom-model ", "ALLOW_EXTERNAL_LLM":"true"})
        self.assertEqual(config.api_key,"right")
        self.assertEqual(config.model,"custom-model")
        self.assertTrue(config.allow_external_llm)

    def test_empty_model_falls_back(self):
        self.assertEqual(self.load({"GEMINI_MODEL":" "}).model,DEFAULT_MODEL)

    def test_question_channels_optional_separate_and_validated(self):
        self.assertEqual(self.load({}).question_channel_ids, frozenset())
        self.assertEqual(self.load({"QUESTION_CHANNEL_IDS":"3, 4,3"}).question_channel_ids, frozenset({3,4}))
        for value in ("2", "0", "-3", "oops", str(2**63)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"QUESTION_CHANNEL_IDS":value})
