"""Owner-local model-family routing and shared premium-budget contracts."""
import sys
from pathlib import Path
import tempfile
import unittest


PRIVATE_ROOT = Path(__file__).resolve().parents[2] / "local-private"
if str(PRIVATE_ROOT) not in sys.path:
    sys.path.insert(0, str(PRIVATE_ROOT))

PRIVATE_ROUTING_MODULE = PRIVATE_ROOT / "role_routing.py"
if PRIVATE_ROUTING_MODULE.is_file():
    from role_routing import routed_model_config  # noqa: E402
else:
    routed_model_config = None


@unittest.skipUnless(PRIVATE_ROUTING_MODULE.is_file(),
                     "owner-local role_routing.py is not part of the public checkout")
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

    def test_ollama_only_maps_qwen_bulk_and_ignores_remote_credentials(self):
        values = self.env(
            SCISAURUS_OLLAMA_ONLY="1",
            SCISAURUS_OLLAMA_BULK_MODEL="gemma4:31b-cloud",
            SCISAURUS_QWEN_ENV_FILE="missing/private-qwen.env",
        )
        for key in ("SCISAURUS_QWEN_BASE_URL", "SCISAURUS_QWEN_MODEL", "SCISAURUS_QWEN_API_KEY"):
            values.pop(key, None)
        config = self.config(values)

        self.assertEqual(
            config["role_models"]["research.cataloger"]["model"],
            "gemma4:31b-cloud",
        )
        self.assertIsNone(config["role_models"]["research.cataloger"].get("auth_env"))
        routes = config["role_routes"]["research.literature-mapper"]
        self.assertEqual([route["pool"] for route in routes], ["ollama", "ollama", "ollama"])
        self.assertEqual([route["id"] for route in routes], [
            "ollama-qwen-bulk", "ollama-gemma-bulk", "ollama-deepseek",
        ])
        self.assertTrue(all(route["base_url"] == "http://127.0.0.1:11434/v1" for route in routes))
        self.assertNotIn("qwen", {
            route["pool"] for route_list in config["role_routes"].values() for route in route_list
        })

    def test_evidence_integrators_use_ollama_high_context_without_bulk_spillover(self):
        config = self.config(self.env())
        high_roles = {
            "research.experiment-author",
            "research.gap-proposer",
            "methods.novelty-challenger",
            "methods.novelty-verifier",
            "methods.survey-reviewer",
            "review.synthesizer",
            "review.journal_editor",
        }
        for role in high_roles:
            with self.subTest(role=role):
                selected = config["role_models"][role]
                self.assertIn(selected["model"], {
                    "deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"})
                self.assertEqual(selected["context_window_tokens"], 131072)
                self.assertEqual(selected["max_input_tokens"], 112000)
                routes = config["role_routes"][role]
                self.assertEqual([route["pool"] for route in routes[:2]], ["ollama", "ollama"])
                self.assertEqual(
                    {route["model"] for route in routes[:2]},
                    {"deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"})
                self.assertTrue(all(
                    route["context_window_tokens"] == 131072
                    and route["max_input_tokens"] == 112000
                    for route in routes[:2]))
                self.assertNotIn("qwen-bulk", {route["id"] for route in routes})

        bulk = config["role_routes"]["research.literature-mapper"]
        self.assertEqual([route["id"] for route in bulk], [
            "qwen-bulk", "ollama-gemma-bulk", "ollama-deepseek"])
        self.assertTrue(all(route["context_window_tokens"] == 65536 for route in bulk))

    def test_high_context_profile_can_be_tuned_independently(self):
        config = self.config(self.env(
            SCISAURUS_OLLAMA_HIGH_CONTEXT_WINDOW_TOKENS="98304",
            SCISAURUS_OLLAMA_HIGH_MAX_INPUT_TOKENS="90000",
        ))
        self.assertEqual(
            config["role_models"]["review.synthesizer"]["context_window_tokens"], 98304)
        self.assertEqual(
            config["role_models"]["review.synthesizer"]["max_input_tokens"], 90000)
        self.assertEqual(
            config["role_models"]["research.literature-mapper"]["context_window_tokens"], 65536)

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
