#!/usr/bin/env python3
"""Build a deterministic control-plane validation paper and render it to PDF."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scisaurus.core.documents import Documents
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.core.surveys import ASSESSMENT_CHECKS, SURVEY_CHECKS, WORK_CHECKS, SurveyGate
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.paper import PaperReleaseBuilder


def publish(store, logical, body, *, author="research.mapper", kind="note", inputs=None):
    return store.publish_artifact(logical_id=logical, artifact_type=kind, author=author,
        body=canonical_bytes(body), media_type="application/json", inputs=inputs)["artifact_ref"]


def checks(names):
    return [{"check_id": name, "outcome": "passed", "method": "Inspect pinned validation evidence",
             "result": "The bounded demonstration contract is satisfied"} for name in sorted(names)]


def recorded_model(control, store, serial, author, reply, prompt):
    tasks = TaskManager(control)
    task_id = f"review-{serial}"
    tasks.create(task_id, "review", {"operation": "model"}, author)
    tasks.admit(task_id, "command.controller")
    tasks.start_attempt(task_id, task_id + "-attempt", owner=author, lease_ttl_seconds=60)
    context = publish(store, f"command/contexts/{task_id}",
        {"client": {"model": "deterministic-validation"}, "prompt": json.dumps(prompt)}, author=author)
    execution = publish(store, f"command/executions/{task_id}", {
        "text": json.dumps(reply), "model": "deterministic-validation", "usage": {"model_calls": 1},
        "elapsed_seconds": 0.01, "finish_reason": "stop"}, author=author, kind="report",
        inputs=[{"ref": context, "purpose": "subject"}])
    tasks.finish_attempt(task_id + "-attempt", "succeeded", usage={"model_calls": 1})
    tasks.transition(task_id, "awaiting_review", author)
    return execution


def make_survey(path):
    control = ControlStore(path); store = ArtifactStore(control); store.init_project(principal_note="demo-survey")
    tasks = TaskManager(control); gate = SurveyGate(control, store)
    score = publish(store, "inputs/score", {"question": "Can the release controls retain exact evidence?"})
    protocol = publish(store, "kb/protocol", {"scope": "deterministic control validation"})
    coverage = publish(store, "kb/coverage", {"queries": 1, "scope": "demonstration only"})
    query = publish(store, "kb/query", {"query": "evidence-bound release controls"})
    work = publish(store, "kb/work", {"work_id": "demo:known", "title": "Known Validation Record"})
    text = "Known Validation Record\nMethods\nThe control retains exact evidence.\nResults\nThe bounded check passed."
    tasks.create("capture-1", "retrieval", {"operation": "fetch"}, "research.retriever")
    tasks.admit("capture-1", "command.controller")
    tasks.start_attempt("capture-1", "capture-1-attempt", owner="research.retriever", lease_ttl_seconds=60)
    context = publish(store, "command/contexts/capture-1", {"url": "https://example.org/validation"},
                      author="research.retriever")
    raw = text.encode()
    fetch = publish(store, "command/executions/capture-1", {"outcome": "ok", "text": text,
        "metadata": {"representation": "extracted_text", "provider": "mcp-fetch", "transport": "mcp_stdio"},
        "capture": {"encoding": "base64", "body": base64.b64encode(raw).decode(), "bytes": len(raw),
                    "sha256": sha256_hex(raw)}, "capture_sha256": sha256_hex(raw)},
        author="research.retriever", kind="report", inputs=[{"ref": context, "purpose": "subject"}])
    tasks.finish_attempt("capture-1-attempt", "succeeded", usage={"retrieval_calls": 1})
    tasks.transition("capture-1", "awaiting_review", "research.retriever")
    source_body = {"representation": "full_text", "work_id": "demo:known", "text": text,
        "identity_verified": True, "identity_checks": {"title_match": True, "section_markers": ["Methods", "Results"]},
        "execution_ref": fetch, "url": "https://example.org/validation"}
    source = publish(store, "kb/source", source_body, author="methods.source-verifier", kind="source_capture")
    proof = {"work_id": "demo:known", "source_ref": source, "quote": "The control retains exact evidence."}
    entry_body = {"work_id": "demo:known", "inclusion": "included", "reason": "The record tests evidence retention.",
        "problem": {"text": None, "evidence": []}, "approach": {"text": None, "evidence": []},
        "finding": {"text": proof["quote"], "evidence": [proof]}, "limitations": {"text": None, "evidence": []}}
    entry = publish(store, "kb/entry", entry_body)
    mapped = publish(store, "kb/map", {"entry_refs": [entry], "relationship_refs": []})
    work_reply = {"checks": checks(WORK_CHECKS), "rationale": "Each mapped field is checked against the source."}
    source_context = {"source_ref": source, "work_id": "demo:known", "representation": "full_text",
                      "text": text, "available_chars": len(text), "window": {"start": 0, "end": len(text)}}
    work_execution = recorded_model(control, store, 2, "methods.work-reviewer", work_reply,
        {"entry_ref": entry, "entry": entry_body, "relationship_refs": [], "relationships": [],
         "sources": [source_context], "relationship_semantics": {
             "extends": "The source work builds on or extends the target work; the direction is source to target, never the inverse.",
             "contradicts": "The source work reports findings incompatible with the target work under comparable scope.",
             "compares": "An analyst comparison supported by evidence from both works; it does not assert that the source explicitly cites the target.",
             "related": "A supported topical connection between the works without a claim of inheritance."}})
    work_review = publish(store, "kb/work-review", {"entry_ref": entry, "relationship_refs": [],
        "execution_ref": work_execution, **work_reply}, author="methods.work-reviewer")
    dependencies = [score, protocol, mapped, coverage, source, work, query, fetch, entry, work_review, work_execution]
    survey_body = {"schema_version": "literature-survey-2", "score_ref": score, "protocol_ref": protocol,
        "map_ref": mapped, "coverage_ref": coverage, "source_refs": [source], "work_refs": [work],
        "query_refs": [query], "work_review_refs": [work_review], "dependency_refs": dependencies}
    survey = publish(store, "kb/survey", survey_body)
    survey_reply = {"checks": checks(SURVEY_CHECKS), "rationale": "The demonstration survey is internally pinned."}
    survey_execution = recorded_model(control, store, 3, "methods.reviewer", survey_reply, {"survey_ref": survey})
    review = publish(store, "kb/survey-review", {"survey_ref": survey, "execution_ref": survey_execution,
        **survey_reply}, author="methods.reviewer")
    gate.accept(survey, review, author="strategy.survey-integrator")
    nomination = publish(store, "kb/gap-nomination", {"survey_ref": survey, "id": "validation-gap",
        "statement": "The control requires a complete release-path validation."}, author="research.gap-proposer")
    assessment_reply = {"state": "eligible_for_experiment",
        "rationale": "The demonstration authorizes only the configured release-path validation.",
        "comparisons": [{"work_id": "demo:known", "relationship": "different",
                         "statement": "The retained source does not execute the release path.", "evidence": [proof]}],
        "checks": checks(ASSESSMENT_CHECKS), "evidence": [proof]}
    assessment_execution = recorded_model(control, store, 4, "methods.novelty-verifier", assessment_reply,
        {"survey_ref": survey, "nomination_ref": nomination,
         "gap": {"id": "validation-gap", "statement": "The control requires a complete release-path validation."}})
    assessment = publish(store, "strategy/assessment", {"survey_ref": survey, "nomination_ref": nomination,
        "execution_ref": assessment_execution, **assessment_reply}, author="methods.novelty-verifier")
    gate.commit_assessment(assessment, survey_ref=survey, author="strategy.survey-integrator")
    control.close()
    return {"survey_ref": survey, "assessment_ref": assessment, "source_ref": source,
            "quote": proof["quote"]}


def make_manuscript(path, quote):
    control = ControlStore(path); store = ArtifactStore(control); store.init_project(principal_note="demo-paper")
    docs = Documents(control, store)
    score = publish(store, "command/scores/paper", {"id": "paper-release-demo"}, author="principal")
    verification = publish(store, "methods/verifications/paper", {"outcome": "passed",
        "scope": "exact assembled validation report"}, author="methods.reviewer", kind="verification")
    specification = [
        ("introduction", "Introduction", [("prior", f"{quote} [[cite:known]]")]),
        ("methods", "Methods", [("method", "Execute all declared deterministic release checks.")]),
        ("results", "Results", [("metric", "The validation run produced 100% contract-check coverage under the declared fixture conditions. The configured release path completed.")]),
        ("limitations", "Limitations", [("limits", "The deterministic demonstration is not evidence of scientific novelty or external validity.")]),
    ]
    groups = []
    for group_id, title, units in specification:
        heading = docs.publish_unit(logical_id=f"strategy/units/{group_id}", kind="heading", text=title, author="principal")
        children = []
        for unit_id, text in units:
            unit = docs.publish_unit(logical_id=f"strategy/units/{unit_id}", kind="paragraph", text=text,
                                     author="strategy.writer")
            children.append({"ref": unit["artifact_ref"], "children": []})
        groups.append({"ref": heading["artifact_ref"], "children": children})
    manifest = docs.publish_manifest(document_id="strategy/documents/paper", tree={"units": groups},
                                     author="strategy.integrator")
    store.adopt(manifest["artifact_id"], target_version=manifest["version"], expected_accepted_version=None,
                actor="strategy.integrator")
    publish(store, "command/results/final", {"status": "accepted", "incumbent_ref": manifest["artifact_ref"],
        "score_ref": score, "candidates": [{"candidate_ref": manifest["artifact_ref"],
                                              "verification_ref": verification}]},
        author="command.controller", kind="report")
    control.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    parser.add_argument("--compile-script", default=(
        "/Users/seungyeop/.codex/plugins/cache/openai-bundled/latex/0.2.6/scripts/compile_latex.py"))
    args = parser.parse_args()
    root = Path(args.output_dir).resolve()
    if root.exists():
        raise SystemExit("output directory already exists")
    evidence = root.parent / (root.name + "-evidence")
    if evidence.exists():
        raise SystemExit("evidence directory already exists")
    evidence.mkdir(parents=True)
    survey = make_survey(evidence / "survey")
    make_manuscript(evidence / "manuscript", survey["quote"])
    results = {"schema_version": "results-package-1", "id": "release_validation", "revision": 1,
        "procedures": [{"id": "contract_check", "description": "Execute all declared deterministic release checks.",
                        "source": "scripts/build-paper-pipeline-demo.py"}],
        "metrics": [{"id": "coverage", "value": 100, "unit": "percent", "conditions": "declared fixture conditions",
                     "source": "paper pipeline validation", "presentation": "100% contract-check coverage"}],
        "findings": [{"id": "release_path", "statement": "The configured release path completed.",
                      "metric_ids": ["coverage"]}],
        "limitations": ["The deterministic demonstration is not evidence of scientific novelty or external validity."],
        "assets": []}
    results_path = evidence / "results.json"; results_path.write_bytes(canonical_bytes(results))
    config = {"schema_version": "paper-release-score-1", "paper_id": "pipeline_validation",
        "title": "Sci-saurus Evidence-Bound Release Pipeline Validation", "revision": 1,
        "document_type": "validation_report", "manuscript_project_dir": str(evidence / "manuscript"),
        "survey_project_dir": str(evidence / "survey"), "survey_ref": survey["survey_ref"],
        "assessment_ref": survey["assessment_ref"], "results_package": str(results_path),
        "evidence": [{"id": "prior_control", "kind": "literature", "locator": survey["source_ref"],
                      "quote": survey["quote"]},
                     {"id": "coverage_result", "kind": "result", "locator": "coverage",
                      "quote": "100% contract-check coverage"}],
        "claims": [{"id": "prior_claim", "statement": survey["quote"], "unit_ids": ["prior"],
                    "evidence_ids": ["prior_control"]},
                   {"id": "coverage_claim", "statement": "100% contract-check coverage",
                    "unit_ids": ["metric"], "evidence_ids": ["coverage_result"]}],
        "references": [{"key": "known", "title": "Known Validation Record", "authors": "Validation Fixture",
                        "year": "2026", "doi": None, "url": "https://example.org/validation",
                        "source_ref": survey["source_ref"]}],
        "authors": ["Sci-saurus Validation Team"], "keywords": ["provenance", "controlled revision", "PDF validation"]}
    config_path = evidence / "paper-score.json"; config_path.write_bytes(canonical_bytes(config))
    result = PaperReleaseBuilder(root, config).build(compile_script=Path(args.compile_script))
    print(json.dumps({"release_dir": str(root), "evidence_dir": str(evidence),
                      "pdf": str(root / "output" / "pdf" / "pipeline_validation.pdf"),
                      "status": result["status"], "release_ref": result["release_ref"]}, indent=2))


if __name__ == "__main__":
    main()
