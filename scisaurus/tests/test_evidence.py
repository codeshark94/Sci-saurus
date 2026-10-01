"""Scientific evidence context retains exact accepted source provenance."""
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.evidence import evidence_ids_from_packet, survey_evidence_projection


class EvidenceProjectionTests(unittest.TestCase):
    def test_decisive_quotation_survives_background_and_excerpt_limits(self):
        sources = {f"artifact:kb/abstract/W{i}@1": {"work_id": f"W{i}", "abstract": "Background."}
                   for i in range(60)}
        source_ref = "artifact:kb/full-text/W99@1"
        quote = "Efficiency reverses with coupling."
        sources[source_ref] = {"work_id": "W99", "text": "Introductory text. " * 150 + quote,
                               "representation": "full_text"}
        proof = {"work_id": "W99", "source_ref": source_ref, "quote": quote}
        packet = survey_evidence_projection({"sources": sources, "assessment": {"evidence": [proof]},
                                             "survey_ref": "survey@1", "assessment_ref": "assessment@1"})
        self.assertEqual(packet["reference_cards"][0]["source_ref"], source_ref)
        exact = next(item for item in packet["literature_evidence"] if item["quote"] == quote)
        self.assertGreater(exact["start"], 1800)
        self.assertIn(exact["id"], evidence_ids_from_packet(packet))
        self.assertEqual(packet["survey_assessment"]["evidence"], [{"evidence_id": exact["id"]}])
        self.assertEqual(packet["survey_lineage"]["survey_ref"], "survey@1")
        from scisaurus.runtime.research_argument import argument_evidence_packet
        from scisaurus.runtime.paper_pipeline import project_writer_packet
        for consumer in (argument_evidence_packet, lambda value: project_writer_packet(value, {})):
            projected = consumer(packet)
            self.assertEqual(projected["survey_lineage"], packet["survey_lineage"])
            self.assertEqual(projected["survey_assessment"], packet["survey_assessment"])

    def test_unbound_assessment_quote_is_rejected(self):
        with self.assertRaises(ValidationError):
            survey_evidence_projection({"sources": {}, "assessment": {"evidence": [
                {"work_id": "W99", "source_ref": "absent", "quote": "Uncaptured."}]}})

    def test_writer_keeps_every_referenced_authoritative_quote(self):
        from scisaurus.runtime.paper_pipeline import project_writer_packet
        source_ref = "artifact:kb/full-text/W1@1"
        quotes = [f"Captured distinct measurement number {index}." for index in range(300)]
        packet = survey_evidence_projection({
            "sources": {source_ref: {"work_id": "W1", "text": " ".join(quotes)}},
            "assessment": {"evidence": [{"work_id": "W1", "source_ref": source_ref, "quote": quote} for quote in quotes]}})
        projected = project_writer_packet(packet, {})
        required = {item["evidence_id"] for item in projected["survey_assessment"]["evidence"]}
        retained = {item["id"] for item in projected["literature_evidence"]}
        self.assertTrue(required.issubset(retained))
