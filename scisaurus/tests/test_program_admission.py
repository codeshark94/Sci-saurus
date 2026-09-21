import unittest
from copy import deepcopy

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.program_admission import (
    SCHEMA_VERSION,
    scan_program_source,
    validate_program_candidate,
)

EXECUTOR = """import json
import sys

import numpy as np


def main():
    request = json.load(sys.stdin)
    run_count = int(request["experiment"]["run_count"])
    values = np.linspace(0.0, 1.0, run_count)
    print(json.dumps({"schema_version": "experiment-program-output-1", "n": int(values.size)}))


if __name__ == "__main__":
    main()
"""

VALIDATOR = """import json
import sys


def main():
    request = json.load(sys.stdin)
    candidate = request.get("candidate")
    print(json.dumps({"schema_version": "experiment-validation-1",
                      "observed": None if candidate is None else len(candidate)}))


if __name__ == "__main__":
    main()
"""

INTENT = {
    "id": "generated_study",
    "revision": 1,
    "study_type": "methods_validation",
    "domain": "computational statistics",
    "research_question": "Does the declared estimator reduce tail error under the declared process?",
    "hypothesis": "The declared estimator reduces the declared tail error metric.",
    "method": "Run a seeded finite comparison and record every replicate before summarizing.",
    "parameters": {"sample_size": 100},
    "seed": 7,
    "run_count": 100,
    "stopping_rule": "Execute exactly the declared replicate count.",
    "primary_outcomes": [
        {"id": "tail_error", "definition": "95th-percentile absolute error.", "unit": "error",
         "direction": "lower", "threshold": None},
    ],
    "limitations": ["Only the declared finite simulation is covered."],
    "required_assets": [{"role": "figure", "media_types": ["image/png"], "min_count": 1}],
    "reviewers": [
        {"id": "statistical_method", "focus": "Estimator definitions, paired comparison, numerical traceability."},
        {"id": "adversarial_claims", "focus": "Overstatement, hidden negative results, missing limitations."},
    ],
    "stage_seconds": {"setup": 10, "supervision": 10, "production": 10,
                      "unit_review": 10, "integrated_review": 10, "reassessment": 10},
    "max_observations": 1000,
    "max_asset_bytes": 1000000,
}


def candidate():
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": "generated_study",
        "revision": 1,
        "executor_source": EXECUTOR,
        "validator_source": VALIDATOR,
        "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
        "test_vector": {"input": {"probe": True}, "expected_output_sha256": "a" * 64},
        "experiment_intent": deepcopy(INTENT),
    }


class ProgramAdmissionTests(unittest.TestCase):
    def test_novel_capability_schema_preserves_its_quality_contract(self):
        from scisaurus.runtime.research_quality import default_research_quality_contract
        value = candidate()
        value["experiment_intent"]["study_type"] = "novel_research"
        with self.assertRaisesRegex(ValidationError, "quality_contract"):
            validate_program_candidate(value)
        value["experiment_intent"]["quality_contract"] = default_research_quality_contract()
        self.assertEqual(validate_program_candidate(value), value)

    def test_valid_candidate_is_admitted(self):
        value = candidate()
        self.assertEqual(validate_program_candidate(value), value)
        self.assertEqual(scan_program_source(EXECUTOR, "program executor"), {"json", "sys", "numpy"})

    def test_forbidden_module_is_rejected(self):
        value = candidate()
        value["executor_source"] = "import socket\nprint(socket.gethostname())\n"
        with self.assertRaisesRegex(ValidationError, "forbidden modules"):
            validate_program_candidate(value)

    def test_module_outside_allowlist_is_rejected(self):
        value = candidate()
        value["validator_source"] = "import requests\nprint(1)\n"
        with self.assertRaisesRegex(ValidationError, "forbidden modules"):
            validate_program_candidate(value)

    def test_forbidden_call_is_rejected(self):
        value = candidate()
        value["executor_source"] = "import json\neval('1')\n"
        with self.assertRaisesRegex(ValidationError, "forbidden function"):
            validate_program_candidate(value)

    def test_forbidden_attribute_is_rejected(self):
        value = candidate()
        value["executor_source"] = "import os\nos.system('echo hi')\n"
        with self.assertRaisesRegex(ValidationError, "forbidden attribute"):
            validate_program_candidate(value)

    def test_shared_source_is_rejected(self):
        value = candidate()
        value["validator_source"] = EXECUTOR
        with self.assertRaisesRegex(ValidationError, "independently authored"):
            validate_program_candidate(value)

    def test_runtime_requires_pins(self):
        value = candidate()
        value["runtime"] = {"python": "3.14", "packages": []}
        with self.assertRaisesRegex(ValidationError, "pinned package"):
            validate_program_candidate(value)

    def test_test_vector_digest_is_checked(self):
        value = candidate()
        value["test_vector"] = {"input": {"probe": True}, "expected_output_sha256": "nothex"}
        with self.assertRaisesRegex(ValidationError, "64-hex"):
            validate_program_candidate(value)

    def test_intent_is_checked_by_the_experiment_contract(self):
        value = candidate()
        intent = dict(value["experiment_intent"])
        intent.pop("required_assets")
        value["experiment_intent"] = intent
        with self.assertRaises(ValidationError):
            validate_program_candidate(value)

    def test_study_identity_must_match(self):
        value = candidate()
        value["study_id"] = "other_study"
        with self.assertRaisesRegex(ValidationError, "must match"):
            validate_program_candidate(value)


if __name__ == "__main__":
    unittest.main()
