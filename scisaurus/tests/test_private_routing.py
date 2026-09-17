"""Owner-local model-family routing and shared premium-budget contracts."""
import sys
from pathlib import Path
import tempfile
import unittest


PRIVATE_ROOT = Path(__file__).resolve().parents[2] / "local-private"
if str(PRIVATE_ROOT) not in sys.path:
    sys.path.insert(0, str(PRIVATE_ROOT))

from role_routing import routed_model_config  # noqa: E402


class TestPrivateRouting(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "SCISAURUS_OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
            "SCISAURUS_OLLAMA_MODEL": "deepseek-v4.1-flash:cloud",
            "SCISAURUS_OLLAMA_STRONG_ALT_MODEL": "glm-5.3-flash:cloud",
            "SCISAURUS_QWEN_BASE_URL": "https://qwen.example/v1",
            "SCISAURUS_QWEN_MODEL": "qwen3.8-27b",
            "SCISAURUS_QWEN_API_KEY": "test-key",
        }
        values.update(overrides)
        return values

    def config(self, values):
        return routed_model_config(
            values,
            root=Path.cwd(),
            default_base_url="http://127.0.0.1:11434/v1",
            default_model="deepseek-v4.1-flash:cloud",
            default_weak_model="gemma4:31b-cloud",
        )

    def test_default_mix_keeps_bulk_models_active_and_premium_off(self):
        config = self.config(self.env())
        self.assertEqual(config["role_models"]["research.cataloger"]["model"], "qwen3.8-27b")
        self.assertEqual(config["role_models"]["editorial.writer"]["model"], "gemma4:31b-cloud")
        self.assertEqual(config["role_models"]["methods.methodologist"]["model"], "deepseek-v4.1-flash:cloud")
        self.assertEqual(config["role_models"]["methods.survey-reviewer"]["model"], "glm-5.3-flash:cloud")
        self.assertEqual(
            config["role_models"]["research.topic-source-challenger"]["model"],
            "deepseek-v4.1-flash:cloud",
        )
        self.assertEqual(
            config["role_models"]["research.topic-maturity-reviewer"]["model"],
            "deepseek-v4.1-flash:cloud",
        )
        self.assertEqual(
            [route["model"] for route in config["role_routes"]["research.topic-maturity-reviewer"]],
            ["deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"],
        )
        premium_ids = {
            route["id"]
            for routes in config["role_routes"].values()
            for route in routes
            if "premium" in route["id"]
        }
        self.assertEqual(premium_ids, set())

    def test_kimi_and_full_glm_share_one_twenty_call_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(self.env(
                SCISAURUS_ENABLE_KIMI="1",
                SCISAURUS_ENABLE_PREMIUM_GLM="1",
                SCISAURUS_PREMIUM_MODEL_BUDGET_PATH=str(Path(directory) / "budget.sqlite"),
            ))
        routes = config["role_routes"]["review.journal_editor"]
        premium = [route for route in routes if "premium" in route["id"]]
        self.assertEqual([route["model"] for route in premium], ["glm-5.3:cloud", "kimi-k3:cloud"])
        self.assertEqual({route["model_call_budget_key"] for route in premium}, {"kimi-k3+glm-5.3"})
        self.assertEqual({route["model_call_budget_limit"] for route in premium}, {20})
        self.assertEqual(
            config["role_models"]["review.journal_editor"]["model"], "kimi-k3:cloud")
        self.assertEqual(
            config["role_models"]["research.topic-maturity-reviewer"]["model"],
            "deepseek-v4.1-flash:cloud")

    def test_premium_limit_cannot_be_raised_above_twenty(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            self.config(self.env(SCISAURUS_ENABLE_KIMI="1", SCISAURUS_PREMIUM_MODEL_CALL_LIMIT="21"))


if __name__ == "__main__":
    unittest.main()
