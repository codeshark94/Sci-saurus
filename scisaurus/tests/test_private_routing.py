"""Owner-local model-family routing and shared premium-budget contracts."""
import sys
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from scisaurus.runtime.models import is_local_qwen_route


PRIVATE_ROOT = Path(__file__).resolve().parents[2] / "local-private"
if str(PRIVATE_ROOT) not in sys.path:
    sys.path.insert(0, str(PRIVATE_ROOT))

PRIVATE_ROUTING_MODULE = PRIVATE_ROOT / "role_routing.py"
if PRIVATE_ROUTING_MODULE.is_file():
    from role_routing import provider_pools, routed_model_config  # noqa: E402
else:
    routed_model_config = None
    provider_pools = None


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

    def test_validator_author_has_explicit_primary_peer_and_context(self):
        config = self.config(self.env())
        role = "methods.validator-author"
        self.assertEqual(config["role_models"][role]["model"], "deepseek-v4.1-flash:cloud")
        self.assertEqual(config["role_model_fallbacks"][role][0]["model"], "glm-5.3-flash:cloud")
        self.assertEqual(config["role_models"][role]["context_window_tokens"],
                         config["role_models"]["research.experiment-author"]["context_window_tokens"])

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
        self.assertEqual([route["pool"] for route in routes], ["ollama", "ollama"])
        self.assertEqual([route["id"] for route in routes], [
            "ollama-gemma-bulk", "ollama-deepseek",
        ])
        self.assertTrue(all(route["base_url"] == "http://127.0.0.1:11434/v1" for route in routes))
        self.assertTrue(all(
            route["context_window_tokens"] == 262144
            and route["max_input_tokens"] == 245760
            for route in routes))
        self.assertNotIn("qwen", {
            route["pool"] for route_list in config["role_routes"].values() for route in route_list
        })
        self.assertFalse(any(
            route["id"] == "ollama-qwen-bulk"
            for route_list in config["role_routes"].values() for route in route_list
        ))
        self.assertEqual(
            len({(route["pool"], route["base_url"], route["model"]) for route in routes}),
            len(routes),
        )

    def test_config_builder_help_is_non_destructive(self):
        configs = PRIVATE_ROOT / "configs"
        before = {
            str(path.relative_to(configs)): (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in configs.rglob("*") if path.is_file()
        }
        completed = subprocess.run(
            [sys.executable, str(PRIVATE_ROOT / "build_configs.py"), "--help"],
            cwd=PRIVATE_ROOT.parent,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout.lower())
        after = {
            str(path.relative_to(configs)): (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in configs.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_local_qwen_cooldown_recovery_is_removed(self):
        values = self.env(
            SCISAURUS_OLLAMA_ONLY="1",
            SCISAURUS_OLLAMA_COOLDOWN_FALLBACK_MODEL="qwen3.8:27b-mlx",
        )
        config = self.config(values)
        self.assertNotIn("provider_cooldown_fallback", config)
        default_config = self.config(self.env(SCISAURUS_OLLAMA_ONLY="1"))
        self.assertNotIn("provider_cooldown_fallback", default_config)

    def test_local_qwen_is_removed_from_every_generated_role_route(self):
        for endpoint in (
                "http://127.0.0.1:11434/v1", "http://127.1:11434/v1",
                "http://0.0.0.0:11434/v1", "http://localhost.localdomain:11434/v1"):
            with self.subTest(endpoint=endpoint):
                config = self.config(self.env(
                    SCISAURUS_QWEN_BASE_URL=endpoint,
                    SCISAURUS_QWEN_MODEL="qwen3.8:27b-mlx",
                ))
                self.assertTrue(all(
                    not is_local_qwen_route(route)
                    for route in config["role_models"].values()
                ))
                self.assertTrue(all(
                    not is_local_qwen_route(route)
                    for routes in config["role_routes"].values()
                    for route in routes
                ))
                self.assertNotIn("qwen", provider_pools({}, config))
                self.assertEqual(
                    config["role_models"]["research.cataloger"]["model"],
                    "gemma4:31b-cloud",
                )
                self.assertEqual(
                    [route["model"] for route in config["role_routes"]["research.literature-mapper"]],
                    ["gemma4:31b-cloud", "deepseek-v4.1-flash:cloud"],
                )

    def test_cooldown_fallback_requires_ollama_only_policy(self):
        with self.assertRaisesRegex(ValueError, "requires SCISAURUS_OLLAMA_ONLY"):
            self.config(self.env(
                SCISAURUS_OLLAMA_COOLDOWN_FALLBACK_MODEL="qwen3.8:27b-mlx"))

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
                self.assertEqual(selected["context_window_tokens"], 262144)
                self.assertEqual(selected["max_input_tokens"], 245760)
                routes = config["role_routes"][role]
                self.assertEqual([route["pool"] for route in routes[:2]], ["ollama", "ollama"])
                self.assertEqual(
                    {route["model"] for route in routes[:2]},
                    {"deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"})
                self.assertTrue(all(
                    route["context_window_tokens"] == 262144
                    and route["max_input_tokens"] == 245760
                    for route in routes[:2]))
                self.assertNotIn("qwen-bulk", {route["id"] for route in routes})

        bulk = config["role_routes"]["research.literature-mapper"]
        self.assertEqual([route["id"] for route in bulk], [
            "qwen-bulk", "ollama-gemma-bulk", "ollama-deepseek"])
        self.assertEqual(
            [(route["context_window_tokens"], route["max_input_tokens"]) for route in bulk],
            [(65536, 56000), (262144, 245760), (262144, 245760)],
        )

    def test_interpretation_and_argument_keep_ollama_recovery_route_after_flash_peers(self):
        config = self.config(self.env(SCISAURUS_OLLAMA_ONLY="1"))
        for role in ("strategy.interpretation", "strategy.argument"):
            with self.subTest(role=role):
                fallbacks = config["role_model_fallbacks"][role]
                self.assertEqual(
                    [route["model"] for route in fallbacks],
                    ["glm-5.3-flash:cloud", "gemma4:31b-cloud"],
                )
                self.assertTrue(all(route["base_url"] == "http://127.0.0.1:11434/v1"
                                    and route.get("auth_env") is None
                                    and route["context_window_tokens"] == 262144
                                    and route["max_input_tokens"] == 245760
                                    for route in fallbacks))
        reviewer = config["role_model_fallbacks"]["strategy.argument-reviewer"]
        self.assertEqual(
            [route["model"] for route in reviewer],
            ["deepseek-v4.1-flash:cloud", "gemma4:31b-cloud"],
        )

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
        self.assertEqual(
            config["role_routes"]["review.synthesizer"][0]["context_window_tokens"], 98304)
        self.assertEqual(
            config["role_routes"]["research.literature-mapper"][1]["context_window_tokens"],
            262144)

    def test_ollama_context_window_override_keeps_input_inside_window(self):
        config = self.config(self.env(
            SCISAURUS_OLLAMA_HIGH_CONTEXT_WINDOW_TOKENS="98304",
        ))
        self.assertEqual(
            config["role_models"]["review.synthesizer"]["context_window_tokens"], 98304)
        self.assertLessEqual(
            config["role_models"]["review.synthesizer"]["max_input_tokens"] + 8192,
            98304)

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
