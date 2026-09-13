"""Bounded literature discovery, scoped mapping, and independent gap assessment."""
from copy import deepcopy
import hashlib
import itertools
import json
import re
import signal
import threading
import time
import unicodedata

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.source_spans import bind as bind_source_spans, contains_legacy
from scisaurus.core.surveys import RELATIONSHIP_SEMANTICS, SurveyGate, work_review_checks
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker
from scisaurus.runtime.config import configured_worker_slots
from scisaurus.runtime.bibliographic_identity import reconcile_result
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.literature import SEARCH_SYNTAX
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.survey_config import validate_survey_config, search_query
from scisaurus.runtime.survey_records import (MAP_FIELDS, SURVEY_CHECKS, GAP_CHECKS, validate_map,
                                             validate_survey_review, validate_assessment, validate_work_review)
from scisaurus.runtime.time_policy import TimePolicy


def normalized(text):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def apply_scoped_map_repair(wid, previous, old_relationships, feedback, patch):
    """Compose a narrow repair without asking a worker to reproduce protected state."""
    exact(patch, {"entry_updates", "relationships"}, "scoped map repair")
    updates = patch["entry_updates"]
    relations = patch["relationships"]
    if not isinstance(updates, dict) or set(updates) != set(feedback["entry_fields"]):
        raise ValidationError("scoped map repair must return exactly the granted entry fields")
    if not isinstance(relations, list):
        raise ValidationError("scoped map repair relationships must be a list")
    targets = set(feedback["relationship_targets"])
    if any(not isinstance(relation, dict) or relation.get("source") != wid
           or relation.get("target") not in targets for relation in relations):
        raise ValidationError("scoped map repair changed an ungranted relationship")
    entry = deepcopy(previous)
    entry.update(updates)
    retained = [{key: relation[key] for key in ("source", "target", "kind", "claim")}
                for relation in old_relationships if relation["target"] not in targets]
    return {"entries": [entry], "relationships": [*retained, *deepcopy(relations)]}


def overlay_post_checkpoint_relationships(relationships, checkpoint_created_at, candidates):
    """Recover validated relationship versions newer than an aggregate checkpoint."""
    restored = dict(relationships)
    for record, relation in candidates:
        key = "-".join(relation[field] for field in ("source", "target", "kind"))
        # A checkpoint that still names this relationship cannot knowingly
        # delete it; advance it to its validated head even when a later restart
        # accidentally wrote another aggregate checkpoint from the stale ref.
        # A relationship absent from the checkpoint is added only when its
        # validated version postdates that checkpoint, preserving deletions.
        if key not in restored and record["created_at"] <= checkpoint_created_at:
            continue
        restored[key] = {**relation, "artifact_ref": record["artifact_ref"]}
    return restored


class SurveyRunner(ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None, resume_policy=None):
        super().__init__(project_dir, validate_survey_config(config), worker_target=_invoke_worker,
                         on_progress=on_progress, resume_policy=resume_policy)
        self.score = self.config["survey"]
        self.bounds = self.score["search"]
        self.operations = OperationsCell(self.control, self.store, project_id=self.config["project_id"])
        self.gate = SurveyGate(self.control, self.store)
        self.worker_slots = configured_worker_slots(self.config["limits"])
        self.bibliography_mode = "openalex"
        self.bibliography_fallback_reason = None
        self.bibliography_fallback_capability = None
        self.work_budget_adjustments = []
        self.time_policy = self._plan_work_budget()
        self.time_policy.started_at = self.started
        self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)
        # Provider pacing is independent from request retries.  The former
        # spaces successful calls across a provider's rate window, while the
        # latter handles transient failures for one request.  Keeping a
        # monotonic schedule means a slow response naturally consumes the
        # interval instead of adding another unnecessary sleep.
        self.provider_intervals = {
            name: float(value)
            for name, value in self.score.get("provider_intervals", {}).items()
        }
        self.next_provider_at = {name: self.started for name in self.provider_intervals}
        self.provider_waits = []
        self.work_records, self.works, self.source_docs, self.source_records = {}, {}, {}, {}
        self.identity_records = {}
        self.aliases, self.dois, self.analysis_records, self.analyzed_basis = {}, {}, {}, {}
        self.relationships, self.bindings, self.capability_ids = {}, {}, []
        self.work_reviews, self.reviewed_basis = {}, {}
        self.query_refs, self.search_log, self.expansion_log, self.gaps, self.time_decisions = [], [], [], [], []
        self.api_calls, self.identity_calls, self.serial, self.survey_revision = 0, 0, 0, 0
        self.expanded, self.full_text_attempted = set(), set()
        self.survey_ref, self.assessment_ref, self.register_ref = None, None, None
        self.nomination = None
        self.nomination_record = None
        self.counter_plan_record = None
        self.counter_query_refs = []
        self.counter_queries_complete = False
        self.countersearch_complete = False
        if self.resume_session:
            self.time_policy.hard_seconds = min(
                self.time_policy.hard_seconds, self.resume_session["additional_seconds"])
            self.time_policy.target_seconds = min(self.time_policy.target_seconds, self.time_policy.hard_seconds)
            self.time_policy.first_result_seconds = min(
                self.time_policy.first_result_seconds, self.time_policy.target_seconds)
            self._restore()

    def _plan_work_budget(self):
        """Fit the discoverable work budget to the configured review reserve.

        Survey estimates include production, per-work review, and integrated
        review.  A large discovery ceiling can therefore be infeasible even
        when a useful minimum survey would fit.  Reduce only the bounded
        workload, preserving the configured seed and challenge reserve, and
        retain the decision in the run artifact.  An impossible minimum still
        remains a hard admission failure; this never fabricates time or
        silently drops required seeds.
        """
        kwargs = {
            "stage_seconds": self.score["stage_seconds"],
            "worker_slots": self.worker_slots,
            "wall_clock_seconds": self.config["limits"]["wall_clock_seconds"],
            "policy": self.config.get("time_policy"),
        }
        requested = self.bounds["max_works"]
        initial = TimePolicy(unit_count=requested, **kwargs)
        snapshot = initial.snapshot()
        if snapshot["initial_hard_limit_feasible"] and snapshot["initial_target_feasible"]:
            return initial

        reserve = self.bounds.get("challenge_reserve", 0)
        floor = max(reserve + 1, len(self.score["seed_work_ids"]) + reserve)
        if floor > requested:
            return initial

        # The schedule is monotone in unit_count.  Binary search keeps the
        # planning pass constant-time even when a caller requests a very large
        # discovery ceiling.
        low, high, feasible = floor, requested, None
        while low <= high:
            candidate = (low + high) // 2
            policy = TimePolicy(unit_count=candidate, **kwargs)
            candidate_snapshot = policy.snapshot()
            if (candidate_snapshot["initial_hard_limit_feasible"]
                    and candidate_snapshot["initial_target_feasible"]):
                feasible = candidate
                low = candidate + 1
            else:
                high = candidate - 1
        if feasible is None:
            return initial

        self.bounds["max_works"] = feasible
        self.work_budget_adjustments.append({
            "kind": "deadline_fit",
            "requested_max_works": requested,
            "effective_max_works": feasible,
            "minimum_preserved": floor,
            "reason": "configured review schedule exceeded target or hard deadline",
        })
        return TimePolicy(unit_count=feasible, **kwargs)

    def _body(self, record):
        return json.loads(self.store.read_body(record["body_hash"]))

    def _wait_provider(self, capability):
        """Wait until the next paced provider slot, bounded by the run deadline."""
        interval = self.provider_intervals.get(capability, 0.0)
        if interval <= 0:
            return
        due = self.next_provider_at.get(capability, self.started)
        waited = 0.0
        while True:
            self._ensure_active()
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            if time.monotonic() + remaining >= self.deadline:
                raise ValidationError(f"provider interval for {capability} exceeds run deadline")
            time.sleep(min(remaining, 0.25))
            waited += min(remaining, 0.25)
        self._ensure_active()
        self.next_provider_at[capability] = time.monotonic() + interval
        self.provider_waits.append({"capability": capability, "waited_seconds": round(waited, 3),
                                    "interval_seconds": interval})

    def _heads(self, prefix):
        rows = self.control._conn.execute(
            "SELECT logical_id, MAX(version) AS version FROM artifacts "
            "WHERE logical_id LIKE ? GROUP BY logical_id ORDER BY logical_id", (prefix + "%",),
        ).fetchall()
        return [self.store.get(f"artifact:{row['logical_id']}@{row['version']}") for row in rows]

    def _restore(self):
        """Reconstruct only durable, content-addressed survey state.

        Completed provider effects are represented by their recorded execution
        artifacts and query records. A source-policy scope can deliberately
        invalidate later interpretation without discarding those captures.
        """
        score = self.store.head(f"command/scores/{self.score['id']}")
        protocol = self.store.head("kb/search-protocol")
        if score:
            self.score_ref = score["artifact_ref"]
        if protocol:
            self.protocol = protocol
        register = self.store.head("kb/work-register")
        register_body = self._body(register) if register else {"aliases": {}, "source_refs": [], "query_refs": []}
        self.register_ref = register["artifact_ref"] if register else None
        self.aliases = dict(register_body.get("aliases", {}))
        for record in self._heads("kb/works/"):
            body = self._body(record)
            wid = body["work_id"]
            self.work_records[wid], self.works[wid] = record, body
            for provider_id in body.get("provider_ids", []):
                self.aliases[provider_id] = wid
            if body.get("doi"):
                self.dois[body["doi"]] = wid
        for ref in register_body.get("identity_refs", []):
            record = self.store.get(ref)
            self.identity_records[self._body(record)["work_id"]] = record
        for ref in register_body.get("source_refs", []):
            record = self.store.get(ref)
            body = self._body(record)
            self.source_docs[ref] = body
            key = "full_text" if record["artifact_id"].startswith("kb/full-text/") else "abstract"
            self.source_records[f"{key}/{body['work_id']}"] = record
            if key == "full_text":
                self.full_text_attempted.add(body["work_id"])
        self.query_refs = list(register_body.get("query_refs", []))
        self.search_log = [self._body(self.store.get(ref)) for ref in self.query_refs]
        reservations = [self._body(record) for record in self._heads("command/api-calls/")]
        self.identity_calls = max(
            len(self.identity_records),
            max((row.get("identity_number", 0) for row in reservations), default=0),
        )
        # Reservations are committed before dispatch, so failed and
        # result-unknown calls remain charged after a restart. The fallback
        # preserves cumulative accounting for runs created before reservations
        # were introduced.
        self.api_calls = max(
            len(self.query_refs) + len(self.identity_records),
            max((row["number"] for row in reservations), default=0),
        )
        coverage = self.store.head("kb/coverage")
        if coverage:
            coverage_body = self._body(coverage)
            self.expansion_log = list(coverage_body.get("expansion", []))
            self.gaps = list(coverage_body.get("access_and_limit_gaps", []))
            self.expanded = {wid for row in self.expansion_log for wid in row.get("seed_work_ids", [])}
        map_record = self.store.head("kb/literature-map")
        if map_record:
            map_body = self._body(map_record)
            self.map_record = map_record
            for ref in map_body.get("entry_refs", []):
                record = self.store.get(ref)
                wid = self._body(record)["work_id"]
                self.analysis_records[wid] = record
            for ref in map_body.get("relationship_refs", []):
                record = self.store.get(ref)
                relation = self._body(record)
                key = "-".join(relation[field] for field in ("source", "target", "kind"))
                self.relationships[key] = {**relation, "artifact_ref": ref}
            # A validated per-work update can be committed immediately before a
            # process stops and therefore postdate the aggregate map checkpoint.
            # Overlay only those later versions. Absence is not interpreted as a
            # deletion, so an interrupted removal is conservatively retried.
            candidates = [(record, self._body(record)) for record in self._heads("kb/relationships/")]
            self.relationships = overlay_post_checkpoint_relationships(
                self.relationships, map_record["created_at"], candidates)
        scopes = set(self.resume_session["reopened_scopes"])
        if "mapping" not in scopes:
            for wid, record in self.analysis_records.items():
                self.analyzed_basis[wid] = [self.work_records[wid]["artifact_ref"],
                    *([self.identity_records[wid]["artifact_ref"]] if wid in self.identity_records else []), *[
                    ref for ref, source in self.source_docs.items() if source["work_id"] == wid]]
        if "focused_review" not in scopes and "mapping" not in scopes:
            for record in self._heads("kb/work-reviews/"):
                body = self._body(record)
                wid = self._body(self.store.get(body["entry_ref"]))["work_id"]
                checks = body.get("checks", [])
                relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
                refs = [relation["artifact_ref"] for relation in relations]
                sources = [ref for ref, source in self.source_docs.items()
                           if source["work_id"] in {wid, *[relation["target"] for relation in relations]}]
                if (body.get("entry_ref") == self.analysis_records.get(wid, {}).get("artifact_ref")
                        and body.get("relationship_refs") == refs and checks
                        and all(check.get("outcome") == "passed" for check in checks)):
                    self.work_reviews[wid] = record
                    self.reviewed_basis[wid] = [body["entry_ref"], *refs, *sources]
        self.survey_revision = self.control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'kb/survey-reviews/%'",
        ).fetchone()[0]
        task_ids = [row[0] for row in self.control._conn.execute(
            "SELECT task_id FROM tasks WHERE task_id LIKE 'survey-%'"
        ).fetchall()]
        suffixes = [int(match.group(1)) for task_id in task_ids if (match := re.search(r"-([0-9]+)$", task_id))]
        self.serial = max(suffixes, default=0)
        if "integrated_review" not in scopes and "mapping" not in scopes and "focused_review" not in scopes:
            accepted = self.store.accepted("kb/surveys/current")
            if accepted:
                try:
                    self.gate.require_current(accepted["artifact_ref"])
                    self.survey_ref = self.incumbent = accepted["artifact_ref"]
                    self.time_policy.mark_retained_result(self.survey_ref)
                except Exception:
                    pass
        nomination = self.store.head("kb/gap-nomination")
        if nomination:
            self.nomination_record, self.nomination = nomination, {
                key: self._body(nomination)[key] for key in ("id", "statement")}
        self._refresh_countersearch_state()
        if self.survey_ref and "gap_assessment" not in scopes:
            accepted = self.store.accepted("kb/gap-assessments/current")
            if accepted:
                try:
                    self.gate.require_current_assessment(accepted["artifact_ref"])
                    self.assessment_ref = accepted["artifact_ref"]
                except Exception:
                    pass

    def _refresh_countersearch_state(self):
        """Reconstruct challenge completion from exact durable dependencies."""
        self.counter_plan_record = None
        self.counter_query_refs = []
        self.counter_queries_complete = False
        self.countersearch_complete = False
        if self.nomination_record is None:
            return
        plan = self.store.head("kb/counter-search-plan")
        if plan is None:
            return
        body = self._body(plan)
        nomination_body = self._body(self.nomination_record)
        if (body.get("nomination_ref") != self.nomination_record["artifact_ref"]
                or body.get("survey_ref") != nomination_body.get("survey_ref")):
            return
        proposal = {key: body.get(key) for key in ("queries", "rationale")}
        try:
            self._plan_validator(proposal)
        except ValidationError:
            return
        successful = {}
        for ref, row in zip(self.query_refs, self.search_log):
            request = row.get("request", {})
            query = request.get("query")
            if (row.get("role") == "methods.novelty-challenger"
                    and row.get("plan_ref") == plan["artifact_ref"]
                    and request.get("operation") == "search" and query):
                successful[normalized(query)] = ref
        required = [normalized(query) for query in proposal["queries"]]
        if not all(query in successful for query in required):
            self.counter_plan_record = plan
            return
        self.counter_plan_record = plan
        self.counter_query_refs = [successful[query] for query in required]
        self.counter_queries_complete = True
        if self.survey_ref:
            survey = self._body(self.store.get(self.survey_ref))
            self.countersearch_complete = all(ref in survey.get("query_refs", [])
                                              for ref in self.counter_query_refs)

    def _record(self, logical, kind, body, author, *, subjects=()):
        head = self.store.head(logical)
        if head and self.store.read_body(head["body_hash"]) == canonical_bytes(body):
            return head
        return self._publish(logical, kind, body, author, subjects=subjects)

    def _reserve_api_call(self, capability, request, actor):
        """Durably charge a provider call before its outcome can become unknown."""
        number = self.api_calls + 1
        identity_number = self.identity_calls + (capability == "identity")
        self._publish(
            f"command/api-calls/{number}",
            "note",
            {"number": number, "identity_number": identity_number,
             "capability": capability, "request": request},
            actor,
            subjects=[self.score_ref],
        )
        self.api_calls = number
        self.identity_calls = identity_number

    def _tick(self, stage, *, count=1, pending_review_count=0):
        self._ensure_active()
        decision = self.time_policy.admit(stage, task_count=count, pending_review_count=pending_review_count)
        decision.update(task_count=count, worker_slots=self.worker_slots, pending_review_count=pending_review_count)
        self.time_decisions.append(decision)
        self._publish(f"command/time-decisions/{len(self.time_decisions)}", "note", decision,
                      "command.controller", subjects=[self.score_ref])
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred {stage}: {decision['reason']}")

    def _before_dispatch(self, spec):
        super()._before_dispatch(spec)
        if spec["kind"] == "model":
            prompt = json.loads(spec["params"]["prompt"])
            if "prerequisite_survey_ref" in prompt:
                required = prompt["prerequisite_survey_ref"]
                if required != prompt.get("survey_ref"):
                    raise ValidationError("gap task has inconsistent survey prerequisites")
                self.gate.require_current(required)
            if prompt.get("phase") in {"counter_plan", "gap_assessment"}:
                nomination = self.gate.require_current_nomination(prompt.get("nomination_ref"))
                if canonical_bytes(prompt.get("gap")) != canonical_bytes({
                        key: nomination[key] for key in ("id", "statement")}):
                    raise ValidationError("gap task does not match its exact nomination")
        for binding in self.bindings.values():
            self.operations.authorize(binding)
        self._ensure_active()

    def _model_checked(self, name, actor, assignment, validator, *, normalizer=None,
                       stage="production", task_kind="production"):
        return self._models_checked([{"name": name, "actor": actor, "assignment": assignment,
            "validator": validator, **({"normalizer": normalizer} if normalizer else {})}],
            stage=stage, task_kind=task_kind)[name]

    def _retained_validation_feedback(self, name, assignment):
        """Reuse a failed response only when its original assignment is still exact."""
        if not self.resume_session:
            return None
        prefix = f"command/validation/survey-{name}-"
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? ORDER BY created_at DESC",
            (prefix + "%",)).fetchall()
        for row in rows:
            validation = self.store.get(row["artifact_ref"])
            try:
                error = self._body(validation)["error"]
                proposal = self.store.get(validation["inputs"][0]["ref"])
                previous_response = self._body(proposal)
                execution = self.store.get(proposal["inputs"][0]["ref"])
                context = self._body(self.store.get(execution["inputs"][0]["ref"]))
                prior_assignment = json.loads(context["prompt"])
                prior_assignment.pop("validation_feedback", None)
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if canonical_bytes(prior_assignment) == canonical_bytes(assignment):
                return {"error": error, "previous_response": previous_response,
                    "scope": "Repair only this assignment's recorded contract violations; preserve every valid field. "
                             "Use every requested field name and enum value exactly as specified; do not substitute synonyms."}
        return None

    def _models_checked(self, jobs, *, stage="production", task_kind="production"):
        """Validate worker waves and repair only rejected assignments.

        Ordinary fixture runs retain the historical ``max_rounds`` bound. A
        long autonomous mission can opt into ``limits.repair_mode`` set to
        ``until_deadline``; then a malformed response never terminates the
        stage because an arbitrary retry counter ran out. The runner keeps
        retrying the rejected assignment until its existing wall-clock and
        admission policy no longer permits another call.
        """
        pending, results = list(jobs), {}
        feedback = {job["name"]: retained for job in pending
                    if (retained := self._retained_validation_feedback(job["name"], job["assignment"])) is not None}
        repair_mode = self.config["limits"].get("repair_mode", "bounded")
        rounds = itertools.count() if repair_mode == "until_deadline" else range(self.config["limits"]["max_rounds"])
        for _ in rounds:
            self._ensure_active()
            rejected = []
            for offset in range(0, len(pending), self.worker_slots):
                wave = pending[offset:offset + self.worker_slots]
                self._tick(stage, count=len(wave), pending_review_count=len(results) if stage in {"production", "revision"} else 0)
                specs = []
                for job in wave:
                    self.serial += 1
                    repair = feedback.get(job["name"])
                    assignment = {**job["assignment"], **({"validation_feedback": repair} if repair else {})}
                    client = deepcopy(self.config["model"])
                    if isinstance(job.get("model_overrides"), dict):
                        client.update(job["model_overrides"])
                    specs.append({"task_id": f"survey-{job['name']}-{self.serial}", "kind": "model",
                        "actor": job["actor"], "task_kind": task_kind,
                        "params": {"client": client, "role": job["actor"],
                                   "prompt": json.dumps(assignment, ensure_ascii=False)}})
                outcomes = self._call_batch(specs, max_parallel=self.worker_slots)
                failures = []
                for job, spec in zip(wave, specs):
                    task_id, actor = spec["task_id"], spec["actor"]
                    outcome = outcomes[task_id]
                    if not outcome["ok"]:
                        failures.append(f"{job['name']}: {outcome['error']}")
                        continue
                    result = ModelResult(**outcome["result"])
                    execution = outcome["record_ref"]
                    self.time_policy.observe(stage, result.elapsed_seconds)
                    try:
                        value = result.json_object()
                    except ValidationError:
                        value = {"raw_text": result.text}
                    proposal = self._publish(f"kb/model-proposals/{task_id}", "note", value, actor, subjects=[execution])
                    try:
                        if result.finish_reason != "stop":
                            raise ValidationError(f"model generation did not finish normally: {result.finish_reason}")
                        value = result.json_object()
                        if job.get("normalizer"):
                            value = job["normalizer"](value)
                        job["validator"](value)
                    except (ValidationError, TypeError, ValueError, KeyError) as exc:
                        feedback[job["name"]] = {"error": str(exc), "previous_response": value,
                            "scope": "Repair only this assignment's contract violations; preserve every valid field. "
                                     "Use every requested field name and enum value exactly as specified; do not substitute synonyms."}
                        self.tasks.transition(task_id, "blocked", "command.controller", reason=str(exc))
                        self._publish(f"command/validation/{task_id}", "note", {"error": str(exc)},
                                      "command.controller", subjects=[proposal["artifact_ref"]])
                        rejected.append(job)
                        continue
                    self._ensure_active()
                    if job.get("on_valid"):
                        job["on_valid"](value, execution)
                    self._complete(task_id)
                    results[job["name"]] = (value, execution)
                if failures:
                    raise ValidationError("model dispatch failed: " + "; ".join(failures))
            if not rejected:
                return results
            pending = rejected
        raise ValidationError("; ".join(
            f"{job['name']} did not satisfy its evidence contract: {feedback[job['name']]['error']}" for job in pending))

    def _plan_validator(self, value):
        exact(value, {"queries", "rationale"}, "search plan")
        queries = value["queries"]
        if (not isinstance(queries, list) or not 1 <= len(queries) <= self.bounds["queries_per_role"]
                or any(not isinstance(q, str) or not q.strip() or len(q) > 2048 for q in queries)
                or len({normalized(q) for q in queries}) != len(queries)
                or not isinstance(value["rationale"], str) or not value["rationale"].strip()):
            raise ValidationError("search plan requires unique bounded queries and a rationale")
        for query in queries:
            search_query(query)

    def _initialize(self):
        score = self._publish(f"command/scores/{self.score['id']}", "note",
            {"schema_version": "literature-survey-score-1", "survey": self.score,
             "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = score["artifact_ref"]
        self.protocol = self._publish("kb/search-protocol", "search_campaign", {
            "question": self.score["question"], "seed_queries": self.score["seed_queries"],
            "seed_work_ids": self.score["seed_work_ids"], "bounds": self.bounds,
            "scope": "Configured providers and finite search/citation batches; not exhaustive scholarly coverage",
            "publication_metadata": "Provider-reported, not independently confirmed publication history",
            "independence": "Separately planned topic searches before the nominated gap is exposed; later targeted counter-search",
        }, "command.search-coordinator", subjects=[self.score_ref])
        self._checkpoint("survey_initialized", force=True)

    def _setup(self):
        self._tick("setup")
        started = time.monotonic()
        for key in ("bibliography", "identity", "full_text"):
            definition = self.score.get(key)
            if definition is None:
                continue
            client = deepcopy(definition["client"])
            if definition["adapter"] == "mcp_fetch":
                client.update(cwd=str(self.operations.workspace_dir(definition["id"])), own_process_group=False)
            self.operations.register(definition["id"], adapter=definition["adapter"], client=client,
                representative=definition["representative"], environment_files=definition["environment_files"],
                engineer="operations.engineer")
            self.capability_ids.append(definition["id"])
        for key in ("bibliography", "identity", "full_text"):
            definition = self.score.get(key)
            if definition is None:
                continue
            self._wait_provider(key)
            state = self.operations.ensure_ready(definition["id"], self._call, operator="operations.operator",
                verifier="operations.verifier", purpose=self.score["question"])
            if state["state"] != "ready":
                if key == "bibliography":
                    # A provider outage or exhausted quota must not strand a
                    # whole run when the configured Crossref identity service
                    # can still supply bounded metadata.  The fallback is
                    # activated only after its own independent readiness check
                    # below; the degraded OpenAlex state remains recorded.
                    self.bibliography_mode = "crossref"
                    self.bibliography_fallback_reason = state.get("reason") or "OpenAlex readiness failed"
                    self.gaps.append({"kind": "bibliography_fallback", "from": "openalex", "to": "crossref",
                                      "reason": self.bibliography_fallback_reason})
                    continue
                self.gaps.append({"kind": key + "_unavailable", "reason": state["reason"]})
                continue
            self.bindings[key] = state["binding"]
            self.operations.idle(definition["id"])
        if self.bibliography_mode == "crossref":
            if "identity" not in self.bindings:
                raise ValidationError("bibliographic capability unavailable and Crossref fallback is not ready")
            self.bibliography_fallback_capability = self.score["identity"]["id"]

    @staticmethod
    def _crossref_work(source):
        """Project a verified Crossref metadata item into the survey card shape.

        Crossref has no OpenAlex work identifier or citation graph.  A stable
        local identifier keeps the provenance graph addressable while the
        source URL, DOI, and provider are retained so the projection cannot be
        mistaken for an OpenAlex record.
        """
        doi = source.get("doi")
        if not isinstance(doi, str) or not doi.strip():
            raise ValidationError("Crossref fallback item has no DOI")
        doi = doi.strip().lower()
        number = int(hashlib.sha256(doi.encode("utf-8")).hexdigest()[:16], 16) or 1
        wid = "W" + str(number)
        title = source.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("Crossref fallback item has no title")
        year = None
        for field in ("published", "published-print", "published-online", "issued"):
            value = source.get(field)
            parts = value.get("date-parts") if isinstance(value, dict) else None
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                candidate = parts[0][0]
                if type(candidate) is int and 1 <= candidate <= 9999:
                    year = candidate
                    break
        abstract = source.get("abstract")
        if abstract is not None and not isinstance(abstract, str):
            abstract = None
        if isinstance(abstract, str):
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip() or None
        authors = []
        for author in source.get("authors", []):
            if not isinstance(author, dict):
                continue
            name = " ".join(str(author.get(key, "")).strip()
                            for key in ("given", "family")
                            if isinstance(author.get(key), str) and author.get(key).strip())
            if name and name not in authors:
                authors.append(name)
        source_url = source.get("source_url") or ("https://doi.org/" + doi)
        return {
            "id": wid, "doi": doi, "title": title.strip(), "year": year,
            "abstract": abstract, "referenced_works": [], "related_works": [],
            "locations": [{"is_oa": False, "version": None,
                           "landing_page_url": source_url, "pdf_url": None}],
            "source_url": source_url, "bibliography_provider": "crossref",
            **({"authors": authors} if authors else {}),
        }

    def _failure_outcome(self, capability_id):
        """Read the recorded provider outcome after an operational failure."""
        try:
            state = self.operations.status(capability_id)
            failure_ref = state.get("failure_ref")
            if not failure_ref:
                return None
            body = self._body(self.store.get(failure_ref))
            execution_ref = body.get("execution_ref")
            if execution_ref:
                execution = self._body(self.store.get(execution_ref))
                return execution.get("outcome") or execution.get("metadata", {}).get("http_status")
            return body.get("outcome")
        except (KeyError, TypeError, ValueError, ValidationError):
            return None

    def _activate_crossref_fallback(self, reason):
        if self.bibliography_mode == "crossref":
            return
        self.bibliography_mode = "crossref"
        # The generic dispatch guard re-authorizes every active binding before
        # spawning a worker.  Once OpenAlex is degraded its binding is no
        # longer valid; retaining it here would make a healthy Crossref
        # fallback fail before it can dispatch.  The degraded capability and
        # failure artifact remain durable evidence in the operations cell.
        self.bindings.pop("bibliography", None)
        self.bibliography_fallback_reason = str(reason)
        reserve = self.bounds.get("challenge_reserve", 0)
        minimum = max(reserve + 1, len(self.score["seed_work_ids"]) + reserve)
        fallback_cap = max(minimum, 20)
        requested = self.bounds["max_works"]
        if requested > fallback_cap:
            self.bounds["max_works"] = fallback_cap
            self.work_budget_adjustments.append({
                "kind": "provider_fallback_fit",
                "requested_max_works": requested,
                "effective_max_works": fallback_cap,
                "minimum_preserved": minimum,
                "reason": "Crossref metadata fallback is bounded to the single-worker review window",
            })
            self.time_policy.rebudget(fallback_cap)
        self.gaps.append({"kind": "bibliography_fallback", "from": "openalex", "to": "crossref",
                          "reason": self.bibliography_fallback_reason})
        if "identity" in self.bindings:
            self.bibliography_fallback_capability = self.score["identity"]["id"]

    def _crossref_bibliographic_call(self, arguments, *, role, plan_ref=None, admission="discovery"):
        """Run one bounded Crossref search through the verified identity cell."""
        capability = self.bibliography_fallback_capability or self.score.get("identity", {}).get("id")
        binding = self.bindings.get("identity") if capability else None
        if binding is None:
            raise ValidationError("Crossref fallback binding is unavailable")
        query = arguments.get("query")
        if arguments.get("operation") == "work":
            known = self.works.get(arguments.get("work_id"), {})
            query = known.get("doi") or arguments.get("work_id")
        if not isinstance(query, str) or not query.strip():
            self.gaps.append({"kind": "bibliography_fallback_unqueryable", "request": arguments})
            return None
        admission_limit = self.bounds["max_works"] - (
            self.bounds.get("challenge_reserve", 0) if admission == "discovery" else 0)
        remaining = admission_limit - len(self.works)
        if remaining <= 0 and admission == "discovery":
            self.gaps.append({"kind": "work_limit", "request": arguments, "provider": "crossref"})
            return None
        provider_arguments = {"query": query, "limit": min(arguments["limit"], max(1, remaining))}
        self._wait_provider("identity")
        self._reserve_api_call("bibliography", arguments, role)
        result, execution = self.operations.run(binding, provider_arguments, self._call, operator=role)
        works = [self._crossref_work(source) for source in result.get("sources", [])]
        added = self._ingest(works, execution, admission=admission)
        metadata = result.get("metadata", {})
        body = {
            "request": arguments, "provider": "crossref", "provider_request": provider_arguments,
            "role": role, "execution_ref": execution, "plan_ref": plan_ref,
            "returned_work_ids": [work["id"] for work in works], "new_unique_works": added,
            "count": len(works), "provider_total_results": metadata.get("total_results"),
            "next_cursor": None, "has_more": False,
        }
        record = self._publish(f"kb/queries/{self.api_calls}", "query_record", body, role,
                               subjects=[execution, *([plan_ref] if plan_ref else [])])
        self.query_refs.append(record["artifact_ref"])
        self.search_log.append(body)
        self._update_register()
        self._checkpoint("literature_captured", force=True)
        return body
        self.time_policy.observe("setup", time.monotonic() - started)

    def _initial_plans(self):
        plans, jobs, plan_ids = [], [], {}
        for role in ("research.search-planner", "methods.blind-search-planner"):
            plan_id = role.replace(".", "-")
            retained = self.store.head(f"kb/search-plans/{plan_id}") if self.resume_session else None
            if retained is not None:
                value = self._body(retained)
                self._plan_validator(value)
                plans.append((role, value["queries"], retained["artifact_ref"]))
                continue
            task_id = plan_id
            if self.control._conn.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
                task_id = f"{plan_id}-{self.run_id}"
            assignment = {"assignment": "Plan a topic search without assuming a particular research gap.",
                "phase": "blind_plan", "question": self.score["question"],
                "seed_terms": self.score["seed_queries"], "max_queries": self.bounds["queries_per_role"],
                "search_syntax": SEARCH_SYNTAX,
                "instructions": "Return {queries:[search strings],rationale:string}. Use a distinct terminology or neighboring method family. Do not assert novelty."}
            plan_ids[task_id] = plan_id
            jobs.append({"task_id": task_id, "kind": "model", "actor": role, "task_kind": "service",
                         "params": {"client": self.config["model"], "role": role,
                                    "prompt": json.dumps(assignment, ensure_ascii=False)}})
        if not jobs:
            return plans
        self._tick("supervision", count=len(jobs))
        outcomes = self._call_batch(jobs, max_parallel=self.worker_slots)
        for job in jobs:
            outcome = outcomes[job["task_id"]]
            if not outcome["ok"]:
                raise ValidationError(f"independent search planning failed: {outcome['error']}")
            result = ModelResult(**outcome["result"])
            if result.finish_reason != "stop":
                raise ValidationError("search planner did not finish normally")
            value = result.json_object()
            self._plan_validator(value)
            self.time_policy.observe("supervision", result.elapsed_seconds)
            record = self._publish(f"kb/search-plans/{plan_ids[job['task_id']]}", "note", value, job["actor"], subjects=[outcome["record_ref"]])
            self._complete(job["task_id"])
            plans.append((job["actor"], value["queries"], record["artifact_ref"]))
        return plans

    def _ingest(self, works, execution, *, admission):
        if admission not in {"discovery", "challenge"}:
            raise ValidationError("literature admission phase is invalid")
        admission_limit = self.bounds["max_works"]
        if admission == "discovery":
            admission_limit -= self.bounds.get("challenge_reserve", 0)
        added = 0
        for observed in works:
            wid = self.aliases.get(observed["id"]) or self.dois.get(observed["doi"]) or observed["id"]
            if wid not in self.works and len(self.works) >= admission_limit:
                self.gaps.append({"kind": "work_limit", "work_id": observed["id"],
                                  "admission": admission,
                                  "reserved_challenge_slots": self.bounds.get("challenge_reserve", 0)})
                continue
            self.aliases[observed["id"]] = wid
            if observed["doi"]:
                self.dois[observed["doi"]] = wid
            old = self.works.get(wid)
            item = deepcopy(observed)
            item.update(work_id=wid, id=wid, provider_ids=sorted({observed["id"], *(old or {}).get("provider_ids", [])}),
                        publication_metadata_status="provider_reported")
            # Duplicate identities retain their first record and every observed provider ID.
            if old and observed["id"] != wid:
                item = {**old, "provider_ids": item["provider_ids"]}
            if not old:
                added += 1
            if old != item:
                self.works[wid] = item
                record = self._record(f"kb/works/{wid}", "reference_card", item, "research.cataloger", subjects=[execution])
                self.work_records[wid] = record
            if item.get("abstract"):
                current = self.source_records.get(f"abstract/{wid}")
                previous = self.source_docs.get(current["artifact_ref"]) if current else None
                if previous is None or previous["text"] != item["abstract"]:
                    source_url = item.get("source_url") or ("https://openalex.org/" + wid)
                    body = {"work_id": wid, "representation": "abstract", "text": item["abstract"],
                            "url": source_url, "execution_ref": execution, "identity_verified": False}
                    source = self._publish(f"kb/abstracts/{wid}", "source_capture", body,
                                           "research.cataloger", subjects=[record["artifact_ref"] if old != item else self.work_records[wid]["artifact_ref"], execution])
                    if current:
                        self.source_docs.pop(current["artifact_ref"], None)
                    self.source_records[f"abstract/{wid}"] = source
                    self.source_docs[source["artifact_ref"]] = body
        return added

    def _bibliographic_call(self, operation, *, role, query=None, work_id=None, cursor=None,
                            plan_ref=None, admission="discovery"):
        self._ensure_active()
        # The discovery/expansion budget must not consume the calls reserved
        # for the independent challenge.  ``challenge_reserve`` already
        # protects work admission; the same revision-5 policy reserves one
        # full challenge query tranche so that a saturated discovery campaign
        # can still execute the counter-search.  Challenge calls are therefore
        # allowed through the base cap plus the configured planner width,
        # while every call remains charged in the immutable reservation ledger.
        api_limit = self.bounds["max_api_calls"]
        if admission == "challenge":
            api_limit += max(1, self.bounds.get("queries_per_role", 1))
        if self.api_calls >= api_limit:
            self.gaps.append({"kind": "api_call_limit", "operation": operation, "query": query, "work_id": work_id})
            return None
        arguments = {"operation": operation, "query": query, "work_id": work_id,
                     "limit": self.bounds["results_per_query"], "cursor": cursor}
        if self.bibliography_mode == "crossref":
            if operation == "citing":
                self.gaps.append({"kind": "bibliography_fallback_unsupported", "operation": operation,
                                  "work_id": work_id, "provider": "crossref"})
                return None
            return self._crossref_bibliographic_call(arguments, role=role, plan_ref=plan_ref, admission=admission)
        self._wait_provider("bibliography")
        self._reserve_api_call("bibliography", arguments, role)
        try:
            result, execution = self.operations.run(self.bindings["bibliography"], arguments, self._call, operator=role)
        except Exception as exc:
            self._ensure_active()
            self.gaps.append({"kind": "bibliographic_failure", "request": arguments, "error": str(exc)})
            outcome = self._failure_outcome(self.score["bibliography"]["id"])
            fallback_outcomes = {"rate_limited", "timeout", "provider_error", "auth_required", "access_denied", 408, 425, 429, 500, 502, 503, 504}
            if (operation in {"search", "work"} and self.score.get("identity")
                    and outcome in fallback_outcomes):
                self._activate_crossref_fallback(outcome)
                if operation == "search" or self.works.get(work_id, {}).get("doi"):
                    return self._crossref_bibliographic_call(arguments, role=role, plan_ref=plan_ref, admission=admission)
                self.gaps.append({"kind": "bibliography_fallback_unsupported", "operation": operation,
                                  "work_id": work_id, "provider": "crossref"})
                return None
            if operation in {"work", "citing"} and outcome in fallback_outcomes:
                return None
            raise
        added = self._ingest(result["works"], execution, admission=admission)
        metadata = result["metadata"]
        body = {"request": arguments, "role": role, "execution_ref": execution, "plan_ref": plan_ref,
                "returned_work_ids": [w["id"] for w in result["works"]], "new_unique_works": added,
                "count": metadata["count"], "next_cursor": metadata["next_cursor"], "has_more": metadata["has_more"]}
        record = self._publish(f"kb/queries/{self.api_calls}", "query_record", body, role,
                               subjects=[execution, *([plan_ref] if plan_ref else [])])
        self.query_refs.append(record["artifact_ref"])
        self.search_log.append(body)
        self._update_register()
        self._checkpoint("literature_captured", force=True)
        return body

    def _search(self, queries, role, plan_ref=None, *, admission="discovery"):
        seen = ({normalized(row["request"]["query"]) for row in self.search_log
                 if (row.get("request", {}).get("operation") == "search"
                     and row["request"].get("query")
                     and (admission != "challenge" or row.get("plan_ref") == plan_ref))}
                if self.resume_session else set())
        for query in queries:
            if normalized(query) in seen:
                continue
            seen.add(normalized(query))
            self._bibliographic_call("search", role=role, query=query, plan_ref=plan_ref,
                                     admission=admission)

    def _expand(self):
        quiet = 0
        for number in range(self.bounds["expansion_rounds"]):
            before = len(self.works)
            seeds = [wid for wid in self.works if wid not in self.expanded][:self.bounds["expansion_seed_count"]]
            if not seeds:
                break
            completed = True
            for wid in seeds:
                self.expanded.add(wid)
                for reference in self.works[wid]["referenced_works"][:self.bounds["references_per_work"]]:
                    if reference not in self.aliases:
                        completed &= self._bibliographic_call("work", role="research.citation-tracer", work_id=reference) is not None
                completed &= self._bibliographic_call("citing", role="research.citation-tracer", work_id=wid) is not None
            new = len(self.works) - before
            quiet = quiet + 1 if completed and new < self.bounds["min_new_works"] else 0
            self.expansion_log.append({"round": number + 1, "seed_work_ids": seeds, "new_unique_works": new,
                                       "completed": completed, "quiet_rounds": quiet})
            if quiet >= self.bounds["saturation_rounds"]:
                break

    def _full_texts(self):
        if "full_text" not in self.bindings:
            return
        routes = [(route, False) for route in self.score["full_text_sources"]]
        # A free-topic run may discover Crossref records after its initial
        # route list was authored.  Use the verified catalog URLs as additional
        # candidates so a stale seed route cannot cap the entire full-text
        # campaign.  The route remains a normal bounded source capture; only
        # its provenance is marked locally as auto-discovered.
        if self.bibliography_mode == "crossref":
            known = {route.get("work_id") for route, _ in routes if isinstance(route, dict)}
            for work in self.works.values():
                wid = work.get("work_id")
                if not isinstance(wid, str) or wid in known:
                    continue
                locations = work.get("locations") or []
                location = next((item for item in locations if isinstance(item, dict)
                                 and (item.get("pdf_url") or item.get("landing_page_url"))), None)
                url = ((location.get("pdf_url") or location.get("landing_page_url")) if location else None)
                url = url or work.get("source_url")
                if not isinstance(url, str) or not url.strip():
                    doi = work.get("doi")
                    url = "https://doi.org/" + doi if isinstance(doi, str) and doi.strip() else None
                if not isinstance(url, str) or not url.strip():
                    continue
                routes.append(({
                    "work_id": wid,
                    "title": work.get("title") or wid,
                    "url": url,
                    "section_markers": ["Introduction"],
                }, True))
                known.add(wid)
                if len(routes) >= max(10, self.bounds["max_full_texts"] * 2):
                    break
        for index, (route, auto_discovered) in enumerate(routes):
            wid = self.aliases.get(route["work_id"], route["work_id"])
            if wid not in self.works or wid in self.full_text_attempted:
                continue
            if len(self.full_text_attempted) >= self.bounds["max_full_texts"]:
                self.gaps.append({"kind": "full_text_limit", "work_id": wid})
                break
            self.full_text_attempted.add(wid)
            try:
                self._wait_provider("full_text")
                result, execution = self.operations.run(self.bindings["full_text"],
                    {"url": route["url"], "max_length": self.bounds["max_text_chars"]}, self._call,
                    operator="research.full-text-reader")
            except Exception as exc:
                self._ensure_active()
                self.gaps.append({"kind": "full_text_failure", "work_id": wid, "reason": str(exc)})
                self.bindings.pop("full_text", None)
                # Operations deliberately degrades a capability after a bad
                # workload.  If more bounded routes remain, re-probe and
                # re-bind the same pinned capability before trying the next
                # source; one paywalled or malformed landing page should not
                # discard otherwise reachable open literature.
                if index + 1 >= len(routes):
                    break
                try:
                    capability_id = self.score["full_text"]["id"]
                    state = self.operations.ensure_ready(
                        capability_id, self._call,
                        operator="research.full-text-reader.recovery",
                        verifier="operations.verifier.full-text-recovery",
                        purpose="Recover the verified full-text route after a bounded workload failure",
                    )
                    if state.get("state") != "ready":
                        break
                    self.bindings["full_text"] = state["binding"]
                    self.operations.idle(capability_id)
                except Exception as recovery_exc:
                    self._ensure_active()
                    self.gaps.append({"kind": "full_text_recovery_failure", "work_id": wid,
                                      "reason": str(recovery_exc)})
                    break
                continue
            text = result["text"]
            title_match = normalized(self.works[wid]["title"]) == normalized(route["title"]) and normalized(route["title"]) in normalized(text)
            sections_match = all(normalized(marker) in normalized(text) for marker in route["section_markers"])
            verified = title_match and sections_match and not any(result["metadata"].get(key) for key in ("capture_truncated", "capture_incomplete"))
            body = {"work_id": wid, "representation": "full_text" if verified else "unverified_text",
                    "text": text, "url": route["url"], "execution_ref": execution, "identity_verified": verified,
                    "identity_checks": {"title_match": title_match, "section_markers": route["section_markers"] if sections_match else []}}
            record = self._publish(f"kb/full-text/{wid}", "source_capture", body, "methods.source-verifier",
                                   subjects=[execution, self.work_records[wid]["artifact_ref"]])
            self.source_records[f"full_text/{wid}"] = record
            self.source_docs[record["artifact_ref"]] = body
            if not verified:
                self.gaps.append({"kind": "full_text_identity_or_scope", "work_id": wid})
            self._update_register()

    def _reconcile_identities(self):
        if "identity" not in self.bindings:
            return
        for wid, work in list(self.works.items()):
            if not work.get("doi") or wid in self.identity_records:
                continue
            if self.api_calls >= self.bounds["max_api_calls"]:
                self.gaps.append({"kind": "identity_call_limit", "work_id": wid})
                break
            arguments = {"query": work["doi"], "limit": 3}
            self._wait_provider("identity")
            self._reserve_api_call("identity", arguments, "research.identity-checker")
            try:
                result, execution = self.operations.run(
                    self.bindings["identity"], arguments, self._call,
                    operator="research.identity-checker")
            except Exception as exc:
                self._ensure_active()
                self.gaps.append({"kind": "identity_lookup_failure", "work_id": wid, "reason": str(exc)})
                self.bindings.pop("identity", None)
                break
            identity = reconcile_result(work, self.work_records[wid]["artifact_ref"], result, execution)
            record = self._record(f"kb/identities/{wid}", "reference_card", identity,
                                  "research.identity-checker",
                                  subjects=[self.work_records[wid]["artifact_ref"], execution])
            self.identity_records[wid] = record
            status = identity["status"]
            if status in {"conflicted", "insufficient_evidence"}:
                self.gaps.append({"kind": "bibliographic_identity_" + status, "work_id": wid,
                                  "identity_ref": record["artifact_ref"]})
        self._update_register()

    def _update_register(self):
        body = {"work_refs": [r["artifact_ref"] for r in self.work_records.values()],
                "source_refs": list(self.source_docs), "query_refs": list(self.query_refs),
                "identity_refs": [r["artifact_ref"] for r in self.identity_records.values()], "aliases": self.aliases}
        self.register_ref = self._record("kb/work-register", "note", body, "research.cataloger")["artifact_ref"]

    def _source_context(self):
        return [{"source_ref": ref, "work_id": value["work_id"], "representation": value["representation"],
                 "text": value["text"][:self.bounds["context_chars"]], "available_chars": len(value["text"]),
                 "window": {"start": 0, "end": min(len(value["text"]), self.bounds["context_chars"])}}
                for ref, value in self.source_docs.items()]

    def _assessment_source_context(self):
        """Bound source windows for the final gap decision.

        The assessment receives the compact map plus enough source text to
        check decisive quotations.  A short prefix is not a safe evidence
        boundary: a verifier can identify the right passage in the captured
        paper and then fail closed simply because that passage occurs later
        in the paper.  Send a complete verified paper when it fits the
        configured capture budget; retain a bounded prefix only for unusually
        large captures where a full request would exceed the model context.
        """
        result = []
        for ref, value in self.source_docs.items():
            representation = value["representation"]
            limit = self.bounds["context_chars"]
            if representation == "full_text":
                # Full-text evidence is the decisive input to eligibility and
                # refutation.  The current configured capture is at most
                # 999,999 characters; use the whole document up to a
                # conservative 100k request budget so equations and methods
                # near the end remain bindable.  Larger documents stay
                # bounded and must be treated as insufficient evidence.
                limit = min(self.bounds["max_text_chars"], 100000)
            elif representation == "unverified_text":
                # Keep enough of captured pages to retain the exact spans
                # surfaced by the map, while still avoiding the full HTML
                # payload that caused the assessment request to overflow.
                limit = min(limit, 40000)
            elif representation == "abstract":
                limit = min(limit, 2500)
            text = value["text"][:limit]
            result.append({"source_ref": ref, "work_id": value["work_id"],
                           "representation": representation,
                           "identity_verified": value.get("identity_verified", False),
                           "text": text,
                           "available_chars": len(value["text"]),
                           "window": {"start": 0, "end": len(text)}})
        return result

    @staticmethod
    def _assessment_statement(statement):
        if not isinstance(statement, dict):
            return {"text": None, "evidence": []}
        return {"text": statement.get("text"), "evidence": [
            {key: proof.get(key) for key in ("work_id", "source_ref", "quote")
             if proof.get(key) is not None}
            for proof in statement.get("evidence", []) if isinstance(proof, dict)
        ]}

    def _assessment_map_context(self):
        """Project map claims to evidence needed for gap reasoning."""
        entries = []
        for record in self.analysis_records.values():
            entry = self._body(record)
            entries.append({"work_id": entry.get("work_id"),
                            "inclusion": entry.get("inclusion"),
                            "reason": entry.get("reason"),
                            **{field: self._assessment_statement(entry.get(field))
                               for field in MAP_FIELDS}})
        relationships = []
        for relation in self.relationships.values():
            relationships.append({"source": relation.get("source"),
                                  "target": relation.get("target"),
                                  "kind": relation.get("kind"),
                                  "claim": self._assessment_statement(relation.get("claim"))})
        return {"entries": entries, "relationships": relationships}

    def _bind_assessment_spans(self, value, sources):
        """Bind quotes and repair an unambiguous abstract/full-text mismatch.

        A map quote can be copied from an abstract while a reviewer selects
        the same work's full-text source reference.  Rebinding is allowed only
        when the exact quote occurs once in another displayed source for that
        work; unsupported or ambiguous quotes still fail the normal evidence
        validator and are sent back for a scoped retry.
        """
        source_by_ref = {source["source_ref"]: source for source in sources}
        by_work = {}
        for source in sources:
            by_work.setdefault(source["work_id"], []).append(source)
        repaired = deepcopy(value)

        def repair(items):
            for item in items:
                if not isinstance(item, dict):
                    continue
                quote, ref, work_id = item.get("quote"), item.get("source_ref"), item.get("work_id")
                if not all(isinstance(part, str) for part in (quote, ref, work_id)):
                    continue
                source = source_by_ref.get(ref)
                visible = source["text"] if source is not None else ""
                if visible.count(quote) == 1:
                    continue
                # First try the displayed representation itself.  The source
                # binder can restore line breaks and typographic punctuation
                # while retaining the full-text source reference, which is
                # required for a decisive assessment.
                if source is not None:
                    try:
                        bound = self._bind_visible_spans({"evidence": [item]}, [source])
                        item.clear()
                        item.update(bound["evidence"][0])
                        continue
                    except ValidationError:
                        pass
                candidates = [candidate for candidate in by_work.get(work_id, [])
                              if candidate["text"].count(quote) == 1]
                if len(candidates) == 1:
                    item["source_ref"] = candidates[0]["source_ref"]

        repair(repaired.get("evidence", []))
        for comparison in repaired.get("comparisons", []):
            if isinstance(comparison, dict):
                repair(comparison.get("evidence", []))
        return self._bind_visible_spans(repaired, sources)

    def _bind_visible_spans(self, value, sources):
        windows = {source["source_ref"]: source["window"] for source in sources}
        return bind_source_spans(value, self.source_docs, windows=windows)

    def _coverage(self):
        return {"unique_works": len(self.works), "abstracts": sum(w.get("abstract") is not None for w in self.works.values()),
            "verified_full_texts": sum(s["representation"] == "full_text" for s in self.source_docs.values()),
            "bibliographic_identities": {"checked": len(self.identity_records),
                "verified": sum(self._body(record)["status"] in {"verified", "verified_with_gaps"}
                                for record in self.identity_records.values()),
                "conflicted": sum(self._body(record)["status"] == "conflicted"
                                  for record in self.identity_records.values())},
            "searches": self.search_log, "expansion": self.expansion_log, "access_and_limit_gaps": self.gaps,
            "pagination_remaining": any(query["has_more"] for query in self.search_log),
            "saturated": bool(self.expansion_log and self.expansion_log[-1]["quiet_rounds"] >= self.bounds["saturation_rounds"]),
            "scope": "Recorded finite queries and citation expansion; no exhaustive-coverage claim",
            "source_windows": [{"source_ref": item["source_ref"], "available_chars": item["available_chars"], "window": item["window"]}
                               for item in self._source_context()]}

    def _map(self):
        requested = []
        basis = {}
        for wid, work in self.work_records.items():
            basis[wid] = [work["artifact_ref"], *([self.identity_records[wid]["artifact_ref"]]
                          if wid in self.identity_records else []),
                          *[ref for ref, source in self.source_docs.items() if source["work_id"] == wid]]
            previous = self._body(self.analysis_records[wid]) if wid in self.analysis_records else None
            relationships = [relation for relation in self.relationships.values() if relation["source"] == wid]
            if (self.analyzed_basis.get(wid) != basis[wid] or previous is None
                    or contains_legacy(previous) or contains_legacy(relationships)):
                requested.append(wid)
        changed = set(requested)
        for relationship in self.relationships.values():
            if (relationship["source"] not in requested and (relationship["target"] in changed or any(
                    proof["source_ref"] not in self.source_docs for proof in relationship["claim"]["evidence"]))):
                requested.append(relationship["source"])
        if requested:
            self._models_checked([self._map_job(wid, basis[wid]) for wid in requested])
        edges = []
        for wid, work in self.works.items():
            for other in work["referenced_works"]:
                target = self.aliases.get(other, other)
                if target in self.works:
                    edges.append({"source": wid, "target": target, "kind": "cites"})
        self.map_record = self._record("kb/literature-map", "note", {
            "question": self.score["question"], "entry_refs": [r["artifact_ref"] for r in self.analysis_records.values()],
            "relationship_refs": [r["artifact_ref"] for r in self.relationships.values()], "citation_edges": edges,
            "publication_metadata_status": "provider_reported_with_separate_identity_reconciliation",
        }, "research.literature-mapper", subjects=[self.register_ref])

    def _map_job(self, wid, basis, *, review_feedback=None):
        sources = [source for source in self._source_context()
                   if source["work_id"] == wid or source["representation"] == "abstract"]
        own_sources = [source for source in sources if source["work_id"] == wid]
        visible_sources = {source["source_ref"]: source for source in sources}
        previous = json.loads(self.store.read_body(self.analysis_records[wid]["body_hash"])) if wid in self.analysis_records else None
        old_relationships = [relation for relation in self.relationships.values() if relation["source"] == wid]
        entry_editable = (self.analyzed_basis.get(wid) != basis or contains_legacy(previous)
                          or contains_legacy(old_relationships))
        if review_feedback is not None:
            entry_editable = bool(review_feedback["entry_fields"])
        assignment = {
            "assignment": "Assess the single assigned work and propose supported outgoing conceptual connections.", "phase": "map",
            "question": self.score["question"], "requested_work_ids": [wid],
            "works": [{**{key: work[key] for key in ("id", "title", "year", "doi", "publication_metadata_status")},
                       "identity_ref": self.identity_records[work["id"]]["artifact_ref"] if work["id"] in self.identity_records else None,
                       "identity_status": self._body(self.identity_records[work["id"]])["status"] if work["id"] in self.identity_records else "not_checked"}
                      for work in self.works.values()],
            "previous_entries": [previous] if previous is not None else [], "entry_editable": entry_editable,
            "previous_affected_relationships": old_relationships,
            "semantic_feedback": review_feedback,
            "relationship_semantics": RELATIONSHIP_SEMANTICS,
            "sources": sources,
            "instructions": "Return exactly {entries:[{work_id:string,inclusion:string,reason:string,problem:Statement,approach:Statement,finding:Statement,limitations:Statement}],"
                "relationships:[{source:string,target:string,kind:string,claim:Statement}]}. entries must contain exactly the assigned work. "
                "inclusion is included/excluded/uncertain. reason is a plain string, never an evidence object. "
                "Only problem, approach, finding, limitations, and relationship claim use Statement={text:string|null,evidence:[{work_id:string,source_ref:string,quote:string}]}. "
                "For unknown facts return {text:null,evidence:[]}. Every substantive statement needs a short contiguous exact quote, usually 3-12 words, from the displayed text. "
                "Each quote must occur exactly once in its displayed source window; make it longer when repeated text would be ambiguous. "
                "Every clause must be supported. Report limitations only when the source states them explicitly; an abstract's silence cannot establish an untested domain or omitted comparison. "
                "Copy quote characters exactly, including whitespace, Markdown, punctuation, and literal escaped newline characters; do not paraphrase, normalize, or repair quotations. "
                "Use only displayed source_ref values. Each entry statement must cite the assigned work. "
                "Each relationship source must be the assigned work; target must be a different known work. "
                "kind is extends/contradicts/compares/related; claim needs evidence from BOTH works. Omit unsupported relationships. "
                "Other works are supplied for comparison, not for editing. Preserve supported previous fields and outgoing relationships. "
                "If entry_editable is false, copy the previous entry exactly without changing any field; only its outgoing relationships may change. "
                "When semantic_feedback is present, change only its entry_fields and relationships with its relationship_targets. "
                "Preserve every other field and relationship exactly. Correct unsupported claims by narrowing them to the evidence, recording unknown facts, or removing unsupported relationships. "
                "Titles, years, and citation links are provider-reported catalog metadata, not textual evidence for substantive claims. "
                "Do not infer confirmed chronology, conceptual inheritance, identity claims, or superiority from metadata or shared terminology alone. "
                "When no captured source belongs to the assigned work, set inclusion to uncertain and make reason a narrow availability note: state that the catalog record is relevant by metadata but no abstract or verified full text was available, so substantive content could not be assessed. Do not put other work IDs, quotations, chronology, evolution, extension, comparison, or superiority in that reason. "
                "Keep each statement text concise (at most 240 characters), return at most one outgoing relationship, and omit any relationship that is not directly supported by both displayed works. Return only the requested JSON object."
        }
        if review_feedback is not None:
            editable_fields = list(review_feedback["entry_fields"])
            editable_targets = list(review_feedback["relationship_targets"])
            assignment.update({
                "response_contract": "scoped_patch",
                "editable_entry_fields": editable_fields,
                "editable_relationship_targets": editable_targets,
                "instructions":
                    "Return exactly {entry_updates:{field:value},relationships:[{source,target,kind,claim}]}. "
                    "entry_updates must contain exactly the editable_entry_fields and no complete entry. "
                    "For inclusion use included/excluded/uncertain; reason is a plain string; problem, approach, finding, and limitations use "
                    "Statement={text:string|null,evidence:[{work_id:string,source_ref:string,quote:string}]}. "
                    "relationships contains only replacements for editable_relationship_targets; an omitted editable target deletes its old relationship. "
                    "Never return unchanged fields or relationships because the control plane retains them from their accepted versions. "
                    "Every substantive statement needs a short contiguous exact quote from the displayed source, with every clause supported. "
                    "Relationship source must be the assigned work, target must be editable, kind is extends/contradicts/compares/related, "
                    "and its claim needs evidence from both works. Use only displayed source_ref values. "
                    "If no captured source belongs to the assigned work, the only valid repair for inclusion/reason is inclusion=uncertain with a narrow note that catalog metadata is relevant but no abstract or verified full text was available, so substantive content could not be assessed; remove other work IDs, quotations, chronology, evolution, extension, comparison, and superiority from that reason. "
                    "Correct the failed checks narrowly by grounding, narrowing, setting unknown, or deleting an unsupported relationship."
            })

        # A catalog-only record cannot support substantive prose.  Asking a
        # model to restate that negative evidence repeatedly creates a
        # pointless retry loop: the model can invent a broader availability
        # explanation even though the validator must reject it.  Project the
        # deterministic, metadata-only state locally and reserve model calls
        # for assignments that actually contain source text.
        source_less_reason = (
            "The catalog record is relevant by metadata, but no abstract or verified full text was available, "
            "so substantive content could not be assessed."
        )
        null_statement = {"text": None, "evidence": []}

        def source_less_value(_value):
            if review_feedback is not None:
                updates = {}
                for field in review_feedback["entry_fields"]:
                    if field == "inclusion":
                        updates[field] = "uncertain"
                    elif field == "reason":
                        updates[field] = source_less_reason
                    elif field in MAP_FIELDS:
                        updates[field] = deepcopy(null_statement)
                    else:
                        raise ValidationError(f"unsupported source-less repair field: {field}")
                return {"entry_updates": updates, "relationships": []}
            return {
                "entries": [{
                    "work_id": wid,
                    "inclusion": "uncertain",
                    "reason": source_less_reason,
                    "problem": deepcopy(null_statement),
                    "approach": deepcopy(null_statement),
                    "finding": deepcopy(null_statement),
                    "limitations": deepcopy(null_statement),
                }],
                "relationships": [],
            }

        def normalize(value):
            if not own_sources:
                return source_less_value(value)
            return self._bind_visible_spans(value, sources)

        effective = {}
        def validate(value):
            if review_feedback is not None:
                value = apply_scoped_map_repair(wid, previous, old_relationships, review_feedback, value)
            validate_map(value, [wid], set(self.works), self.source_docs, require_spans=True)
            if not own_sources:
                entry = value["entries"][0]
                reason = entry["reason"].lower()
                forbidden = set(self.works) - {wid}
                if entry["inclusion"] != "uncertain":
                    raise ValidationError("a work without captured source text must be marked uncertain")
                if ("abstract" not in reason or "full text" not in reason
                        or not any(token in reason for token in ("could not", "cannot", "not available", "unavailable"))
                        or any(other.lower() in reason for other in forbidden)
                        or any(token in reason for token in ("evolved", "extends", "superior", "foundational", "chronolog"))):
                    raise ValidationError("a source-less work reason must report only metadata relevance and unavailable abstract/full text")
            if not entry_editable and canonical_bytes(value["entries"][0]) != canonical_bytes(previous):
                raise ValidationError(f"unchanged work {wid} must preserve its exact previous entry; only outgoing relationships may change")
            if any(relation["source"] != wid for relation in value["relationships"]):
                raise ValidationError(f"relationship source must be its assigned owner {wid}")
            effective["value"] = value

        def integrate(value, execution):
            value = effective["value"]
            entry = value["entries"][0]
            self.analysis_records[wid] = self._record(f"kb/work-analyses/{wid}", "note", entry,
                "research.literature-mapper", subjects=[execution, *basis])
            self.analyzed_basis[wid] = basis
            self.relationships = {key: relation for key, relation in self.relationships.items() if relation["source"] != wid}
            for relationship in value["relationships"]:
                key = "-".join(relationship[field] for field in ("source", "target", "kind"))
                record = self._record(f"kb/relationships/{key}", "note", relationship,
                    "research.literature-mapper", subjects=[execution, *[proof["source_ref"] for proof in relationship["claim"]["evidence"]]])
                self.relationships[key] = {**relationship, "artifact_ref": record["artifact_ref"]}

        return {"name": f"map-{wid}", "actor": "research.literature-mapper", "assignment": assignment,
                # Preserve the mission's configured inference profile for
                # source binding as well.  A compute-rich run may deliberately
                # spend the same reasoning and output budget on every
                # scientific role; a provider that does not expose a profile
                # simply receives no override here.
                "model_overrides": {
                    "max_output_tokens": int(self.config["model"]["max_output_tokens"]),
                    **({"reasoning_effort": self.config["model"]["reasoning_effort"]}
                       if self.config["model"].get("reasoning_effort") is not None else {}),
                },
                "normalizer": normalize,
                "validator": validate, "on_valid": integrate}

    def _map_body(self):
        return {"entries": [json.loads(self.store.read_body(r["body_hash"])) for r in self.analysis_records.values()],
                "relationships": list(self.relationships.values()),
                **json.loads(self.store.read_body(self.map_record["body_hash"]))}

    def _survey_review_packet(self):
        """Build a bounded, text-only context for the aggregate survey review.

        Focused reviews already inspect the exact captured spans for every map
        entry and relationship.  Sending the complete search log and source
        bodies again makes the aggregate request needlessly large and can
        trigger gateway failures.  The aggregate reviewer therefore receives
        the map statements, evidence references, source inventory, coverage
        counters, and the independent focused-review outcomes; the immutable
        source captures remain pinned in the survey dependencies.
        """
        def compact_statement(statement):
            if not isinstance(statement, dict):
                return {"text": None, "evidence": []}
            proofs = []
            for proof in statement.get("evidence", []):
                if not isinstance(proof, dict):
                    continue
                proofs.append({key: proof.get(key) for key in ("work_id", "source_ref", "quote_sha256")
                               if proof.get(key) is not None})
            return {"text": statement.get("text"), "evidence": proofs}

        entries = []
        for record in self.analysis_records.values():
            entry = json.loads(self.store.read_body(record["body_hash"]))
            entries.append({
                "work_id": entry.get("work_id"),
                "inclusion": entry.get("inclusion"),
                "reason": entry.get("reason"),
                **{field: compact_statement(entry.get(field)) for field in MAP_FIELDS},
            })

        relationships = []
        for relation in self.relationships.values():
            claim = relation.get("claim") or {}
            relationships.append({
                "source": relation.get("source"),
                "target": relation.get("target"),
                "kind": relation.get("kind"),
                "claim": compact_statement(claim),
            })

        coverage = self._coverage()
        search_counts = {}
        for row in coverage.get("searches", []):
            if not isinstance(row, dict):
                continue
            outcome = row.get("outcome", "unknown")
            search_counts[outcome] = search_counts.get(outcome, 0) + 1
        gap_counts = {}
        for row in coverage.get("access_and_limit_gaps", []):
            if not isinstance(row, dict):
                continue
            kind = row.get("kind", "unknown")
            gap_counts[kind] = gap_counts.get(kind, 0) + 1
        coverage_summary = {
            key: coverage.get(key) for key in (
                "unique_works", "abstracts", "verified_full_texts", "bibliographic_identities",
                "source_windows", "pagination_remaining", "saturated",
            )
        }
        coverage_summary.update({
            "search_count": len(coverage.get("searches", [])),
            "search_outcomes": search_counts,
            "gap_count": len(coverage.get("access_and_limit_gaps", [])),
            "gap_kinds": gap_counts,
        })

        sources = []
        for source in self._source_context():
            sources.append({key: source.get(key) for key in (
                "source_ref", "work_id", "representation", "identity_verified", "available_chars",
            )})

        focused_reviews = []
        for wid, record in self.work_reviews.items():
            review = self._body(record)
            focused_reviews.append({
                "work_id": wid,
                "checks": [{"check_id": check.get("check_id"), "outcome": check.get("outcome")}
                           for check in review.get("checks", [])],
                "rationale": str(review.get("rationale", ""))[:800],
            })
        return {
            "map": {"entries": entries, "relationships": relationships},
            "coverage": coverage_summary,
            "sources": sources,
            "focused_review_summary": focused_reviews,
        }

    def _review_work_claims(self):
        repair_mode = self.config["limits"].get("repair_mode", "bounded")
        rounds = (itertools.count() if repair_mode == "until_deadline"
                  else range(self.config["limits"]["max_rounds"]))
        for round_number in rounds:
            self._ensure_active()
            jobs, rejected = [], []
            for wid, entry_record in self.analysis_records.items():
                relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
                refs = [relation["artifact_ref"] for relation in relations]
                source_ids = {wid, *[relation["target"] for relation in relations]}
                sources = [source for source in self._source_context() if source["work_id"] in source_ids]
                basis = [entry_record["artifact_ref"], *refs, *[source["source_ref"] for source in sources]]
                if self.reviewed_basis.get(wid) == basis:
                    continue
                entry = json.loads(self.store.read_body(entry_record["body_hash"]))
                assignment = {
                    "phase": "work_review", "assignment": "Independently audit the entailment of each individual literature claim.",
                    "question": self.score["question"], "entry_ref": entry_record["artifact_ref"],
                    "entry": entry, "relationship_refs": refs, "relationships": relations,
                    "relationship_semantics": RELATIONSHIP_SEMANTICS,
                    "sources": sources, "required_checks": list(work_review_checks(refs)),
                    "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
                    "instructions": "Return exactly {checks:[{check_id,outcome,method,result}],rationale:string}. "
                        "Run each required check separately; outcome is passed/failed/insufficient_evidence/check_failed. "
                        "Judge whether the supplied text entails the ENTIRE claim, not whether its quotation merely exists or the topic sounds plausible. "
                        "A passed check requires support for every clause. Fail unsupported minor clauses too; a correct main point does not excuse them. "
                        "For limitations require an explicit source statement; reject a claim about missing evaluation or excluded scope inferred only from an abstract's silence. "
                        "An abstract's silence cannot establish what a full paper did not evaluate, include, compare, or generalize. "
                        "Attention-based does not entail attention-only or absence of recurrence/convolution. "
                        "A newer date, citation, shared terminology, or similar application does not establish extension, conceptual inheritance, or superiority. "
                        "For a claimed extends relationship require text establishing the specific dependency; otherwise fail it and identify the unsupported part. "
                        "Do not fill missing text from model memory or titles. A proceedings preface is not architectural research evidence. "
                        "Check inclusion and reason against actual scope and source content. Check null statements as explicit abstentions; those may pass. "
                        "A claim may be scientifically plausible yet unsupported by these sources. Fail each unsupported assertion and state the narrowest evidence-grounded correction."
                }
                def integrate(value, execution, *, wid=wid, basis=basis, entry_ref=entry_record["artifact_ref"], refs=refs, relations=relations):
                    record = self._publish(f"kb/work-reviews/{wid}", "note", {
                        "entry_ref": entry_ref, "relationship_refs": refs, "execution_ref": execution, **value},
                        "methods.work-reviewer", subjects=[entry_ref, *refs, execution])
                    self.work_reviews[wid] = record
                    failed = [check for check in value["checks"] if check["outcome"] != "passed"]
                    if not failed:
                        self.reviewed_basis[wid] = basis
                        return
                    self.reviewed_basis.pop(wid, None)
                    by_ref = {relation["artifact_ref"]: relation for relation in relations}
                    fields = [check["check_id"] for check in failed if not check["check_id"].startswith("relationship:")]
                    targets = sorted({by_ref[check["check_id"][len("relationship:"):]]["target"]
                                      for check in failed if check["check_id"].startswith("relationship:")})
                    rejected.append((wid, {"review_ref": record["artifact_ref"], "entry_fields": fields,
                                           "relationship_targets": targets, "checks": failed, "rationale": value["rationale"]}))
                jobs.append({"name": f"work-review-{wid}", "actor": "methods.work-reviewer", "assignment": assignment,
                             "validator": lambda value, refs=refs: validate_work_review(value, refs), "on_valid": integrate})
            if jobs:
                self._models_checked(jobs, stage="unit_review", task_kind="verification")
            if not rejected:
                return
            if repair_mode != "until_deadline" and round_number + 1 == self.config["limits"]["max_rounds"]:
                raise ValidationError("focused literature review remains unresolved: " + ", ".join(wid for wid, _ in rejected))
            self._models_checked([self._map_job(wid, self.analyzed_basis[wid], review_feedback=feedback)
                                  for wid, feedback in rejected], stage="revision")
            self._map()

    def _accept_survey(self):
        if not self.works:
            raise ValidationError("no works were captured; a survey cannot be fabricated")
        self._reconcile_identities()
        self._map()
        self._review_work_claims()
        coverage = self._record("kb/coverage", "coverage_report", self._coverage(), "command.search-coordinator",
                                subjects=[self.register_ref, *self.query_refs])
        dependencies = [self.score_ref, self.protocol["artifact_ref"], self.map_record["artifact_ref"], coverage["artifact_ref"],
            self.register_ref, *[r["artifact_ref"] for r in self.work_records.values()], *self.source_docs, *self.query_refs,
            *[r["artifact_ref"] for r in self.identity_records.values()],
            *[ref for r in self.identity_records.values() for ref in self._body(r).get("observation_refs", [])],
            *[self._body(r)["lookup_execution_ref"] for r in self.identity_records.values()
              if self._body(r).get("lookup_execution_ref")],
            *[r["artifact_ref"] for r in self.analysis_records.values()], *[r["artifact_ref"] for r in self.relationships.values()],
            *[r["artifact_ref"] for r in self.work_reviews.values()],
            *[json.loads(self.store.read_body(r["body_hash"]))["execution_ref"] for r in self.work_reviews.values()],
            *[source["execution_ref"] for source in self.source_docs.values()]]
        body = {"schema_version": "literature-survey-3", "score_ref": self.score_ref, "protocol_ref": self.protocol["artifact_ref"],
                "map_ref": self.map_record["artifact_ref"], "coverage_ref": coverage["artifact_ref"],
                "source_refs": list(self.source_docs), "work_refs": [r["artifact_ref"] for r in self.work_records.values()],
                "query_refs": list(self.query_refs),
                "identity_refs": [r["artifact_ref"] for r in self.identity_records.values()],
                "dependency_refs": list(dict.fromkeys(dependencies))}
        body["work_review_refs"] = [r["artifact_ref"] for r in self.work_reviews.values()]
        bundle = self._publish("kb/surveys/current", "note", body, "research.literature-mapper", subjects=body["dependency_refs"])
        self.survey_revision += 1
        review_packet = self._survey_review_packet()
        value, execution = self._model_checked("survey-review", "methods.survey-reviewer", {
            "assignment": "Independently check this exact survey, including honest reporting of incomplete coverage.",
            "phase": "survey_review", "survey_ref": bundle["artifact_ref"], "question": self.score["question"],
            "map": review_packet["map"], "coverage": review_packet["coverage"],
            "sources": review_packet["sources"],
            "focused_review_summary": review_packet["focused_review_summary"],
            "relationship_semantics": RELATIONSHIP_SEMANTICS,
            "required_checks": list(SURVEY_CHECKS),
            "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
            "instructions": "Return {checks:[{check_id,outcome,method,result}],rationale}. Execute exactly the required checks. "
                "Outcomes passed/failed/insufficient_evidence/check_failed. Passing approves a faithful bounded survey, not novelty or exhaustive coverage. "
                "Check accurate coverage/accounting, faithful quotations and source scope, and support for every map claim. "
                "The focused-review summary records independent exact-span checks; use it as the primary support for map claims. "
                "The sources list is an inventory only and intentionally contains no source body or image bytes; do not infer text that is not represented. "
                "Unknown facts must stay unknown. Unverified provider metadata is not itself a false assertion if explicitly labeled; "
                "fail unsupported chronology or superiority inferred from it."
        }, validate_survey_review, stage="unit_review", task_kind="verification")
        review = self._publish(f"kb/survey-reviews/{self.survey_revision}", "note", {
            "survey_ref": bundle["artifact_ref"], "execution_ref": execution, **value}, "methods.survey-reviewer",
            subjects=[bundle["artifact_ref"], execution])
        accepted = self.store.accepted(bundle["artifact_id"])
        adopted = self.gate.accept(bundle["artifact_ref"], review["artifact_ref"], author="strategy.survey-integrator",
                                  expected_version=accepted["version"] if accepted else None, guard=self._admission_guard)
        self.survey_ref = adopted["artifact_ref"]
        self.incumbent = self.survey_ref
        self.time_policy.mark_first_verified_result(self.survey_ref)
        self.verified_changes.append({"kind": "accepted_literature_survey", "ref": self.survey_ref})
        self._checkpoint("survey_accepted", force=True)

    def _admission_guard(self):
        for binding in self.bindings.values():
            self.operations.authorize(binding)
        self._ensure_active()

    def _nominate(self):
        self.gate.require_current(self.survey_ref)
        if self.score["proposed_gap"] is not None:
            self.nomination = deepcopy(self.score["proposed_gap"])
        else:
            def validate(value):
                exact(value, {"id", "statement"}, "gap nomination")
                identifier(value["id"])
                if not isinstance(value["statement"], str) or not value["statement"].strip():
                    raise ValidationError("nomination needs an explicit bounded statement")
            self.nomination, _ = self._model_checked("nominate", "research.gap-proposer", {
                "phase": "nomination", "assignment": "Nominate one bounded and testable research-gap hypothesis from the accepted map.",
                "question": self.score["question"], "map": self._map_body(), "coverage": self._coverage(),
                "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
                "instructions": "Return {id:lowercase_identifier,statement:string}. This is a hypothesis to challenge, not an established novelty claim."
            }, validate, task_kind="selection")
        self.nomination_record = self._publish("kb/gap-nomination", "note", {
            "survey_ref": self.survey_ref, **self.nomination}, "research.gap-proposer", subjects=[self.survey_ref])

    def _counter_plan(self):
        self._refresh_countersearch_state()
        if self.counter_plan_record is not None:
            body = self._body(self.counter_plan_record)
            return {key: body[key] for key in ("queries", "rationale")}, self.counter_plan_record
        plan, execution = self._model_checked("counter-plan", "methods.novelty-challenger", {
            "assignment": "Find searches most likely to disprove the nominated gap by locating an existing solution or alternate terminology.",
            "phase": "counter_plan", "question": self.score["question"], "gap": self.nomination,
            "map": self._map_body(), "max_queries": self.bounds["queries_per_role"],
            "search_syntax": SEARCH_SYNTAX,
            "nomination_ref": self.nomination_record["artifact_ref"],
            "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
            "instructions": "Return {queries:[search strings],rationale:string}. Seek prior solutions, incompatible assumptions, and decisive counterevidence."
        }, self._plan_validator, stage="supervision", task_kind="selection")
        record = self._publish("kb/counter-search-plan", "note", {
            **plan, "survey_ref": self.survey_ref,
            "nomination_ref": self.nomination_record["artifact_ref"],
        }, "methods.novelty-challenger", subjects=[execution, self.survey_ref,
                                                   self.nomination_record["artifact_ref"]])
        self.counter_plan_record = record
        return plan, record

    def _countersearch(self):
        plan, record = self._counter_plan()
        self._search(plan["queries"], "methods.novelty-challenger", record["artifact_ref"],
                     admission="challenge")
        self._full_texts()
        self._accept_survey()
        self._refresh_countersearch_state()
        if not self.countersearch_complete:
            raise ValidationError("counter-search completion could not be bound to the accepted survey")

    def _assess(self):
        assessment_sources = self._assessment_source_context()
        assessment_source_lookup = {source["source_ref"]: source for source in assessment_sources}
        verified_full_text_refs = [source["source_ref"] for source in assessment_sources
                                   if source["representation"] == "full_text"
                                   and source.get("identity_verified") is True]
        displayed_lengths = {source["source_ref"]: len(source["text"])
                             for source in assessment_sources}

        def validate_gap_assessment(value):
            validate_assessment(value, assessment_source_lookup, self.works, require_spans=True)
            # A decisive literature state cannot be adopted while the
            # accepted survey still records access, identity, or bounded
            # coverage gaps.  Treat this as a model-contract rejection so the
            # normal scoped retry asks for an evidence-bounded abstention,
            # rather than discovering the contradiction after publication.
            if value["state"] != "insufficient_evidence" and (self.gaps or any(
                    len(source["text"]) > displayed_lengths.get(ref, 0)
                    for ref, source in self.source_docs.items()
                    if source["representation"] == "full_text")):
                raise ValidationError("decisive gap state requires complete access and source context")

        value, execution = self._model_checked("gap-assessment", "methods.novelty-verifier", {
            "assignment": "Independently determine the status of the nominated gap using the current accepted survey and targeted counter-search.",
            "phase": "gap_assessment", "question": self.score["question"], "gap": self.nomination,
            "nomination_ref": self.nomination_record["artifact_ref"],
            "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
            "map": self._assessment_map_context(), "coverage": self._coverage(), "sources": assessment_sources,
            "verified_full_text_refs": verified_full_text_refs,
            "required_checks": list(GAP_CHECKS),
            "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
            "instructions": "Return exactly {state:string,rationale:string,comparisons:[{work_id:string,relationship:string,statement:string,evidence:[{work_id:string,source_ref:string,quote:string}]}],checks:[{check_id:string,outcome:string,method:string,result:string}],evidence:[{work_id:string,source_ref:string,quote:string}]}. "
                "Both top-level evidence and every comparison evidence field must be arrays of evidence objects, never arrays of quote strings. Each evidence item quotes exact available source text. Each quote must be unique in its displayed source window. relationship is solves/partial/different/uncertain. "
                "state is refuted_by_prior_work, insufficient_evidence, or eligible_for_experiment. Run exactly all required checks. "
                "For every check, copy outcome from allowed_check_outcomes exactly; words such as pass, incomplete, inconclusive, or partial are invalid. "
                "A prior solution supported by decisive full-text quotes refutes the gap even if global search is incomplete. "
                "For decisive states all checks must pass and evidence must include verified full_text sources. Abstracts alone cannot authorize a decisive state. "
                "Use insufficient_evidence if access, source windows, missing closest work, or incomparable conditions prevent the judgment. "
                "A decisive state is valid only when every required check has outcome=passed. If any check is insufficient_evidence, failed, or check_failed, state must be insufficient_evidence. "
                "For insufficient_evidence, keep comparisons different or uncertain as warranted by the captured text; do not relabel an abstract sentence as full_text. "
                "If coverage.access_and_limit_gaps is nonempty or any verified full text is longer than the displayed assessment context, state must be insufficient_evidence because the runner cannot adopt a decisive result. "
                "When state is decisive, every comparison evidence item for that comparison must remain attached to an exact verified full_text quotation; prefer short contiguous prose spans over rendered equations. "
                "For refuted_by_prior_work or eligible_for_experiment, cite only the listed verified_full_text_refs for any decisive comparison; "
                "if no listed full-text quote directly supports the comparison, set the state to insufficient_evidence and use relationship=uncertain. "
                "Eligibility requires meaningful, testable distinction, no prior solution or unresolved comparison, and adequate search coverage. "
                "It authorizes an experiment under the stated scope, never publication-ready novelty. Do not force a positive finding to finish the task."
        }, validate_gap_assessment,
            normalizer=lambda value: self._bind_assessment_spans(value, assessment_sources),
            stage="integrated_review", task_kind="verification")
        if value["state"] == "eligible_for_experiment" and (self.gaps or any(
                len(source["text"]) > displayed_lengths.get(ref, 0)
                for ref, source in self.source_docs.items()
                if source["representation"] == "full_text")):
            raise ValidationError("gap eligibility lacks complete access or decisive source context")
        record = self._publish("kb/gap-assessments/current", "note", {
            "survey_ref": self.survey_ref, "nomination_ref": self.nomination_record["artifact_ref"],
            "execution_ref": execution, **value}, "methods.novelty-verifier",
            subjects=[self.survey_ref, execution, self.nomination_record["artifact_ref"]])
        adopted = self.gate.commit_assessment(record["artifact_ref"], survey_ref=self.survey_ref,
                                             author="strategy.survey-integrator", guard=self._admission_guard)
        self.assessment_ref = adopted["artifact_ref"]
        self.verified_changes.append({"kind": "accepted_gap_assessment", "ref": self.assessment_ref})
        return value["state"]

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("survey run requires the main thread")
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt("termination requested")
        signal.signal(signal.SIGTERM, terminate)
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error, decision = "blocked", None, "insufficient_evidence"
        try:
            if not self.resume_session:
                self._initialize()
            if not self.resume_session and not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured survey stages do not fit the hard deadline; no external work dispatched")
            needs_operations = not self.assessment_ref and (
                not self.survey_ref or self.nomination is None or not self.countersearch_complete)
            if needs_operations:
                self._setup()
            if self.assessment_ref:
                decision = self._body(self.store.get(self.assessment_ref))["state"]
            else:
                if not self.survey_ref:
                    if self.nomination is not None:
                        self._accept_survey()
                        self._refresh_countersearch_state()
                    else:
                        plans = self._initial_plans()
                        for wid in self.score["seed_work_ids"]:
                            if wid not in self.aliases:
                                self._bibliographic_call("work", role="research.seed-reader", work_id=wid)
                        self._search(self.score["seed_queries"], "research.seed-searcher", self.protocol["artifact_ref"])
                        for role, queries, ref in plans:
                            self._search(queries, role, ref)
                        self._expand()
                        self._full_texts()
                        self._accept_survey()
                if self.nomination is None:
                    self._nominate()
                if not self.countersearch_complete:
                    self._countersearch()
                decision = self._assess()
            status = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
        finally:
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller", reason="Run ended without an accepted scoped output")
            current, assessment_current = False, False
            if self.survey_ref:
                try:
                    self.gate.require_current(self.survey_ref)
                    current = True
                except Exception:
                    pass
            if self.assessment_ref:
                try:
                    self.gate.require_current_assessment(self.assessment_ref)
                    assessment_current = True
                except Exception:
                    pass
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "project_id": self.config["project_id"], "status": status, "error": error,
            "incumbent_ref": self.incumbent,
            "score_ref": getattr(self, "score_ref", None), "survey_ref": self.survey_ref, "survey_current": current,
            "assessment_ref": self.assessment_ref, "assessment_current": assessment_current,
            "gap_state": decision, "nomination": self.nomination,
            "nomination_ref": self.nomination_record["artifact_ref"] if self.nomination_record else None,
            "coverage": self._coverage(), "time_plan": self.time_policy.snapshot(), "time_decisions": self.time_decisions,
            "provider_intervals": self.provider_intervals, "provider_waits": self.provider_waits,
            "work_budget_adjustments": self.work_budget_adjustments,
            "bibliography_mode": self.bibliography_mode,
            "bibliography_fallback_reason": self.bibliography_fallback_reason,
            "bibliography_fallback_capability": self.bibliography_fallback_capability,
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "capabilities": {key: self.operations.status(key) for key in self.capability_ids},
            "blockers": self.blockers, "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result)
        self.control.close()
        return result

    def _export(self, result):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_bytes(canonical_bytes(result))
        (output / "works.json").write_bytes(canonical_bytes(list(self.works.values())))
        (output / "bibliographic-identities.json").write_bytes(canonical_bytes([
            self._body(record) for record in self.identity_records.values()]))
        (output / "coverage.json").write_bytes(canonical_bytes(self._coverage()))
        if hasattr(self, "map_record"):
            (output / "literature-map.json").write_bytes(canonical_bytes({
                "survey_ref": self.survey_ref, "survey_current": result["survey_current"],
                "map_ref": self.map_record["artifact_ref"], "map": self._map_body()}))
        if self.assessment_ref:
            body = self.store.read_body(self.store.get(self.assessment_ref)["body_hash"])
            (output / "gap-assessment.json").write_bytes(body)
        lines = ["# Literature assessment", "", self.score["question"], "",
            f"Run status: **{result['status']}**. Gap state: **{result['gap_state']}**.", "",
            f"Current accepted survey: {result['survey_ref'] or 'none'}. Current applicability: {result['survey_current']}.", "",
            "This assessment covers the recorded search protocol. It does not establish exhaustive coverage or publication-ready novelty.", "",
            "## Works and comparisons", "", "Publication years below remain provider observations; separate identity records report any Crossref reconciliation.", ""]
        for wid, work in self.works.items():
            lines.extend([f"### {work['title']}", "", f"[Provider record](https://openalex.org/{wid}); reported year: {work['year'] or 'unknown'}.", ""])
            record = self.analysis_records.get(wid)
            if record:
                entry = json.loads(self.store.read_body(record["body_hash"]))
                lines.extend([f"Screening: {entry['inclusion']}. {entry['reason']}", ""])
                for field in MAP_FIELDS:
                    claim = entry[field]
                    lines.extend([f"**{field.capitalize()}:** {claim['text'] or 'Not established by available text.'}", ""])
        if self.assessment_ref:
            assessment = json.loads(self.store.read_body(self.store.get(self.assessment_ref)["body_hash"]))
            lines.extend(["## Gap assessment", "", f"Hypothesis: {self.nomination['statement']}", "",
                          f"Nomination: {result['nomination_ref']}. Current assessment applicability: {result['assessment_current']}.", "",
                          assessment["rationale"], ""])
            for comparison in assessment["comparisons"]:
                lines.extend([f"- **{self.works[comparison['work_id']]['title']}** ({comparison['relationship']}): {comparison['statement']}"])
        lines.extend(["", "## Coverage and unresolved access", "", f"Unique works: {len(self.works)}. Bibliographic calls: {self.api_calls}.", ""])
        for gap in self.gaps + self.blockers:
            lines.append("- " + json.dumps(gap, ensure_ascii=False))
        lines.extend(["", "Release status: not_released.", ""])
        (output / "survey.md").write_text("\n".join(lines), encoding="utf-8", newline="")
