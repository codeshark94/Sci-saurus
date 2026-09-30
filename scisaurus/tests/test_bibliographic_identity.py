"""Bibliographic identity reconciliation tests."""
import unittest

from scisaurus.runtime.bibliographic_identity import (crossref_observation, openalex_observation,
                                                       reconcile)


class BibliographicIdentityTests(unittest.TestCase):
    def observations(self, *, title="Same title", year=2024):
        work = {"work_id": "W1", "doi": "10.1234/example", "title": "Same title", "year": 2024}
        source = {"doi": "10.1234/EXAMPLE", "title": title,
                  "published": {"date-parts": [[year]]}}
        return (openalex_observation(work, "artifact:kb/works/W1@1"),
                crossref_observation(source, "artifact:command/executions/crossref@1", work_id="W1"))

    def test_verifies_matching_independent_metadata(self):
        identity = reconcile(*self.observations(), lookup_execution_ref="artifact:command/executions/crossref@1")
        self.assertEqual(identity["status"], "verified")
        self.assertTrue(all(check["outcome"] == "match" for check in identity["checks"]))

    def test_one_year_variance_with_matching_doi_and_title_is_not_an_identity_conflict(self):
        identity = reconcile(*self.observations(year=2025))
        self.assertEqual(identity["status"], "verified_with_gaps")
        year = next(check for check in identity["checks"] if check["field"] == "year")
        self.assertEqual(year["outcome"], "compatible_variance")
        self.assertEqual(year["openalex"], 2024)
        self.assertEqual(year["crossref"], 2025)
        self.assertEqual(year["variance_years"], 1)

    def test_larger_year_variance_remains_a_conflict(self):
        identity = reconcile(*self.observations(year=2026))
        self.assertEqual(identity["status"], "conflicted")
        year = next(check for check in identity["checks"] if check["field"] == "year")
        self.assertEqual(year["outcome"], "conflict")

    def test_preserves_field_level_conflicts(self):
        identity = reconcile(*self.observations(title="Different title", year=2023))
        self.assertEqual(identity["status"], "conflicted")
        self.assertEqual({check["field"] for check in identity["checks"] if check["outcome"] == "conflict"},
                         {"title", "year"})

    def test_missing_crossref_record_is_insufficient_not_verified(self):
        openalex, _ = self.observations()
        identity = reconcile(openalex, None, lookup_execution_ref="artifact:command/executions/crossref@1")
        self.assertEqual(identity["status"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
