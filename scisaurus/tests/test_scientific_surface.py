import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.scientific_surface import (
    classify_surface_text,
    editorial_audit,
    project_internal_language,
    validate_scientific_surface,
)


class ScientificSurfaceTests(unittest.TestCase):
    def test_control_terms_are_classified_as_operational(self):
        result = classify_surface_text("The frozen artifact was accepted by the validator.")
        self.assertEqual(result["class"], "operational")
        self.assertGreaterEqual(len(result["leaks"]), 2)

    def test_projection_translates_internal_state_without_silent_release(self):
        text = "The finite by specification, protocol-bound result has insufficient_evidence; retained alternatives remain."
        projected = project_internal_language(text)
        self.assertIn("prespecified design", projected)
        self.assertIn("available literature was insufficient to determine whether", projected)
        self.assertNotEqual(projected, text)

    def test_editorial_audit_catches_repeated_numeric_fact(self):
        text = "The log loss changed by 0.10. The log loss changed by 0.10. The log loss changed by 0.10."
        audit = editorial_audit(text, max_numeric_repetitions=2)
        self.assertEqual(len(audit["numeric_repetitions"]), 1)

    def test_editorial_audit_ignores_table_labels_as_numeric_facts(self):
        text = "Table 1 reports the threshold. The control uses f(x) = 1."
        self.assertEqual(editorial_audit(text, max_numeric_repetitions=1)["numeric_repetitions"], [])

    def test_validation_rejects_surface_leak_and_allows_scientific_language(self):
        with self.assertRaisesRegex(ValidationError, "control-plane"):
            validate_scientific_surface("The frozen protocol was accepted by the validator.")
        self.assertEqual(validate_scientific_surface(
            "Under the prespecified design, the calibration map changed confidence without changing rank.")["surface"]["class"],
            "scientific")


if __name__ == "__main__":
    unittest.main()
