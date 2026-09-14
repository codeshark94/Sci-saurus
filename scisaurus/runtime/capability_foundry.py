"""Model-driven capability foundry (foundry P3.5).

This is the piece that lets the lab invent an experiment instead of choosing a
pre-written one.  The model authors a study program and an *independently*
authored validator; the foundry then:

1. runs the executor once in the sandbox to compute the deterministic output
   digest (the model cannot know a hash in advance),
2. runs the static/replay/digest/independent-recalculation/review gates,
3. registers the admitted program as a pinned ``experiment-capability-1``
   descriptor in the capability registry.

A failed gate is fed back to the model as a repair request.  Nothing is
executed outside the sandbox and nothing is registered before every gate
passes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_registry import register_capability
from scisaurus.runtime.models import ModelClient, resolve_model_config
from scisaurus.runtime.program_admission import scan_program_source, validate_program_candidate
from scisaurus.runtime.program_gates import admit_program_candidate
from scisaurus.runtime.program_sandbox import run_sandboxed

SYSTEM = (
    "You are the program-authoring specialist for an autonomous research laboratory. "
    "You write ONE deterministic, seeded experiment program and ONE independently authored "
    "validator that recalculates the declared outcomes from the recorded observations alone. "
    "Never use the network, subprocesses, eval/exec, or open(); use only json, math, statistics, "
    "hashlib, pathlib, sys, itertools, functools, random, collections, dataclasses, typing, "
    "decimal, fractions, re, time, os, numpy and matplotlib. "
    "The executor reads a JSON request from stdin and writes exactly one JSON object to stdout. "
    "Return exactly the requested JSON object and no markdown."
)

PROGRAM_OUTPUT_FIELDS = ("schema_version", "study_id", "revision", "procedures", "observations",
                         "metrics", "findings", "limitations", "assets")
ATTEMPT_FIELDS = {"executor_source", "validator_source", "runtime", "test_input", "experiment_intent"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def candidate_prompt(brief, runtime_packages, test_input):
    return {
        "assignment": "author_experiment_program",
        "capability_brief": brief,
        "output_contract": {
            "executor_source": "complete Python source; reads {'configured_input','experiment'} from stdin, "
                               f"writes one JSON object with exactly {list(PROGRAM_OUTPUT_FIELDS)} "
                               "and optionally analysis; schema_version must be experiment-program-output-1; "
                               "study_id/revision must equal experiment_intent.id/revision",
            "validator_source": "complete, separately authored Python source; reads "
                                "{'configured_input','candidate','candidate_sha256'} (or 'primary_outcomes') "
                                "from stdin and writes {'schema_version':'experiment-validation-1',"
                                "'study_id','candidate_sha256','decision','checks','metric_recalculations',"
                                "'limitations'}; decision 'accepted' only when every recalculation matches",
            "runtime": {"python": "declared interpreter version", "packages": [
                {"name": name, "version": version} for name, version in runtime_packages]},
            "test_input": test_input,
            "experiment_intent": {
                "id": "bounded lowercase identifier", "revision": 1,
                "study_type": "one of novel_research|replication|methods_validation|exploratory",
                "domain": "text", "research_question": "text", "hypothesis": "text", "method": "text",
                "parameters": {}, "seed": "non-negative integer",
                "run_count": "positive integer == number of replicate observations per condition",
                "stopping_rule": "text",
                "primary_outcomes": [{"id": "identifier", "definition": "text", "unit": "text",
                                      "direction": "higher|lower|descriptive", "threshold": None}],
                "limitations": ["text", "..."],
                "required_assets": [{"role": "figure", "media_types": ["image/png"], "min_count": 3}],
                "reviewers": [{"id": "identifier", "focus": "text"}, {"id": "identifier", "focus": "text"}],
                "stage_seconds": {"setup": 60, "supervision": 30, "production": 90,
                                  "unit_review": 60, "integrated_review": 180, "reassessment": 90},
                "max_observations": "integer >= run_count",
                "max_asset_bytes": "positive integer",
            },
        },
        "stdin_examples": {
            "executor_receives": {
                "configured_input": "the test_input object supplied below",
                "experiment": {"id": "the study id", "revision": 1, "study_type": "methods_validation",
                               "domain": "...", "research_question": "...", "hypothesis": "...",
                               "method": "...", "parameters": {}, "seed": 7, "run_count": 100,
                               "stopping_rule": "...", "primary_outcomes": [], "limitations": []},
            },
            "validator_receives": {
                "configured_input": {},
                "candidate": "the exact JSON object the executor printed",
                "candidate_sha256": "sha256 of the executor's stdout bytes",
                "primary_outcomes": "the declared primary_outcomes list",
            },
        },
        "experiment_intent_example": {
            "id": "skewed_tail_comparison", "revision": 1, "study_type": "methods_validation",
            "domain": "robust statistics", "research_question": "Does estimator A lower tail error than B?",
            "hypothesis": "Estimator A lowers the 95th-percentile absolute error relative to B.",
            "method": "Seeded finite Monte Carlo comparison recording every replicate before summarizing.",
            "parameters": {"sample_size": 200}, "seed": 11, "run_count": 500,
            "stopping_rule": "Execute exactly 500 replicates; no interim inspection.",
            "primary_outcomes": [{"id": "tail_error", "definition": "95th-percentile absolute error.",
                                  "unit": "error", "direction": "lower", "threshold": None}],
            "limitations": ["Only the declared sampling process and estimators are covered."],
            "required_assets": [{"role": "figure", "media_types": ["image/png"], "min_count": 3}],
            "reviewers": [{"id": "statistical_method", "focus": "Estimator definitions and numerical traceability."},
                          {"id": "adversarial_claims", "focus": "Overstatement and missing limitations."}],
            "stage_seconds": {"setup": 60, "supervision": 30, "production": 90, "unit_review": 60,
                              "integrated_review": 180, "reassessment": 90},
            "max_observations": 5000, "max_asset_bytes": 10000000,
        },
        "constraints": [
            "read the executor's experiment metadata from request['experiment'], never from configured_input.experiment",
            "the validator must read request['candidate'], request['candidate_sha256'] and request['primary_outcomes'] "
            "from the top level; it must not expect an 'experiment' key",
            "the engine must be fully deterministic: one seed, no clock, no unordered iteration",
            "the executor must emit at least three image/png figure assets with captions",
            "the validator must not import or copy the executor source",
            "the validator must recompute every declared primary_outcome from observations only",
            "every observation row must carry a replicate index and the raw values used for the metrics",
        ],
    }


class CapabilityFoundry:
    def __init__(self, model_config, *, runtime_python, workspace_root, registry_root, repo_root,
                 requirements_file, runtime_packages, max_attempts=4, timeout_seconds=900.0):
        self.model_config = deepcopy_config(model_config)
        self.runtime_python = Path(runtime_python)
        self.workspace_root = Path(workspace_root)
        self.registry_root = Path(registry_root)
        self.repo_root = Path(repo_root)
        self.requirements_file = Path(requirements_file)
        self.runtime_packages = [(name, version) for name, version in runtime_packages]
        self.max_attempts = int(max_attempts)
        self.timeout_seconds = timeout_seconds
        for path in (self.runtime_python, self.requirements_file):
            if not path.is_file():
                raise ValidationError(f"foundry requires an existing file: {path}")
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _payload(intent, configured_input):
        """Build the program input exactly as ExperimentRunner does."""
        return {"configured_input": configured_input,
                "experiment": {key: intent[key] for key in (
                    "id", "revision", "study_type", "domain", "research_question", "hypothesis",
                    "method", "parameters", "seed", "run_count", "stopping_rule",
                    "primary_outcomes", "limitations")}}

    @staticmethod
    def _validator_input(data, intent):
        """Add the declared outcomes exactly as ExperimentRunner does."""
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return data
        payload["primary_outcomes"] = intent["primary_outcomes"]
        return canonical_bytes(payload)

    def _execute(self, source, payload):
        workdir = self.workspace_root / "sandbox"
        workdir.mkdir(parents=True, exist_ok=True)
        program = workdir / "program.py"
        program.write_text(source)
        return run_sandboxed([str(self.runtime_python), str(program)], workspace=workdir,
                             input_bytes=payload, timeout_seconds=self.timeout_seconds, max_bytes=60_000_000)

    def generate(self, brief, *, test_input=None, client=None):
        client = client or ModelClient(**resolve_model_config(self.model_config, role="research.experiment-author"))
        feedback = None
        last_error = None
        last_attempt = None
        for attempt in range(self.max_attempts):
            prompt_value = candidate_prompt(brief, self.runtime_packages,
                                            test_input if test_input is not None else {"probe": True})
            if feedback is not None:
                prompt_value["repair_request"] = {
                    "previous_error": str(feedback)[:4000],
                    "previous_attempt": last_attempt,
                    "instructions": "Patch the previous attempt in place. Fix only the reported failure. Keep the exact "
                                    "same top-level keys (executor_source, validator_source, runtime, test_input, "
                                    "experiment_intent) and return nothing else. Never call open(), eval(), exec(), "
                                    "compile(), input() or __import__(); use Path.write_bytes for files.",
                }
            prompt = json.dumps(prompt_value, ensure_ascii=False, sort_keys=True)
            result = client.complete(system=SYSTEM, prompt=prompt)
            if result.finish_reason != "stop":
                last_error = ValidationError("program author did not finish normally")
                feedback = last_error
                continue
            attempt_value = None
            try:
                attempt_value = result.json_object()
                if set(attempt_value) != ATTEMPT_FIELDS:
                    raise ValidationError(
                        f"program author must return exactly {sorted(ATTEMPT_FIELDS)}; "
                        f"observed keys: {sorted(attempt_value)}")
                executor, validator = attempt_value["executor_source"], attempt_value["validator_source"]
                scan_program_source(executor, "program executor")
                scan_program_source(validator, "program validator")
                payload_value = self._payload(attempt_value["experiment_intent"],
                                              attempt_value["test_input"])
                payload = canonical_bytes(payload_value)
                first = self._execute(executor, payload)
                if first.timed_out or first.truncated or first.returncode != 0:
                    raise ValidationError(
                        f"executor failed in the sandbox (status={first.returncode}, "
                        f"timeout={first.timed_out}, truncated={first.truncated}): "
                        + first.stderr.decode("utf-8", "replace")[-1200:])
                import hashlib
                try:
                    document = json.loads(first.stdout)
                except (ValueError, TypeError) as exc:
                    raise ValidationError("executor did not return a JSON document") from exc
                digest = hashlib.sha256(canonical_bytes(document)).hexdigest()
                candidate_value = {
                    "schema_version": "method-program-candidate-1",
                    "study_id": attempt_value["experiment_intent"]["id"],
                    "revision": attempt_value["experiment_intent"]["revision"],
                    "executor_source": executor, "validator_source": validator,
                    "runtime": attempt_value["runtime"],
                    "test_vector": {"input": payload_value, "expected_output_sha256": digest},
                    "experiment_intent": attempt_value["experiment_intent"],
                }
                validate_program_candidate(candidate_value)
                admission = admit_program_candidate(
                    candidate_value,
                    execute=lambda data, src=executor: self._execute(src, data),
                    validate=lambda data, src=validator: self._execute(
                        src, self._validator_input(data, attempt_value["experiment_intent"])),
                    review=self._review)
                registration = register_capability(
                    self.registry_root, candidate_value, admission,
                    runtime_python=self.runtime_python, repo_root=self.repo_root,
                    requirements_file=self.requirements_file)
                return {"status": "registered", "attempts": attempt + 1, "admission": admission,
                        "registration": registration, "candidate": candidate_value}
            except (ValidationError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                feedback = exc
                last_attempt = {key: (value[:20000] if isinstance(value, str) else value)
                                for key, value in (attempt_value.items() if isinstance(attempt_value, dict) else [])}
                continue
        raise ValidationError(f"capability foundry did not admit a program in {self.max_attempts} attempts: {last_error}")

    @staticmethod
    def _review(candidate, document, verdict):
        """Deterministic local review gate.

        A model-backed adversarial reviewer can replace this callable; the
        deterministic checks below always run first so a generated program
        cannot be admitted with empty findings.
        """
        findings = []
        metrics = {item.get("id") for item in document.get("metrics", []) if isinstance(item, dict)}
        declared = {item["id"] for item in candidate["experiment_intent"]["primary_outcomes"]}
        if declared - metrics:
            findings.append({"severity": "blocking", "finding": "declared outcome missing from program metrics"})
        figures = [item for item in document.get("assets", [])
                   if isinstance(item, dict) and item.get("role") == "figure"]
        if len(figures) < 3:
            findings.append({"severity": "blocking", "finding": "fewer than three figure assets"})
        if not document.get("limitations"):
            findings.append({"severity": "blocking", "finding": "program reports no limitations"})
        return {"status": "rejected" if findings else "admitted", "findings": findings}


def deepcopy_config(value):
    return json.loads(canonical_bytes(value).decode())
