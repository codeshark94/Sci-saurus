"""Survey prerequisites bind independent runtime evidence and governing heads."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ConflictError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.core.surveys import (
    ASSESSMENT_CHECKS, RELATIONSHIP_SEMANTICS, SURVEY_CHECKS, WORK_CHECKS, SurveyGate, work_review_checks,
)
from scisaurus.core.tasks import TaskManager


def checks(names, outcome="passed"):
    return [{"check_id": name, "outcome": outcome,
             "method": "Inspect the pinned source records", "result": "All scoped assertions are supported"}
            for name in sorted(names)]


class TestSurveyGate(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scisaurus-survey-")
        self.control = ControlStore(self.directory.name)
        self.store = ArtifactStore(self.control)
        self.store.init_project()
        self.gate = SurveyGate(self.control, self.store)
        self.tasks = TaskManager(self.control)
        self.serial = 0
        self.nomination = None
        self.score = self.publish("inputs/score", {"question": "Does the treatment generalize?"})
        self.protocol = self.publish("kb/protocol", {"scope": "configured work register"})
        self.coverage = self.publish("kb/coverage", {"queries": 1})
        self.query = self.publish("kb/query", {"query": "treatment generalization"})
        self.work = self.publish("kb/work", {"work_id": "doi:10.1000/known", "title": "Known Treatment"})
        text = "Known Treatment\nMethods\nThe treatment also works in condition B.\nResults\nSuccessful replication."
        self.capture = self.fetch(text)
        self.source_body = {
            "representation": "full_text", "work_id": "doi:10.1000/known",
            "text": text,
            "identity_verified": True, "identity_checks": {"title_match": True, "section_markers": ["Methods", "Results"]},
            "execution_ref": self.capture, "url": "https://example.org/known",
        }
        self.source = self.publish("kb/source", self.source_body, author="methods.source-verifier", kind="source_capture")
        self.entry_body = {"work_id": "doi:10.1000/known", "inclusion": "included",
            "reason": "The treatment is evaluated in the configured domain.",
            "problem": {"text": None, "evidence": []}, "approach": {"text": None, "evidence": []},
            "finding": {"text": "The treatment also works in condition B.", "evidence": [{
                "work_id": "doi:10.1000/known", "source_ref": self.source,
                "quote": "The treatment also works in condition B."}]},
            "limitations": {"text": None, "evidence": []}}
        self.entry = self.publish("kb/entry", self.entry_body)
        self.map = self.publish("kb/map", {"entry_refs": [self.entry], "relationship_refs": []})
        self.work_review = self.work_review_for(self.entry)
        self.survey_body = {
            "schema_version": "literature-survey-2", "score_ref": self.score,
            "protocol_ref": self.protocol, "map_ref": self.map, "coverage_ref": self.coverage,
            "source_refs": [self.source], "work_refs": [self.work], "query_refs": [self.query],
            "work_review_refs": [self.work_review],
            "dependency_refs": [self.score, self.protocol, self.map, self.coverage, self.source,
                                self.work, self.query, self.capture, self.entry, self.work_review,
                                self.body(self.work_review)["execution_ref"]],
        }
        self.survey = self.publish("kb/survey", self.survey_body)
        self.review = self.review_for(self.survey)

    def tearDown(self):
        self.control.close()
        self.directory.cleanup()

    def publish(self, logical, body, *, author="research.mapper", kind="note", inputs=None, conn=None):
        return self.store.publish_artifact(logical_id=logical, artifact_type=kind, author=author,
            body=canonical_bytes(body), media_type="application/json", inputs=inputs, conn=conn)["artifact_ref"]

    def body(self, ref):
        return json.loads(self.store.read_body(self.store.get(ref)["body_hash"]))

    def model(self, reply, *, author="methods.reviewer", survey=None, task=True, prompt=None):
        self.serial += 1
        task_id = f"review-{self.serial}"
        if task:
            self.tasks.create(task_id, "review", {"operation": "model"}, author)
            self.tasks.admit(task_id, "command.controller")
            self.tasks.start_attempt(task_id, f"{task_id}-attempt", owner=author, lease_ttl_seconds=20)
        survey_ref = survey or getattr(self, "survey", None)
        request = {"survey_ref": survey_ref} if survey_ref else {}
        request.update(prompt or {})
        context = self.publish(f"command/contexts/{task_id}",
            {"client": {"model": "fixture"}, "prompt": json.dumps(request)}, author=author)
        execution = self.publish(f"command/executions/{task_id}", {
            "text": getattr(self, "reply_wrapper", lambda text: text)(json.dumps(reply)),
            "model": "fixture", "usage": {"model_calls": 1},
            "elapsed_seconds": 0.1, "finish_reason": "stop",
        }, author=author, kind="report", inputs=[{"ref": context, "purpose": "subject"}])
        if task:
            self.tasks.finish_attempt(f"{task_id}-attempt", "succeeded", usage={"model_calls": 1})
            self.tasks.transition(task_id, "awaiting_review", author)
        return execution

    def fetch(self, text, *, overrides=None):
        self.serial += 1
        task_id, author = f"capture-{self.serial}", "research.retriever"
        self.tasks.create(task_id, "retrieval", {"operation": "fetch"}, author)
        self.tasks.admit(task_id, "command.controller")
        self.tasks.start_attempt(task_id, f"{task_id}-attempt", owner=author, lease_ttl_seconds=20)
        context = self.publish(f"command/contexts/{task_id}", {"url": "https://example.org/known"}, author=author)
        raw = text.encode("utf-8")
        result = {"outcome": "ok", "text": text,
                  "metadata": {"representation": "extracted_text", "provider": "mcp-fetch", "transport": "mcp_stdio"},
                  "capture": {"encoding": "base64", "body": base64.b64encode(raw).decode(),
                              "bytes": len(raw), "sha256": sha256_hex(raw)}, "capture_sha256": sha256_hex(raw)}
        result.update(overrides or {})
        ref = self.publish(f"command/executions/{task_id}", result, author=author, kind="report",
                           inputs=[{"ref": context, "purpose": "subject"}])
        self.tasks.finish_attempt(f"{task_id}-attempt", "succeeded", usage={"retrieval_calls": 1})
        self.tasks.transition(task_id, "awaiting_review", author)
        return ref

    def review_for(self, survey, *, author="methods.reviewer", reply=None, task=True, context_survey=None):
        reply = reply or {"checks": checks(SURVEY_CHECKS), "rationale": "Search accounting and map claims have source support."}
        execution = self.model(reply, author=author, survey=context_survey or survey, task=task)
        return self.publish(f"kb/review-{self.serial}", {
            "survey_ref": survey, "execution_ref": execution, **reply,
        }, author=author)

    def work_review_for(self, entry, relationships=(), *, author="methods.work-reviewer", reply=None,
                        task=True, prompt=None, prompt_overrides=None, overrides=None):
        reply = reply or {"checks": checks(work_review_checks(relationships)),
                          "rationale": "Each field and outgoing relationship is supported by the captured sources."}
        execution = self.model(reply, author=author, task=task, prompt={
            **(self.work_prompt(entry, relationships) if prompt is None else prompt), **(prompt_overrides or {})})
        return self.publish(f"kb/work-review-{self.serial}", {"entry_ref": entry,
            "relationship_refs": list(relationships), "execution_ref": execution, **reply, **(overrides or {})}, author=author)

    def work_prompt(self, entry, relationships=()):
        body = self.body(entry)
        relations = [{**self.body(ref), "artifact_ref": ref} for ref in relationships]
        statements = [body[field] for field in WORK_CHECKS[2:]] + [relation["claim"] for relation in relations]
        refs = dict.fromkeys(proof["source_ref"] for statement in statements for proof in statement["evidence"])
        return {"entry_ref": entry, "entry": body, "relationship_refs": list(relationships),
                "relationships": relations, "sources": [self.source_context(ref) for ref in refs],
                "relationship_semantics": dict(RELATIONSHIP_SEMANTICS)}

    def source_context(self, ref, *, start=0, end=None):
        body = self.body(ref)
        text = body["text"]
        end = len(text) if end is None else end
        return {"source_ref": ref, "work_id": body["work_id"], "representation": body["representation"],
                "text": text[start:end], "available_chars": len(text), "window": {"start": start, "end": end}}

    @staticmethod
    def unknown_entry(work_id):
        return {"work_id": work_id, "inclusion": "uncertain", "reason": "Captured text does not establish the scoped findings.",
                **{field: {"text": None, "evidence": []} for field in WORK_CHECKS[2:]}}

    def survey_with_work_reviews(self, reviews, *, extra_dependencies=(), overrides=None):
        dependencies = [ref for ref in self.survey_body["dependency_refs"]
                        if ref not in (self.work_review, self.body(self.work_review)["execution_ref"])]
        for ref in reviews:
            dependencies.append(ref)
            execution = self.body(ref).get("execution_ref")
            if execution:
                dependencies.append(execution)
        dependencies.extend(extra_dependencies)
        self.survey = self.publish("kb/focused-survey", {**self.survey_body,
            "work_review_refs": reviews, "dependency_refs": dependencies, **(overrides or {})})
        self.review = self.review_for(self.survey)
        return self.survey

    def map_with_relationship(self, *, source="doi:10.1000/known", target_id="doi:10.1000/related"):
        work_id = "doi:10.1000/related"
        work = self.publish("kb/related-work", {"work_id": work_id, "title": "Earlier Treatment"})
        target = self.publish("kb/related-entry", self.unknown_entry(work_id))
        captured = self.publish("kb/related-source", {"work_id": work_id, "representation": "abstract",
            "text": "The earlier study introduces the treatment."}, kind="source_capture")
        relation = self.publish("kb/relationship", {"source": source, "target": target_id,
            "kind": "extends", "claim": {"text": "The treatment inherits the earlier study's method.",
                "evidence": [*self.entry_body["finding"]["evidence"], {"work_id": work_id,
                    "source_ref": captured, "quote": "The earlier study introduces the treatment."}]}})
        mapped = self.publish("kb/relationship-map", {"entry_refs": [self.entry, target],
                              "relationship_refs": [relation]})
        target_review = self.work_review_for(target)
        self.survey_body = {**self.survey_body, "work_refs": [*self.survey_body["work_refs"], work],
            "source_refs": [*self.survey_body["source_refs"], captured],
            "dependency_refs": [*self.survey_body["dependency_refs"], work, captured]}
        return mapped, relation, target_review, [mapped, relation, target]

    def survey_with_source(self, source):
        serial = self.serial
        body = self.body(self.entry)
        for field in WORK_CHECKS[2:]:
            body[field]["evidence"] = [{**proof, "source_ref": source} for proof in body[field]["evidence"]]
        entry = self.publish(f"kb/source-entry-{serial}", body)
        mapped = self.publish(f"kb/source-map-{serial}", {"entry_refs": [entry], "relationship_refs": []})
        review = self.work_review_for(entry)
        replaced = {self.source, self.capture, self.entry, self.map, self.work_review,
                    self.body(self.work_review)["execution_ref"]}
        dependencies = [ref for ref in self.survey_body["dependency_refs"] if ref not in replaced]
        dependencies.extend([source, self.body(source)["execution_ref"], entry, mapped, review,
                             self.body(review)["execution_ref"]])
        self.survey = self.publish(f"kb/source-survey-{serial}", {**self.survey_body, "map_ref": mapped,
            "source_refs": [source], "work_review_refs": [review], "dependency_refs": dependencies})
        self.review = self.review_for(self.survey)

    def accept(self):
        return self.gate.accept(self.survey, self.review, author="command.controller")

    def nominate(self, *, survey=None, statement="The treatment has not been studied in condition B.", conn=None):
        self.nomination = self.publish("kb/gap-nomination", {
            "survey_ref": survey or self.survey, "id": "treatment-gap", "statement": statement,
        }, author="research.gap-proposer", conn=conn)
        return self.nomination

    def assessment(self, *, state="eligible_for_experiment", evidence=None, author="methods.novelty-verifier", overrides=None,
                   nomination_ref=None, prompt_overrides=None):
        if nomination_ref is None:
            nomination = json.loads(self.store.read_body(self.store.get(self.nomination)["body_hash"])) if self.nomination else None
            if nomination is None or self.store.get(nomination["survey_ref"])["artifact_id"] != self.store.get(self.survey)["artifact_id"]:
                self.nominate()
            nomination_ref = self.nomination
        nomination = json.loads(self.store.read_body(self.store.get(nomination_ref)["body_hash"]))
        proof = evidence if evidence is not None else [{"work_id": "doi:10.1000/known",
            "source_ref": self.source, "quote": "The treatment also works in condition B."}]
        reply = {
            "state": state, "rationale": "The bounded comparison is supported by decisive source text.",
            "comparisons": [{"work_id": "doi:10.1000/known", "relationship": (
                "uncertain" if state == "insufficient_evidence" else "solves" if state == "refuted_by_prior_work" else "different"),
                "statement": "Different treatment domain", "evidence": proof}],
            "checks": checks(ASSESSMENT_CHECKS),
            "evidence": proof,
        }
        reply.update(overrides or {})
        execution = self.model(reply, author=author, prompt={"nomination_ref": nomination_ref,
            "gap": {key: nomination[key] for key in ("id", "statement")}, **(prompt_overrides or {})})
        return self.publish(f"strategy/assessment-{self.serial}", {
            "survey_ref": self.survey, "nomination_ref": nomination_ref, "execution_ref": execution, **reply,
        }, author=author)

    def commit(self, ref, **kwargs):
        return self.gate.commit_assessment(ref, survey_ref=self.survey, author="command.controller", **kwargs)

    def test_independent_acceptance_and_assessment_are_durable(self):
        self.assertEqual(self.accept()["artifact_ref"], self.survey)
        self.assertEqual(self.gate.require_current(self.survey)["artifact_ref"], self.survey)
        assessment = self.assessment()
        self.assertEqual(self.commit(assessment)["artifact_ref"], assessment)
        self.assertEqual(self.control.replay()[-1]["payload"]["survey_ref"], self.survey)
        self.assertEqual(self.control.replay()[-1]["event_type"], "assessment.accepted")
        self.assertTrue(self.control.verify_chain()[0])

    def test_fenced_review_replies_pass_exact_execution_validation(self):
        self.reply_wrapper = lambda text: f"```json\n{text}\n```"
        work_review = self.work_review_for(self.entry)
        self.survey_with_work_reviews([work_review])
        self.assertEqual(self.accept()["artifact_ref"], self.survey)
        assessment = self.assessment()
        self.assertEqual(self.commit(assessment)["artifact_ref"], assessment)

    def test_reasoning_wrapped_replies_pass_exact_execution_validation(self):
        self.reply_wrapper = lambda text: f"provider reasoning</think>```json\n{text}\n```"
        work_review = self.work_review_for(self.entry)
        self.survey_with_work_reviews([work_review])
        self.assertEqual(self.accept()["artifact_ref"], self.survey)

    def test_unaccepted_survey_cannot_authorize_assessment(self):
        assessment = self.assessment()
        with self.assertRaisesRegex(ValidationError, "accepted current survey"):
            self.commit(assessment)

    def test_plain_adoption_does_not_create_authoritative_acceptance(self):
        self.store.adopt("kb/survey", target_version=1, expected_accepted_version=None, actor="anyone")
        with self.assertRaisesRegex(ValidationError, "authoritative"):
            self.gate.require_current(self.survey)

    def test_new_governing_heads_invalidate_accepted_survey(self):
        for logical in ("kb/map", "kb/source", "kb/survey", "kb/work", "kb/query", "inputs/score"):
            with self.subTest(logical=logical):
                self.accept()
                with self.assertRaisesRegex(ConflictError, "newer published version"):
                    with self.control.tx() as conn:
                        self.publish(logical, {"new": "evidence"}, conn=conn)
                        self.gate.require_current(self.survey)
                self.gate.require_current(self.survey)
                # Undo only the acceptance to exercise the same fixture in another subcase.
                self.control._conn.execute("DELETE FROM accepted_heads WHERE logical_id='kb/survey'")

    def test_self_review_is_rejected(self):
        review = self.review_for(self.survey, author="research.mapper")
        with self.assertRaisesRegex(ValidationError, "independent"):
            self.gate.accept(self.survey, review, author="controller")

    def test_fabricated_execution_without_successful_task_is_rejected(self):
        review = self.review_for(self.survey, task=False)
        with self.assertRaisesRegex(ValidationError, "successful independent model attempt"):
            self.gate.accept(self.survey, review, author="controller")

    def test_review_dispatch_must_name_exact_survey(self):
        review = self.review_for(self.survey, context_survey="artifact:kb/survey@99")
        with self.assertRaisesRegex(ValidationError, "exact survey"):
            self.gate.accept(self.survey, review, author="controller")

    def test_review_cannot_rewrite_model_verdict(self):
        manifest = self.store.get(self.review)
        body = json.loads(self.store.read_body(manifest["body_hash"]))
        body["rationale"] = "Unsupported replacement rationale"
        forged = self.publish("kb/forged-review", body, author=manifest["author"])
        with self.assertRaisesRegex(ValidationError, "model reply"):
            self.gate.accept(self.survey, forged, author="controller")

    def test_posthoc_execution_cannot_reuse_a_successful_attempt(self):
        review = self.store.get(self.review)
        review_body = json.loads(self.store.read_body(review["body_hash"]))
        execution = self.store.get(review_body["execution_ref"])
        replacement = self.publish(execution["artifact_id"],
            json.loads(self.store.read_body(execution["body_hash"])),
            author=execution["author"], kind="report", inputs=execution["inputs"])
        forged = self.publish("kb/posthoc-review", {**review_body, "execution_ref": replacement}, author=review["author"])
        with self.assertRaisesRegex(ValidationError, "during its successful attempt"):
            self.gate.accept(self.survey, forged, author="controller")

    def test_missing_failed_and_duplicate_review_checks_rejected(self):
        candidates = [checks(SURVEY_CHECKS)[:-1], checks(SURVEY_CHECKS, "failed"), [checks(SURVEY_CHECKS)[0]] * 3]
        for values in candidates:
            with self.subTest(checks=values):
                review = self.review_for(self.survey, reply={"checks": values, "rationale": "Scoped verdict"})
                with self.assertRaises(ValidationError):
                    self.gate.accept(self.survey, review, author="controller")

    def test_focused_checks_cover_every_field_and_exact_relationship(self):
        refs = ["artifact:kb/relationship-a@2", "artifact:kb/relationship-b@1"]
        self.assertEqual(work_review_checks(refs), (*WORK_CHECKS, *(f"relationship:{ref}" for ref in refs)))
        self.assertEqual(work_review_checks([]), WORK_CHECKS)

    def test_focused_execution_precedes_the_bundle_and_is_hash_pinned(self):
        execution = self.store.get(self.body(self.work_review)["execution_ref"])
        context_ref = execution["inputs"][0]["ref"]
        prompt = json.loads(self.body(context_ref)["prompt"])
        self.assertNotIn("survey_ref", prompt)
        self.assertEqual(prompt, self.work_prompt(self.entry))
        self.accept()
        accepted = self.control.replay()[-1]["payload"]
        dependencies = {pin["ref"]: pin["body_hash"] for pin in accepted["dependency_pins"]}
        evidence = {pin["ref"]: pin["body_hash"] for pin in accepted["evidence_pins"]}
        for ref in (self.entry, self.work_review, execution["artifact_ref"]):
            self.assertEqual(dependencies[ref], self.store.get(ref)["body_hash"])
        self.assertEqual(evidence[context_ref], self.store.get(context_ref)["body_hash"])

    def test_focused_execution_must_include_actual_entry_relationship_and_source_content(self):
        for field in ("entry", "relationships", "sources"):
            with self.subTest(field=field):
                prompt = self.work_prompt(self.entry)
                del prompt[field]
                review = self.work_review_for(self.entry, prompt=prompt)
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "focused review"):
                    self.accept()

    def test_focused_execution_cannot_inspect_a_modified_entry_body(self):
        body = self.body(self.entry)
        body["finding"]["text"] = "The treatment has only been evaluated in condition A."
        review = self.work_review_for(self.entry, prompt_overrides={"entry": body})
        self.survey_with_work_reviews([review])
        with self.assertRaisesRegex(ValidationError, "exact entry and relationship bodies"):
            self.accept()

    def test_focused_execution_requires_the_shared_directed_relationship_semantics(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        for missing in (True, False):
            with self.subTest(missing=missing):
                prompt = self.work_prompt(self.entry, [relation])
                if missing:
                    del prompt["relationship_semantics"]
                else:
                    prompt["relationship_semantics"]["extends"] = "The target work builds on the source work."
                review = self.work_review_for(self.entry, [relation], prompt=prompt)
                self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies,
                                              overrides={"map_ref": mapped})
                with self.assertRaisesRegex(ValidationError, "shared relationship semantics"):
                    self.accept()

    def test_focused_execution_cannot_inspect_modified_relationship_bodies(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        original = {**self.body(relation), "artifact_ref": relation}
        for changes in ({"kind": "related"}, {"artifact_ref": "artifact:kb/relationship@99"}):
            with self.subTest(changes=changes):
                review = self.work_review_for(self.entry, [relation],
                    prompt_overrides={"relationships": [{**original, **changes}]})
                self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies,
                                              overrides={"map_ref": mapped})
                with self.assertRaisesRegex(ValidationError, "exact entry and relationship bodies"):
                    self.accept()

    def test_focused_execution_requires_the_captured_source_identity_and_representation(self):
        for changes in ({"work_id": "doi:10.1000/another"}, {"representation": "abstract"}):
            with self.subTest(changes=changes):
                source = {**self.source_context(self.source), **changes}
                review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "captured work and representation"):
                    self.accept()

    def test_focused_execution_requires_exact_captured_text_and_integer_window_bounds(self):
        size = len(self.source_body["text"])
        changes = [{"text": "Fabricated source support"}, {"available_chars": size + 1},
                   {"available_chars": float(size)}, {"available_chars": True},
                   *[{"window": window} for window in ({"start": -1, "end": size}, {"start": 0, "end": size + 1},
                       {"start": 5, "end": 4}, {"start": 0.0, "end": size}, {"start": False, "end": size},
                       {"start": 0, "end": float(size)}, {"start": 0}, {"start": 0, "end": size, "offset": 1})]]
        for change in changes:
            with self.subTest(change=change):
                source = {**self.source_context(self.source), **change}
                review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "exact captured text slice"):
                    self.accept()

    def test_focused_execution_requires_cited_sources_in_its_visible_context(self):
        review = self.work_review_for(self.entry, prompt_overrides={"sources": []})
        self.survey_with_work_reviews([review])
        with self.assertRaisesRegex(ValidationError, "correct visible source and work"):
            self.accept()

    def test_focused_execution_cannot_cite_text_outside_the_recorded_window(self):
        source = self.source_context(self.source, end=len("Known Treatment"))
        review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
        self.survey_with_work_reviews([review])
        with self.assertRaisesRegex(ValidationError, "quotation must be visible"):
            self.accept()

    def test_focused_execution_accepts_a_declared_prefix_with_all_cited_quotes(self):
        quote = self.entry_body["finding"]["evidence"][0]["quote"]
        end = self.source_body["text"].index(quote) + len(quote)
        source = self.source_context(self.source, end=end)
        self.assertLess(source["window"]["end"], source["available_chars"])
        review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
        self.survey_with_work_reviews([review])
        self.assertEqual(self.accept()["artifact_ref"], self.survey)

    def test_focused_execution_accepts_a_declared_nonprefix_source_window(self):
        quote = self.entry_body["finding"]["evidence"][0]["quote"]
        start = self.source_body["text"].index(quote)
        source = self.source_context(self.source, start=start, end=start + len(quote))
        review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
        self.survey_with_work_reviews([review])
        self.assertEqual(self.accept()["artifact_ref"], self.survey)

    def test_unknown_entry_statements_allow_an_empty_source_context(self):
        entry = self.publish("kb/unknown-entry", self.unknown_entry("doi:10.1000/known"))
        mapped = self.publish("kb/unknown-map", {"entry_refs": [entry], "relationship_refs": []})
        review = self.work_review_for(entry, prompt_overrides={"sources": []})
        self.survey_with_work_reviews([review], extra_dependencies=[entry, mapped], overrides={"map_ref": mapped})
        self.assertEqual(self.accept()["artifact_ref"], self.survey)

    def test_relationship_quotations_require_their_own_visible_source_windows(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        prompt = self.work_prompt(self.entry, [relation])
        for truncate in (False, True):
            with self.subTest(truncate=truncate):
                sources = [source for source in prompt["sources"] if source["work_id"] == "doi:10.1000/known"]
                if truncate:
                    other = next(source for source in prompt["sources"] if source["work_id"] == "doi:10.1000/related")
                    sources.append(self.source_context(other["source_ref"], end=5))
                review = self.work_review_for(self.entry, [relation], prompt_overrides={"sources": sources})
                self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies,
                                              overrides={"map_ref": mapped})
                with self.assertRaisesRegex(ValidationError, "visible"):
                    self.accept()

    def test_review_source_context_cannot_import_an_unpinned_capture(self):
        foreign = self.publish("kb/foreign-source", self.source_body, kind="source_capture")
        source = self.source_context(foreign)
        review = self.work_review_for(self.entry, prompt_overrides={"sources": [source]})
        self.survey_with_work_reviews([review], extra_dependencies=[foreign])
        with self.assertRaisesRegex(ValidationError, "pinned by the survey source_refs"):
            self.accept()

    def test_generic_note_cannot_impersonate_a_source_capture(self):
        source = self.publish("kb/non-capture-source", self.source_body)
        self.survey_with_source(source)
        with self.assertRaisesRegex(ValidationError, "captured work and representation"):
            self.accept()

    def test_focused_reviews_cannot_omit_registered_map_entries(self):
        empty = self.publish("kb/empty-map", {"entry_refs": [], "relationship_refs": []})
        self.survey_with_work_reviews([], extra_dependencies=[empty], overrides={"map_ref": empty})
        with self.assertRaisesRegex(ValidationError, "every registered work"):
            self.accept()
        additional = self.publish("kb/unmapped-work", {"work_id": "doi:10.1000/unmapped"})
        self.survey_with_work_reviews([self.work_review], extra_dependencies=[additional],
                                      overrides={"work_refs": [self.work, additional]})
        with self.assertRaisesRegex(ValidationError, "every registered work"):
            self.accept()

    def test_map_relationship_target_must_have_a_registered_entry(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship(target_id="doi:10.1000/unmapped")
        review = self.work_review_for(self.entry, [relation])
        self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies, overrides={"map_ref": mapped})
        with self.assertRaisesRegex(ValidationError, "valid target entry"):
            self.accept()

    def test_global_pass_cannot_replace_omitted_focused_work_review(self):
        self.survey_with_work_reviews([])
        self.assertTrue(all(row["outcome"] == "passed" for row in self.body(self.review)["checks"]))
        with self.assertRaisesRegex(ValidationError, "one focused work review for every map entry"):
            self.accept()
        missing = {key: value for key, value in self.survey_body.items() if key != "work_review_refs"}
        survey = self.publish("kb/missing-focused-reviews", missing)
        with self.assertRaisesRegex(ValidationError, "work_review_refs"):
            self.gate.accept(survey, self.review_for(survey), author="controller")

    def test_global_pass_cannot_override_a_failed_focused_field(self):
        for field in WORK_CHECKS:
            with self.subTest(field=field):
                values = checks(WORK_CHECKS)
                for row in values:
                    if row["check_id"] == field:
                        row.update(outcome="failed", result="The captured abstract does not support this assertion.")
                review = self.work_review_for(self.entry, reply={"checks": values, "rationale": "Field support is insufficient."})
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "focused work review did not pass"):
                    self.accept()

    def test_focused_review_requires_every_check_exactly_once(self):
        values = checks(WORK_CHECKS)
        candidates = [values[:-1], values + [values[0]], [values[0]] * len(values),
                      [{**row, "check_id": "unknown"} if index == 0 else row for index, row in enumerate(values)]]
        for invalid in candidates:
            with self.subTest(checks=invalid):
                review = self.work_review_for(self.entry, reply={"checks": invalid, "rationale": "Scoped review"})
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "focused work review"):
                    self.accept()

    def test_two_focused_reviews_cannot_substitute_for_one_review_per_entry(self):
        duplicate = self.work_review_for(self.entry)
        self.survey_with_work_reviews([self.work_review, duplicate])
        with self.assertRaisesRegex(ValidationError, "exactly one current map entry"):
            self.accept()

    def test_focused_review_rejects_self_review(self):
        review = self.work_review_for(self.entry, author=self.store.get(self.entry)["author"])
        self.survey_with_work_reviews([review])
        with self.assertRaisesRegex(ValidationError, "focused work review requires an independent reviewer"):
            self.accept()

    def test_outgoing_relationships_require_their_own_executed_checks(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        for outcome in ("failed", "passed"):
            with self.subTest(outcome=outcome):
                values = checks(work_review_checks([relation]))
                for row in values:
                    if row["check_id"] == f"relationship:{relation}":
                        row.update(outcome=outcome, result="Inspected both works for the claimed method inheritance.")
                review = self.work_review_for(self.entry, [relation], reply={"checks": values, "rationale": "Inheritance claim checked."})
                self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies,
                                              overrides={"map_ref": mapped})
                if outcome == "failed":
                    with self.assertRaisesRegex(ValidationError, "focused work review did not pass"):
                        self.accept()
                else:
                    self.assertEqual(self.accept()["artifact_ref"], self.survey)

    def test_focused_relationship_coverage_must_match_exact_outgoing_map_refs(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        for relationships in ([], ["artifact:kb/relationship@99"]):
            with self.subTest(relationships=relationships):
                review = self.work_review_for(self.entry, [relation], overrides={"relationship_refs": relationships})
                self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies,
                                              overrides={"map_ref": mapped})
                with self.assertRaisesRegex(ValidationError, "every exact outgoing relationship"):
                    self.accept()
        incoming_review = self.work_review_for(self.body(target_review)["entry_ref"], [relation])
        review = self.work_review_for(self.entry, [relation])
        self.survey_with_work_reviews([review, incoming_review], extra_dependencies=dependencies, overrides={"map_ref": mapped})
        with self.assertRaisesRegex(ValidationError, "every exact outgoing relationship"):
            self.accept()

    def test_relationship_cannot_escape_review_through_an_unknown_source(self):
        mapped, _, target_review, dependencies = self.map_with_relationship(source="doi:10.1000/unmapped")
        self.survey_with_work_reviews([self.work_review, target_review], extra_dependencies=dependencies,
                                      overrides={"map_ref": mapped})
        with self.assertRaisesRegex(ValidationError, "valid source entry"):
            self.accept()

    def test_focused_dispatch_must_bind_exact_entry_and_relationships(self):
        for prompt in ({"entry_ref": "artifact:kb/entry@99"}, {"relationship_refs": [self.entry]}):
            with self.subTest(prompt=prompt):
                review = self.work_review_for(self.entry, prompt_overrides=prompt)
                self.survey_with_work_reviews([review])
                with self.assertRaisesRegex(ValidationError, "exact entry and relationships"):
                    self.accept()

    def test_focused_review_cannot_rewrite_the_completed_model_response(self):
        for field, replacement in (("rationale", "Invented field support"), ("checks", checks(WORK_CHECKS, "failed"))):
            with self.subTest(field=field):
                original = self.body(self.work_review)
                if field == "checks":
                    execution = self.model({"checks": replacement, "rationale": original["rationale"]},
                        author="methods.work-reviewer", prompt=self.work_prompt(self.entry))
                    forged_body = {**original, "execution_ref": execution}
                else:
                    forged_body = {**original, field: replacement}
                forged = self.publish(f"kb/forged-focused-{field}", forged_body, author="methods.work-reviewer")
                self.survey_with_work_reviews([forged])
                with self.assertRaisesRegex(ValidationError, "focused work review does not match the completed model reply"):
                    self.accept()

    def test_focused_review_requires_a_successful_recorded_execution(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                review = self.work_review_for(self.entry, task=False, overrides={"execution_ref": None} if missing else None)
                self.survey_with_work_reviews([review])
                with self.assertRaises(ValidationError):
                    self.accept()

    def test_focused_posthoc_execution_cannot_reuse_a_successful_attempt(self):
        original = self.body(self.work_review)
        execution = self.store.get(original["execution_ref"])
        replacement = self.publish(execution["artifact_id"], self.body(execution["artifact_ref"]),
            author=execution["author"], kind="report", inputs=execution["inputs"])
        forged = self.publish("kb/posthoc-focused-review", {**original, "execution_ref": replacement},
                              author=execution["author"])
        self.survey_with_work_reviews([forged])
        with self.assertRaisesRegex(ValidationError, "during its successful attempt"):
            self.accept()

    def test_focused_review_and_execution_must_be_pinned_dependencies(self):
        for omitted in (self.work_review, self.body(self.work_review)["execution_ref"], self.entry):
            with self.subTest(omitted=omitted):
                self.survey_with_work_reviews([self.work_review], overrides={"dependency_refs": [
                    ref for ref in self.survey_body["dependency_refs"] if ref != omitted]})
                with self.assertRaisesRegex(ValidationError, "dependencies must pin"):
                    self.accept()

    def test_focused_review_entry_execution_and_context_heads_revoke_acceptance(self):
        review = self.store.get(self.work_review)
        execution = self.store.get(self.body(self.work_review)["execution_ref"])
        context = self.store.get(execution["inputs"][0]["ref"])
        for manifest in (self.store.get(self.entry), review, execution, context):
            with self.subTest(ref=manifest["artifact_ref"]):
                self.accept()
                with self.assertRaisesRegex(ConflictError, "newer published version"):
                    with self.control.tx() as conn:
                        self.publish(manifest["artifact_id"], self.body(manifest["artifact_ref"]),
                            author=manifest["author"], kind=manifest["artifact_type"], inputs=manifest["inputs"], conn=conn)
                        self.gate.require_current(self.survey)
                self.gate.require_current(self.survey)
                self.control._conn.execute("DELETE FROM accepted_heads WHERE logical_id='kb/survey'")

    def test_stale_relationship_review_cannot_authorize_updated_map_relationship(self):
        mapped, relation, target_review, dependencies = self.map_with_relationship()
        review = self.work_review_for(self.entry, [relation])
        self.survey_with_work_reviews([review, target_review], extra_dependencies=dependencies, overrides={"map_ref": mapped})
        self.accept()
        self.publish("kb/relationship", {**self.body(relation), "claim": "A stronger inheritance claim"})
        with self.assertRaisesRegex(ConflictError, "newer published version"):
            self.gate.require_current(self.survey)

    def test_dependency_omissions_and_duplicates_rejected(self):
        for dependencies in (self.survey_body["dependency_refs"][:-2], self.survey_body["dependency_refs"] * 2):
            with self.subTest(dependencies=dependencies):
                survey = self.publish("kb/invalid-survey", {**self.survey_body, "dependency_refs": dependencies})
                review = self.review_for(survey)
                with self.assertRaises(ValidationError):
                    self.gate.accept(survey, review, author="controller")

    def test_body_corruption_revokes_acceptance(self):
        self.accept()
        source = self.store.get(self.source)
        Path(self.store.objects_dir, source["body_hash"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValidationError, "body integrity"):
            self.gate.require_current(self.survey)

    def test_manifest_corruption_is_rejected(self):
        self.control._conn.execute("UPDATE artifacts SET manifest_hash='forged' WHERE artifact_ref=?", (self.map,))
        with self.assertRaisesRegex(ValidationError, "manifest integrity"):
            self.accept()

    def test_cross_store_gate_is_rejected(self):
        other = ControlStore(self.directory.name)
        try:
            with self.assertRaisesRegex(ValidationError, "control connection"):
                SurveyGate(other, self.store)
        finally:
            other.close()

    def test_acceptance_uses_exact_head_cas(self):
        self.accept()
        with self.assertRaises(ConflictError):
            self.accept()
        self.gate.accept(self.survey, self.review, author="controller", expected_version=1)

    def test_deadline_after_validation_and_after_mutation_rolls_back_acceptance(self):
        for fail_at in (2, 3, 4):
            with self.subTest(fail_at=fail_at):
                calls = []
                before = self.control.trusted_head()
                def guard():
                    calls.append(None)
                    if len(calls) == fail_at:
                        raise ValidationError("run deadline reached")
                with self.assertRaisesRegex(ValidationError, "deadline"):
                    self.gate.accept(self.survey, self.review, author="controller", guard=guard)
                self.assertIsNone(self.store.accepted("kb/survey"))
                self.assertEqual(self.control.trusted_head(), before)

    def test_deadline_after_evidence_validation_and_mutation_rolls_back_assessment(self):
        self.accept()
        ref = self.assessment()
        for fail_at in (2, 3, 4):
            with self.subTest(fail_at=fail_at):
                calls = []
                before = self.control.trusted_head()
                def guard():
                    calls.append(None)
                    if len(calls) == fail_at:
                        raise ValidationError("run deadline reached")
                with self.assertRaisesRegex(ValidationError, "deadline"):
                    self.commit(ref, guard=guard)
                self.assertIsNone(self.store.accepted(self.store.get(ref)["artifact_id"]))
                self.assertEqual(self.control.trusted_head(), before)

    def test_governing_change_during_assessment_adoption_rolls_back(self):
        self.accept()
        ref = self.assessment()
        original = self.store.adopt
        def adopt(*args, **kwargs):
            result = original(*args, **kwargs)
            self.publish("kb/map", {"relations": ["new contradictory finding"]}, conn=kwargs["conn"])
            return result
        with patch.object(self.store, "adopt", side_effect=adopt):
            with self.assertRaises(ConflictError):
                self.commit(ref)
        self.assertEqual(self.store.versions("kb/map"), [1])
        self.assertIsNone(self.store.accepted(self.store.get(ref)["artifact_id"]))

    def test_insufficient_evidence_can_abstain_without_full_text(self):
        self.accept()
        ref = self.assessment(state="insufficient_evidence", evidence=[], overrides={"checks": checks(ASSESSMENT_CHECKS, "failed")})
        self.assertEqual(self.commit(ref)["artifact_ref"], ref)

    def test_decisive_states_require_full_text_quotes(self):
        self.accept()
        for state in ("eligible_for_experiment", "refuted_by_prior_work"):
            for evidence in ([], [{"work_id": "doi:10.1000/known", "source_ref": self.source, "quote": "not in source"}]):
                with self.subTest(state=state, evidence=evidence):
                    with self.assertRaises(ValidationError):
                        self.commit(self.assessment(state=state, evidence=evidence))

    def test_abstracts_and_unverified_identities_never_authorize_decisive_states(self):
        variants = [{"representation": "abstract"}, {"identity_verified": False},
                    {"identity_checks": {"title_match": True, "section_markers": ["Missing appendix"]}}]
        for index, change in enumerate(variants):
            with self.subTest(change=change):
                source = self.publish(f"kb/invalid-source-{index}", {**self.source_body, **change}, kind="source_capture")
                self.survey_with_source(source)
                self.accept()
                evidence = [{"work_id": "doi:10.1000/known", "source_ref": source,
                             "quote": "The treatment also works in condition B."}]
                for state in ("eligible_for_experiment", "refuted_by_prior_work"):
                    with self.assertRaises(ValidationError):
                        self.commit(self.assessment(state=state, evidence=evidence))

    def test_assessment_requires_independent_recorded_response(self):
        self.accept()
        with self.assertRaisesRegex(ValidationError, "independent reviewer"):
            self.commit(self.assessment(author="research.mapper"))
        ref = self.assessment()
        manifest = self.store.get(ref)
        body = json.loads(self.store.read_body(manifest["body_hash"]))
        body["rationale"] = "Invented approval"
        forged = self.publish("strategy/forged-assessment", body, author=manifest["author"])
        with self.assertRaisesRegex(ValidationError, "recorded model reply"):
            self.commit(forged)

    def test_nomination_author_cannot_assess_their_own_gap(self):
        self.accept()
        nomination = self.nominate()
        proposer = self.store.get(nomination)["author"]
        self.assertNotEqual(proposer, self.store.get(self.survey)["author"])
        ref = self.assessment(author=proposer, nomination_ref=nomination)
        with self.assertRaisesRegex(ValidationError, "independent of the nomination author"):
            self.commit(ref)
        self.assertIsNone(self.store.accepted(self.store.get(ref)["artifact_id"]))

    def test_full_text_requires_matching_complete_fetch_capture(self):
        metadata = {"representation": "extracted_text", "provider": "mcp-fetch", "transport": "mcp_stdio"}
        variants = [
            ({"outcome": "partial"}, {}),
            ({"metadata": {**metadata, "capture_truncated": True}}, {}),
            ({"capture_sha256": "forged"}, {}),
            ({}, {"text": self.source_body["text"] + " Unsupported additional claim."}),
            ({}, {"url": "https://example.org/different-work"}),
        ]
        for index, (capture_changes, source_changes) in enumerate(variants):
            with self.subTest(capture=capture_changes, source=source_changes):
                execution = self.fetch(self.source_body["text"], overrides=capture_changes)
                source = self.publish(f"kb/unverified-capture-{index}",
                    {**self.source_body, "execution_ref": execution, **source_changes}, kind="source_capture")
                self.survey_with_source(source)
                self.accept()
                evidence = [{"work_id": "doi:10.1000/known", "source_ref": source,
                             "quote": "The treatment also works in condition B."}]
                with self.assertRaisesRegex(ValidationError, "full.text"):
                    self.commit(self.assessment(evidence=evidence))

    def test_decisive_evidence_cannot_import_an_unpinned_source_or_work(self):
        self.accept()
        for field, value in (("source_ref", self.work), ("work_id", "doi:10.1000/foreign")):
            evidence = {"work_id": "doi:10.1000/known", "source_ref": self.source,
                        "quote": "The treatment also works in condition B.", field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "pinned"):
                self.commit(self.assessment(evidence=[evidence]))

    def test_decisive_assessment_cannot_ignore_failed_check(self):
        self.accept()
        with self.assertRaisesRegex(ValidationError, "every check to pass"):
            self.commit(self.assessment(overrides={"checks": checks(ASSESSMENT_CHECKS, "failed")}))

    def second_work_survey(self, *, full_text):
        work_id, title = "doi:10.1000/second", "Second Treatment"
        work = self.publish("kb/second-work", {"work_id": work_id, "title": title})
        text = "Second Treatment\nMethods\nThe second study establishes the proposed effect.\nResults\nSuccessful replication."
        execution = self.fetch(text)
        source = self.publish("kb/second-source", {**self.source_body, "work_id": work_id,
            "text": text, "execution_ref": execution, "representation": "full_text" if full_text else "abstract"}, kind="source_capture")
        proof = {"work_id": work_id, "source_ref": source, "quote": "The second study establishes the proposed effect."}
        entry = self.publish("kb/second-entry", {**self.unknown_entry(work_id),
            "finding": {"text": proof["quote"], "evidence": [proof]}})
        mapped = self.publish("kb/two-work-map", {"entry_refs": [self.entry, entry], "relationship_refs": []})
        review = self.work_review_for(entry)
        self.survey = self.publish("kb/two-work-survey", {**self.survey_body,
            "work_refs": [self.work, work], "source_refs": [self.source, source], "map_ref": mapped,
            "work_review_refs": [self.work_review, review], "dependency_refs": [*self.survey_body["dependency_refs"],
                work, source, execution, entry, mapped, review, self.body(review)["execution_ref"]]})
        self.review = self.review_for(self.survey)
        self.accept()
        return proof

    def test_unrelated_full_text_cannot_authorize_an_abstract_only_comparison(self):
        proof = self.second_work_survey(full_text=False)
        for state, relationship in (("refuted_by_prior_work", "solves"), ("eligible_for_experiment", "different")):
            with self.subTest(state=state), self.assertRaisesRegex(ValidationError, "identity-verified full text"):
                self.commit(self.assessment(state=state, overrides={"comparisons": [{
                    "work_id": proof["work_id"], "relationship": relationship,
                    "statement": "The second study establishes the proposed effect.", "evidence": [proof],
                }]}))

    def test_decisive_comparison_cannot_borrow_another_works_proof(self):
        proof = self.second_work_survey(full_text=True)
        borrowed = {"work_id": "doi:10.1000/known", "source_ref": self.source,
                    "quote": "The treatment also works in condition B."}
        for evidence in ([], [borrowed]):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValidationError, "own work"):
                self.commit(self.assessment(state="refuted_by_prior_work", overrides={"comparisons": [{
                    "work_id": proof["work_id"], "relationship": "solves",
                    "statement": "The second study establishes the proposed effect.", "evidence": evidence,
                }]}))

    def test_both_decisive_states_accept_comparison_bound_full_text(self):
        proof = self.second_work_survey(full_text=True)
        for state, relationship in (("refuted_by_prior_work", "solves"), ("eligible_for_experiment", "different")):
            with self.subTest(state=state):
                assessment = self.assessment(state=state, overrides={"comparisons": [{
                    "work_id": proof["work_id"], "relationship": relationship,
                    "statement": "The second study establishes the proposed effect.", "evidence": [proof],
                }]})
                self.assertEqual(self.commit(assessment)["artifact_ref"], assessment)

    def test_decisive_comparison_can_retain_supplemental_abstract_evidence(self):
        proof = self.second_work_survey(full_text=True)
        abstract = self.publish("kb/second-abstract", {"work_id": proof["work_id"], "representation": "abstract",
            "text": "The second study establishes the proposed effect.", "identity_verified": False}, kind="source_capture")
        survey = json.loads(self.store.read_body(self.store.get(self.survey)["body_hash"]))
        self.survey = self.publish("kb/mixed-evidence-survey", {**survey,
            "source_refs": [*survey["source_refs"], abstract], "dependency_refs": [*survey["dependency_refs"], abstract]})
        self.review = self.review_for(self.survey)
        self.accept()
        mixed = [proof, {**proof, "source_ref": abstract}]
        assessment = self.assessment(state="refuted_by_prior_work", evidence=mixed, overrides={"comparisons": [{
            "work_id": proof["work_id"], "relationship": "solves",
            "statement": "The second study establishes the proposed effect.", "evidence": mixed,
        }]})
        self.assertEqual(self.commit(assessment)["artifact_ref"], assessment)

    def test_insufficient_assessment_accepts_runtime_abstention_outcomes(self):
        self.accept()
        for outcome in ("insufficient_evidence", "check_failed"):
            with self.subTest(outcome=outcome):
                assessment = self.assessment(state="insufficient_evidence", evidence=[],
                    overrides={"checks": checks(ASSESSMENT_CHECKS, outcome)})
                self.assertEqual(self.commit(assessment)["artifact_ref"], assessment)

    def test_assessment_requires_an_exact_nomination_reference(self):
        self.accept()
        ref = self.assessment()
        manifest = self.store.get(ref)
        body = json.loads(self.store.read_body(manifest["body_hash"]))
        del body["nomination_ref"]
        missing = self.publish("strategy/missing-nomination", body, author=manifest["author"])
        with self.assertRaisesRegex(ValidationError, "nomination_ref"):
            self.commit(missing)

    def test_changed_nomination_between_model_and_commit_rejects_the_old_assessment(self):
        self.accept()
        ref = self.assessment()
        self.nominate(statement="The treatment is ineffective in a different clinical population.")
        with self.assertRaisesRegex(ConflictError, "newer published version"):
            self.commit(ref)
        self.assertIsNone(self.store.accepted(self.store.get(ref)["artifact_id"]))

    def test_changed_nomination_revokes_an_accepted_assessment(self):
        self.accept()
        ref = self.assessment()
        self.commit(ref)
        self.assertEqual(self.gate.require_current_assessment(ref)["artifact_ref"], ref)
        self.nominate(statement="The treatment is ineffective in a different clinical population.")
        with self.assertRaisesRegex(ConflictError, "newer published version"):
            self.gate.require_current_assessment(ref)

    def test_assessment_prompt_must_bind_exact_nomination_and_gap(self):
        self.accept()
        self.nominate()
        variants = [
            {"nomination_ref": "artifact:kb/gap-nomination@99"},
            {"gap": {"id": "treatment-gap", "statement": "A different research hypothesis."}},
            {"gap": {"id": "another-gap", "statement": "The treatment has not been studied in condition B."}},
        ]
        for prompt in variants:
            with self.subTest(prompt=prompt), self.assertRaisesRegex(ValidationError, "exact nomination and gap"):
                self.commit(self.assessment(prompt_overrides=prompt))

    def test_nomination_can_keep_an_earlier_accepted_version_of_the_same_survey(self):
        self.accept()
        old_survey, nomination = self.survey, self.nominate()
        self.survey = self.publish("kb/survey", self.survey_body)
        self.review = self.review_for(self.survey)
        self.gate.accept(self.survey, self.review, author="command.controller", expected_version=1)
        with self.assertRaises(ConflictError):
            self.gate.require_current(old_survey)
        self.assertEqual(self.gate.require_current_nomination(nomination)["survey_ref"], old_survey)
        ref = self.assessment(nomination_ref=nomination)
        self.commit(ref)
        self.assertEqual(self.gate.require_current_assessment(ref)["artifact_ref"], ref)

    def test_nomination_from_an_unrelated_accepted_survey_cannot_authorize_assessment(self):
        self.accept()
        nomination = self.nominate()
        self.survey = self.publish("kb/unrelated-survey", self.survey_body)
        self.review = self.review_for(self.survey)
        self.accept()
        with self.assertRaisesRegex(ValidationError, "same logical survey"):
            self.commit(self.assessment(nomination_ref=nomination))

    def test_nomination_requires_authoritative_historical_survey_acceptance(self):
        self.store.adopt("kb/survey", target_version=1, expected_accepted_version=None, actor="controller")
        with self.assertRaisesRegex(ValidationError, "authoritative survey.accepted"):
            self.gate.require_current_nomination(self.nominate())

    def test_plain_assessment_adoption_cannot_create_authorization(self):
        self.accept()
        ref = self.assessment()
        manifest = self.store.get(ref)
        self.store.adopt(manifest["artifact_id"], target_version=manifest["version"],
                         expected_accepted_version=None, actor="controller")
        with self.assertRaisesRegex(ValidationError, "authoritative assessment.accepted"):
            self.gate.require_current_assessment(ref)

    def test_nomination_change_during_assessment_adoption_rolls_back(self):
        self.accept()
        ref = self.assessment()
        original = self.store.adopt
        def adopt(*args, **kwargs):
            result = original(*args, **kwargs)
            self.nominate(statement="A different question published during decision integration.", conn=kwargs["conn"])
            return result
        before = self.control.trusted_head()
        with patch.object(self.store, "adopt", side_effect=adopt), self.assertRaises(ConflictError):
            self.commit(ref)
        self.assertEqual(self.store.versions("kb/gap-nomination"), [1])
        self.assertIsNone(self.store.accepted(self.store.get(ref)["artifact_id"]))
        self.assertEqual(self.control.trusted_head(), before)

    def test_new_source_evidence_revokes_current_assessment_authorization(self):
        self.accept()
        ref = self.assessment()
        self.commit(ref)
        self.publish("kb/source", {**self.source_body, "text": "New contradictory observations"})
        with self.assertRaises(ConflictError):
            self.gate.require_current_assessment(ref)


if __name__ == "__main__":
    unittest.main()
