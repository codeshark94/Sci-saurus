"""T06–T10: critique admissibility, rebuttal, verification closure, coverage,
and the review gate that never converts failure into a pass."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.documents import Documents
from scisaurus.review.issues import IssueManager
from scisaurus.review.protocol import ReviewProtocol
from scisaurus.core.errors import ValidationError


def critique_kwargs(**overrides):
    base = dict(
        critique_id="c-x",
        author_role="strategy.adversary",
        target_ref="artifact:strategy/sections/s-2@1",
        target_location="paragraph 2, claim C7",
        criterion_ref="artifact:command/missions/acceptance@1",
        allegation="claim C7 asserts causation while the supplied result establishes an association",
        basis={"evidence_refs": ["artifact:methods/assessment/m-1@1"]},
        material_impact="C7 is a required claim; the overstatement would enter the manuscript",
        proposed_severity="blocking",
        resolution_condition="claim qualified to association, or causal procedure supplied",
        verification_method="Methods review of the revised claim",
    )
    base.update(overrides)
    return base


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-rv-")
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="review-test")
        self.documents = Documents(self.control, self.store)
        self.issues = IssueManager(self.control, self.store, self.documents)
        self.protocol = ReviewProtocol(self.control, self.store)

        self.u1 = self.documents.publish_unit(
            logical_id="strategy/units/u-1", kind="paragraph",
            text="The result shows the mechanism.", author="strategy.writer-1",
        )
        self.doc = self.documents.publish_manifest(
            document_id="strategy/documents/doc-1",
            tree={"units": [{"ref": self.u1["artifact_ref"], "children": []}]},
            author="editorial.chief",
        )

    def tearDown(self):
        self.control.close()


class TestT06Admissibility(ReviewFixture):
    def test_vague_criticism_rejected_at_boundary(self):
        with self.assertRaises(ValidationError):
            self.issues.publish_critique(
                **critique_kwargs(
                    critique_id="c-vague",
                    allegation="this section should be improved",
                    basis={},                       # no evidence, no counterexample
                    material_impact="",
                    resolution_condition="",
                )
            )
        # invented requirement: no criterion link, no basis
        with self.assertRaises(ValidationError):
            self.issues.publish_critique(
                **critique_kwargs(
                    critique_id="c-invented",
                    allegation="the study should have used a delayed-recall design",
                    basis={},
                    resolution_condition="run more experiments",
                )
            )
        # no issue opened, so no producer edit is forced
        rows = self.control._conn.execute("SELECT COUNT(*) c FROM issues").fetchone()
        self.assertEqual(rows["c"], 0)

    def test_inadmissible_critique_triaged_out(self):
        manifest = self.issues.publish_critique(
            **critique_kwargs(critique_id="c-weak", basis={"counterexample": "none demonstrated"})
        )
        # triage rejects a critique whose basis does not establish a defect
        result = self.issues.register_issue(issue_id="i-1", critique_ref=manifest["artifact_ref"])
        self.issues.triage("i-1", "methods.chief", admissible=False, reason="basis does not establish a defect")
        self.assertEqual(self.issues.get("i-1")["state"], "rejected_invalid")


class TestT07Rebuttal(ReviewFixture):
    def test_rebuttal_adjudicated_without_fake_progress(self):
        manifest = self.issues.publish_critique(
            **critique_kwargs(critique_id="c-rebut")
        )
        self.issues.register_issue(issue_id="i-1", critique_ref=manifest["artifact_ref"])
        self.issues.triage("i-1", "methods.chief", admissible=True, reason="specific and grounded")
        # producer rebuts with supplied evidence
        self.issues.respond(
            "i-1", response_id="r-1", author="strategy.writer-1", stance="rebut",
            supporting_refs=["artifact:methods/assessment/m-2@1"],
        )
        # independent adjudicator (not the critic, not the producer) upholds the rebuttal
        self.issues.adjudicate(
            "i-1", adjudication_id="a-1", adjudicator="methods.chief",
            validity="rebutted",
            rationale="the association wording already matches the supplied analysis",
            conflict_checks="adjudicator authored neither the critique nor the rebuttal",
        )
        self.assertEqual(self.issues.get("i-1")["state"], "rebutted")
        # no artifact progress was credited: no candidate was accepted anywhere
        events = [e["event_type"] for e in self.control.replay()]
        self.assertNotIn("artifact.accepted", events)


class TestT08Verification(ReviewFixture):
    def test_failed_verification_keeps_issue_open_and_candidate_unpromoted(self):
        manifest = self.issues.publish_critique(
            **critique_kwargs(critique_id="c-fix", resolution_condition="claim qualified to association")
        )
        self.issues.register_issue(issue_id="i-2", critique_ref=manifest["artifact_ref"])
        self.issues.triage("i-2", "methods.chief", admissible=True, reason="ok")
        self.issues.submit_repair("i-2", candidate_ref=self.doc["artifact_ref"], author="strategy.writer-1")
        self.issues.verify(
            "i-2", verification_id="v-1", verifier="methods.verifier",
            resolved=False,
            checks=[{"check": "claim wording", "result": "still asserts causation"}],
            rationale="patch does not resolve the named defect",
        )
        # issue stays open; the failing candidate was never adopted
        self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")
        accepted = self.store.accepted("strategy/documents/doc-1")
        self.assertIsNone(accepted)
        # a later successful repair closes the issue
        self.issues.submit_repair("i-2", candidate_ref=self.doc["artifact_ref"], author="strategy.writer-1")
        self.issues.verify(
            "i-2", verification_id="v-2", verifier="methods.verifier",
            resolved=True,
            checks=[{"check": "claim wording", "result": "qualified"}],
            rationale="claim now states association with scope language",
        )
        self.assertEqual(self.issues.get("i-2")["state"], "resolved_verified")


class TestT09Coverage(ReviewFixture):
    def test_repair_deleting_required_finding_fails_coverage(self):
        # candidate manifest that omits u-1 (the required finding)
        self.u2 = self.documents.publish_unit(
            logical_id="strategy/units/u-2", kind="paragraph", text=" filler.",
            author="strategy.writer-1",
        )
        candidate = self.documents.publish_manifest(
            document_id="strategy/documents/doc-1",
            tree={"units": [{"ref": self.u2["artifact_ref"], "children": []}]},
            author="strategy.writer-1",
        )
        with self.assertRaises(ValidationError) as ctx:
            self.issues.check_required_coverage(
                candidate_manifest_ref=candidate["artifact_ref"],
                required_units=["strategy/units/u-1"],
            )
        self.assertIn("required finding removed", str(ctx.exception))


class TestT10ReviewGate(ReviewFixture):
    def test_review_failed_never_passes(self):
        for outcome in ("review_failed", "insufficient_evidence"):
            manifest = self.protocol.publish_review_coverage(
                review_id=f"rv-{outcome}",
                reviewer="editorial.qa",
                target_refs=[self.doc["artifact_ref"]],
                checks_attempted=["consistency"],
                checks_completed=[],
                exclusions=["tool unavailable"],
                outcome=outcome,
            )
            ok, reason = self.protocol.review_passes(outcome)
            self.assertFalse(ok)
            self.assertIn("T10", reason)

    def test_completed_review_without_objections_passes(self):
        manifest = self.protocol.publish_review_coverage(
            review_id="rv-clean",
            reviewer="editorial.qa",
            target_refs=[self.doc["artifact_ref"]],
            checks_attempted=["consistency", "numbers"],
            checks_completed=["consistency", "numbers"],
            exclusions=[],
            outcome="no_valid_objection_found",
        )
        ok, reason = self.protocol.review_passes("no_valid_objection_found")
        self.assertTrue(ok, reason)
        self.assertEqual(manifest["artifact_type"], "review_coverage")

    def test_empty_pass_blocked_at_publication(self):
        with self.assertRaises(ValidationError):
            self.protocol.publish_review_coverage(
                review_id="rv-empty",
                reviewer="editorial.qa",
                target_refs=[self.doc["artifact_ref"]],
                checks_attempted=["consistency"],
                checks_completed=[],
                exclusions=[],
                outcome="no_valid_objection_found",
            )


if __name__ == "__main__":
    unittest.main()