"""T06–T10: critique admissibility, rebuttal, verification closure, coverage,
and the review gate that never converts failure into a pass."""

import json
import unittest
from unittest.mock import patch

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.documents import Documents
from scisaurus.core.schema import canonical_bytes
from scisaurus.review.issues import IssueManager
from scisaurus.review.protocol import ReviewProtocol
from scisaurus.core.errors import NotFoundError, StateError, ValidationError


def critique_kwargs(**overrides):
    base = dict(
        critique_id="c-x",
        author_role="strategy.adversary",
        target_ref="artifact:strategy/documents/doc-1@1",
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

        self.tempdir = tempfile.TemporaryDirectory(prefix="scisaurus-rv-")
        self.dir = self.tempdir.name
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
        self.criterion = self.store.publish_artifact(
            logical_id="command/missions/acceptance", artifact_type="note",
            author="principal", body=b"No unsupported causal claims; preserve all findings.",
        )
        for n in (1, 2):
            self.store.publish_artifact(
                logical_id=f"methods/assessment/m-{n}", artifact_type="evidence_record",
                author="methods.analyst", body=b"Only association was measured.",
            )

    def tearDown(self):
        self.control.close()
        self.tempdir.cleanup()

    def ready_issue(self, issue_id="i-2"):
        critique = self.issues.publish_critique(**critique_kwargs(critique_id=f"c-{issue_id}"))
        self.issues.register_issue(issue_id=issue_id, critique_ref=critique["artifact_ref"])
        self.issues.triage(issue_id, "methods.chief", admissible=True, reason="specific and grounded")
        return critique

    def candidate(self, text="The result shows an association.", author="strategy.writer-1"):
        unit = self.documents.publish_unit(
            logical_id="strategy/units/u-1", kind="paragraph", text=text, author=author,
        )
        return self.documents.publish_manifest(
            document_id="strategy/documents/doc-1",
            tree={"units": [{"ref": unit["artifact_ref"], "children": []}]}, author=author,
        )

    def check_evidence(self, candidate, response, *, issue_id="i-2", author="methods.verifier"):
        issue = self.issues.get(issue_id)
        critique = self.issues._record_body(issue["critique_ref"])
        candidate_tree = self.documents.get_tree(candidate["artifact_ref"])
        baseline_tree = self.documents.get_tree(self.doc["artifact_ref"])
        units = [self.documents.read_unit(n["ref"]) for n in candidate_tree["units"]]
        text = "\n".join(unit["text"] for unit in units)
        baseline_ids = {n["ref"].split("@")[0] for n in baseline_tree["units"]}
        candidate_ids = {n["ref"].split("@")[0] for n in candidate_tree["units"]}
        refs = []
        checks = (
            ("resolution", "association" in text and "mechanism" not in text,
             "Read the candidate units and check that mechanism is qualified to association.", text),
            ("regression", baseline_ids <= candidate_ids,
             "Compare required baseline unit identities with candidate identities.",
             f"required={sorted(baseline_ids)}; present={sorted(candidate_ids)}"),
        )
        for kind, passed, method, result in checks:
            body = {
                "issue_id": issue_id,
                "critique_ref": issue["critique_ref"],
                "response_ref": response["artifact_ref"],
                "candidate_ref": candidate["artifact_ref"],
                "baseline_ref": self.doc["artifact_ref"],
                "resolution_condition": critique["resolution_condition"],
                "check_id": kind,
                "kind": kind,
                "outcome": "passed" if passed else "failed",
                "method": method,
                "result": result,
            }
            check = self.store.publish_artifact(
                logical_id=f"issues/checks/{issue_id}-{kind}", artifact_type="evidence_record",
                author=author, body=canonical_bytes(body), media_type="application/json",
                inputs=[
                    {"ref": ref, "purpose": "subject"}
                    for ref in (self.doc["artifact_ref"], candidate["artifact_ref"])
                ],
            )
            refs.append(check["artifact_ref"])
        return refs

    def verification_args(self, candidate, response, **overrides):
        args = dict(
            verification_id="v-1", verifier="methods.verifier",
            response_ref=response["artifact_ref"], candidate_ref=candidate["artifact_ref"],
            baseline_ref=self.doc["artifact_ref"],
            resolution_condition=critique_kwargs()["resolution_condition"],
            check_refs=self.check_evidence(candidate, response),
            rationale="Candidate wording and retained findings inspected against the baseline.",
        )
        args.update(overrides)
        return args


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
    def prepare_repair(self):
        self.ready_issue()
        candidate = self.candidate()
        response = self.issues.submit_repair(
            "i-2", candidate_ref=candidate["artifact_ref"], author="strategy.writer-1",
        )
        return candidate, response, self.verification_args(candidate, response)

    def test_failed_verification_keeps_issue_open_and_candidate_unpromoted(self):
        self.ready_issue()
        bad = self.candidate("The result conclusively shows the mechanism.")
        response = self.issues.submit_repair("i-2", bad["artifact_ref"], "strategy.writer-1")
        self.issues.verify("i-2", **self.verification_args(bad, response))
        self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")
        self.assertIsNone(self.store.accepted("strategy/documents/doc-1"))

        repaired = self.candidate()
        response = self.issues.submit_repair("i-2", repaired["artifact_ref"], "strategy.writer-1")
        verification = self.issues.verify(
            "i-2", **self.verification_args(repaired, response, verification_id="v-2"),
        )
        self.assertEqual(self.issues.get("i-2")["state"], "resolved_verified")
        body = self.issues._record_body(verification["artifact_ref"])
        self.assertTrue(body["resolved"])
        self.assertEqual(body["candidate_ref"], repaired["artifact_ref"])
        self.assertEqual(body["response_ref"], response["artifact_ref"])
        self.assertEqual(body["baseline_ref"], self.doc["artifact_ref"])
        self.assertIsNone(self.store.accepted("strategy/documents/doc-1"))

    def test_missing_candidate_does_not_publish_a_response(self):
        self.ready_issue()
        with self.assertRaises(NotFoundError):
            self.issues.submit_repair("i-2", "artifact:strategy/missing-candidate@999", "strategy.writer-1")
        self.assertIsNone(self.store.head("issues/responses/resp-i-2"))
        self.assertEqual(self.issues.get("i-2")["state"], "awaiting_response")

    def test_unchanged_and_unrelated_candidates_are_not_repairs(self):
        self.ready_issue()
        unrelated = self.store.publish_artifact(
            logical_id="strategy/notes/unrelated", artifact_type="note",
            author="strategy.writer-1", body=b"Association.",
        )
        unchanged = self.documents.publish_manifest(
            document_id="strategy/documents/doc-1",
            tree=self.documents.get_tree(self.doc["artifact_ref"]), author="strategy.writer-1",
        )
        for candidate in (self.doc, unchanged, unrelated):
            with self.subTest(ref=candidate["artifact_ref"]), self.assertRaises(ValidationError):
                self.issues.submit_repair("i-2", candidate["artifact_ref"], "strategy.writer-1")
        self.assertIsNone(self.store.head("issues/responses/resp-i-2"))

    def test_empty_checks_leave_submitted_repair_unchanged(self):
        _, _, args = self.prepare_repair()
        args["check_refs"] = []
        with self.assertRaises(ValidationError):
            self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")
        self.assertIsNone(self.store.head("issues/verifications/v-1"))

    def test_explicit_regression_prevents_closure_despite_passing_checks(self):
        _, _, args = self.prepare_repair()
        args["regressions"] = ["Required finding deleted"]
        verification = self.issues.verify("i-2", **args)
        self.assertFalse(self.issues._record_body(verification["artifact_ref"])["resolved"])
        self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")

    def test_uncertainty_or_incomplete_coverage_does_not_close_issue(self):
        candidate, response, args = self.prepare_repair()
        args["uncertainties"] = ["Finding coverage could not be established."]
        self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")
        response = self.issues.submit_repair("i-2", candidate["artifact_ref"], "strategy.writer-1")
        args = self.verification_args(candidate, response, verification_id="v-2")
        args["check_refs"] = args["check_refs"][:1]
        verification = self.issues.verify("i-2", **args)
        body = self.issues._record_body(verification["artifact_ref"])
        self.assertFalse(body["resolved"])
        self.assertEqual(body["missing_check_kinds"], ["regression"])

    def test_producer_critic_and_target_author_cannot_verify(self):
        _, _, args = self.prepare_repair()
        for author in ("strategy.writer-1", "strategy.adversary", "editorial.chief"):
            with self.subTest(author=author), self.assertRaises(ValidationError):
                self.issues.verify("i-2", **{**args, "verifier": author})
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")

    def test_independent_judge_cannot_use_producer_authored_checks(self):
        candidate, response, args = self.prepare_repair()
        args["check_refs"] = self.check_evidence(candidate, response, author="strategy.writer-1")
        with self.assertRaises(ValidationError):
            self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")

    def test_verification_must_match_candidate_baseline_condition_and_response(self):
        candidate, response, args = self.prepare_repair()
        other = self.candidate("Another association claim.")
        overrides = (
            {"candidate_ref": other["artifact_ref"]},
            {"baseline_ref": candidate["artifact_ref"]},
            {"resolution_condition": "Whatever the producer prefers."},
            {"response_ref": "artifact:issues/responses/nonexistent@1"},
        )
        for changed in overrides:
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.issues.verify("i-2", **{**args, **changed})
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")

    def test_checks_for_another_candidate_or_response_are_rejected(self):
        candidate, response, args = self.prepare_repair()
        stale_checks = args["check_refs"]
        self.issues.verify("i-2", **{**args, "regressions": ["Coverage not preserved."]})
        response = self.issues.submit_repair("i-2", candidate["artifact_ref"], "strategy.writer-1")
        args = self.verification_args(candidate, response, verification_id="v-2", check_refs=stale_checks)
        with self.assertRaises(ValidationError):
            self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")

    def test_non_check_artifact_cannot_support_closure(self):
        _, _, args = self.prepare_repair()
        args["check_refs"] = [self.criterion["artifact_ref"]]
        with self.assertRaises(ValidationError):
            self.issues.verify("i-2", **args)

    def test_failed_or_inconclusive_check_cannot_close_issue(self):
        candidate, response, args = self.prepare_repair()
        for n, outcome in enumerate(("failed", "insufficient_evidence", "check_failed")):
            if n:
                response = self.issues.submit_repair("i-2", candidate["artifact_ref"], "strategy.writer-1")
                args = self.verification_args(candidate, response, verification_id=f"v-{n + 1}")
            original = self.store.get(args["check_refs"][0])
            check = self.issues._record_body(original["artifact_ref"])
            check["outcome"] = outcome
            check["result"] = "The required condition could not be established."
            evidence = self.store.publish_artifact(
                logical_id=original["artifact_id"], artifact_type="evidence_record",
                author=original["author"], body=canonical_bytes(check), inputs=original["inputs"],
            )
            args["check_refs"][0] = evidence["artifact_ref"]
            verification = self.issues.verify("i-2", **args)
            self.assertFalse(self.issues._record_body(verification["artifact_ref"])["resolved"])
            self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")

    def test_check_level_regression_cannot_be_hidden_by_pass_outcome(self):
        _, _, args = self.prepare_repair()
        original = self.store.get(args["check_refs"][1])
        check = self.issues._record_body(original["artifact_ref"])
        check["regressions"] = ["Required finding disappeared from the candidate."]
        evidence = self.store.publish_artifact(
            logical_id=original["artifact_id"], artifact_type="evidence_record",
            author=original["author"], body=canonical_bytes(check), inputs=original["inputs"],
        )
        args["check_refs"][1] = evidence["artifact_ref"]
        verification = self.issues.verify("i-2", **args)
        self.assertFalse(self.issues._record_body(verification["artifact_ref"])["resolved"])
        self.assertEqual(self.issues.get("i-2")["state"], "upheld_open")

    def test_missing_check_input_binding_is_rejected(self):
        _, _, args = self.prepare_repair()
        original = self.store.get(args["check_refs"][0])
        evidence = self.store.publish_artifact(
            logical_id=original["artifact_id"], artifact_type="evidence_record",
            author=original["author"], body=self.store.read_body(original["body_hash"]), inputs=[],
        )
        args["check_refs"][0] = evidence["artifact_ref"]
        with self.assertRaises(ValidationError):
            self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")

    def test_verdict_and_transition_rollback_together_on_event_failure(self):
        _, _, args = self.prepare_repair()
        before = list(self.control.replay())
        append = self.control.append_event

        def fail_on_completion(conn, **kwargs):
            if kwargs["event_type"] == "verification.completed":
                raise RuntimeError("event storage unavailable")
            return append(conn, **kwargs)

        with patch.object(self.control, "append_event", side_effect=fail_on_completion):
            with self.assertRaises(RuntimeError):
                self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "repair_submitted")
        self.assertIsNone(self.store.head("issues/verifications/v-1"))
        self.assertEqual(list(self.control.replay()), before)
        self.issues.verify("i-2", **args)
        self.assertEqual(self.issues.get("i-2")["state"], "resolved_verified")


class TestResponseAndAdjudicationBoundaries(ReviewFixture):
    def test_request_evidence_is_published_with_a_valid_transition(self):
        self.ready_issue()
        response = self.issues.respond(
            "i-2", response_id="r-evidence", author="strategy.writer-1", stance="request_evidence",
        )
        self.assertEqual(self.issues.get("i-2")["state"], "needs_evidence")
        self.assertEqual(self.issues._record_body(response["artifact_ref"])["stance"], "request_evidence")
        duplicate = self.issues.publish_critique(**critique_kwargs(critique_id="c-same-issue"))
        result = self.issues.register_issue(issue_id="i-duplicate", critique_ref=duplicate["artifact_ref"])
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["issue"]["issue_id"], "i-2")
        self.issues.respond(
            "i-2", response_id="r-evidence-supplied", author="strategy.writer-1", stance="rebut",
            supporting_refs=["artifact:methods/assessment/m-2@1"],
        )
        self.assertEqual(self.issues.get("i-2")["state"], "adjudication_pending")

    def test_missing_required_candidate_does_not_publish(self):
        self.ready_issue()
        with self.assertRaises(ValidationError):
            self.issues.respond("i-2", response_id="r-bad", author="strategy.writer-1", stance="accept")
        self.assertIsNone(self.store.head("issues/responses/r-bad"))
        self.assertEqual(self.issues.get("i-2")["state"], "awaiting_response")

    def test_response_and_transition_rollback_together_on_event_failure(self):
        self.ready_issue()
        before = list(self.control.replay())
        append = self.control.append_event

        def fail_on_transition(conn, **kwargs):
            if kwargs["event_type"] == "issue.transitioned":
                raise RuntimeError("event storage unavailable")
            return append(conn, **kwargs)

        with patch.object(self.control, "append_event", side_effect=fail_on_transition):
            with self.assertRaises(RuntimeError):
                self.issues.respond(
                    "i-2", response_id="r-evidence", author="strategy.writer-1", stance="request_evidence",
                )
        self.assertEqual(self.issues.get("i-2")["state"], "awaiting_response")
        self.assertIsNone(self.store.head("issues/responses/r-evidence"))
        self.assertEqual(list(self.control.replay()), before)

    def test_critic_producer_and_responders_must_recuse_from_adjudication(self):
        self.ready_issue()
        duplicate = self.issues.publish_critique(**critique_kwargs(
            critique_id="c-second-critic", author_role="methods.adversary",
        ))
        self.issues.register_issue(issue_id="i-duplicate", critique_ref=duplicate["artifact_ref"])
        response = self.issues.respond(
            "i-2", response_id="r-rebut", author="strategy.responder", stance="rebut",
            supporting_refs=["artifact:methods/assessment/m-2@1"],
        )
        for actor in (
            "strategy.adversary", "methods.adversary", "strategy.responder",
            "editorial.chief", "strategy.writer-1",
        ):
            with self.subTest(actor=actor), self.assertRaises(ValidationError):
                self.issues.adjudicate(
                    "i-2", adjudication_id="a-bad", adjudicator=actor, validity="upheld",
                    rationale="The objection is valid.", conflict_checks="Caller claims independence.",
                )
        self.assertEqual(self.issues.get("i-2")["state"], "adjudication_pending")
        self.assertIsNone(self.store.head("issues/adjudications/a-bad"))
        adjudication = self.issues.adjudicate(
            "i-2", adjudication_id="a-good", adjudicator="methods.arbiter", validity="rebutted",
            rationale="Counterevidence refutes the objection.", conflict_checks="No production or dispute authorship.",
        )
        body = self.issues._record_body(adjudication["artifact_ref"])
        self.assertEqual(body["response_refs"], [response["artifact_ref"]])
        self.assertEqual(body["critique_ref"], self.issues.get("i-2")["critique_ref"])
        self.assertEqual(self.issues.get("i-2")["state"], "rebutted")

    def test_adjudication_and_state_rollback_together(self):
        self.ready_issue()
        self.issues.respond("i-2", response_id="r-rebut", author="strategy.writer-1", stance="rebut")
        append = self.control.append_event
        before = list(self.control.replay())

        def fail_on_transition(conn, **kwargs):
            if kwargs["event_type"] == "issue.transitioned":
                raise RuntimeError("event storage unavailable")
            return append(conn, **kwargs)

        with patch.object(self.control, "append_event", side_effect=fail_on_transition):
            with self.assertRaises(RuntimeError):
                self.issues.adjudicate(
                    "i-2", adjudication_id="a-1", adjudicator="methods.arbiter", validity="upheld",
                    rationale="The objection is valid.", conflict_checks="No production or dispute authorship.",
                )
        self.assertIsNone(self.store.head("issues/adjudications/a-1"))
        self.assertEqual(self.issues.get("i-2")["state"], "adjudication_pending")
        self.assertEqual(list(self.control.replay()), before)

    def test_invalid_state_does_not_publish_another_response(self):
        self.ready_issue()
        self.issues.respond("i-2", response_id="r-rebut", author="strategy.writer-1", stance="rebut")
        with self.assertRaises(StateError):
            self.issues.respond(
                "i-2", response_id="r-too-late", author="strategy.writer-1", stance="request_evidence",
            )
        self.assertIsNone(self.store.head("issues/responses/r-too-late"))
        self.assertEqual(self.issues.get("i-2")["state"], "adjudication_pending")


class TestT09Coverage(ReviewFixture):
    def test_required_coverage_traverses_nested_sections(self):
        headings = [
            self.documents.publish_unit(
                logical_id=f"strategy/units/heading-{n}", kind="heading",
                text=f"Section {n}", author="strategy.writer-1",
            )
            for n in (1, 2)
        ]
        candidate = self.documents.publish_manifest(
            document_id="strategy/documents/doc-1", author="strategy.writer-1",
            tree={"units": [{"ref": headings[0]["artifact_ref"], "children": [
                {"ref": headings[1]["artifact_ref"], "children": [
                    {"ref": self.u1["artifact_ref"], "children": []},
                ]},
            ]}]},
        )
        self.issues.check_required_coverage(
            candidate_manifest_ref=candidate["artifact_ref"], required_units=["strategy/units/u-1"],
        )

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
