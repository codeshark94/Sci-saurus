"""A bounded, inspectable paragraph production, verification, and adoption run."""
from __future__ import annotations

import json
import signal
import threading
import time

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import validate_config
from scisaurus.runtime.contracts import (_NUMBER, _REQUIRED_REGRESSION_CHECKS, required_strings,
                                          validate_verdict, validate_reassessment)
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker



class ParagraphRunner(ExecutionRuntime):
    """One accepted baseline with independently reserved verification capacity."""
    def __init__(self, project_dir, config, *, on_progress=None):
        super().__init__(project_dir, validate_config(config), worker_target=_invoke_worker,
                         on_progress=on_progress)

    def _retrieve(self, role, label):
        limits = self.config["limits"]
        captures = []
        campaign = self._publish(f"kb/campaigns/{label}", "search_campaign", {
            "owner": role, "objective": self.config["objective"],
            "public_queries": self.config["public_queries"], "source_urls": self.config["source_urls"],
            "coverage_scope": "explicit query and source list; not an exhaustive literature review",
        }, role)
        for index, query in enumerate(self.config["public_queries"]):
            task = f"{label}-search-{index}"
            result, report = self._call(task, "crossref", {
                "client": {"timeout": limits["retrieval_timeout_seconds"], "max_bytes": limits["max_source_bytes"]},
                "query": query, "limit": limits["search_results"],
            }, actor=role, task_kind="retrieval")
            record = self._publish(f"kb/queries/{task}", "query_record", result, role, subjects=[campaign["artifact_ref"], report])
            if result["outcome"] != "ok":
                raise ValidationError(f"scholarly search unavailable: {result.get('error', result['outcome'])}")
            for source_index, source in enumerate(result["sources"]):
                discovery = self._publish(f"kb/discoveries/{task}-{source_index}", "discovery_record", source, role,
                                          subjects=[record["artifact_ref"]])
                self._publish(f"kb/references/{task}-{source_index}", "reference_card", source, role,
                              subjects=[discovery["artifact_ref"]])
            self.information_changes.append({"kind": "metadata_discovery", "ref": record["artifact_ref"]})
            self._complete(task)
        for index, url in enumerate(self.config["source_urls"]):
            task = f"{label}-fetch-{index}"
            discovery = self._publish(f"kb/discoveries/{task}", "discovery_record",
                {"source_url": url, "discovery_route": "configured_public_source", "representation": "lead"}, role,
                subjects=[campaign["artifact_ref"]])
            reference = self._publish(f"kb/references/{task}", "reference_card", {"source_url": url}, role,
                                      subjects=[discovery["artifact_ref"]])
            result, report = self._call(task, "fetch", {
                "client": {"command": self.config["mcp_fetch_command"], "timeout": limits["retrieval_timeout_seconds"],
                           "max_bytes": limits["max_source_bytes"], "own_process_group": False},
                "url": url, "max_length": limits["max_capture_chars"],
            }, actor=role, task_kind="retrieval")
            record = self._publish(f"kb/captures/{task}", "source_capture", result, role, subjects=[reference["artifact_ref"], report])
            if (result["outcome"] != "ok" or not result.get("text")
                or result.get("metadata", {}).get("representation") != "extracted_text"):
                raise ValidationError(f"source capture unavailable: {result.get('error', result['outcome'])}")
            captures.append({"ref": record["artifact_ref"], "url": url, "text": result["text"],
                             "representation": result.get("metadata", {}).get("representation", "extracted_text"),
                             "capture_sha256": result["capture_sha256"]})
            self.information_changes.append({"kind": "source_capture", "ref": record["artifact_ref"]})
            self._complete(task)
        self._publish(f"kb/coverage/{label}", "coverage_report", {
            "campaign_ref": campaign["artifact_ref"], "captures": [c["ref"] for c in captures],
            "termination_reason": "configured_routes_completed", "scope": "partial",
            "independent_context": role == "methods.verifier",
        }, role, subjects=[campaign["artifact_ref"], *[c["ref"] for c in captures]])
        return captures

    _required_strings = staticmethod(required_strings)

    _validate_verdict = staticmethod(validate_verdict)

    _validate_reassessment = staticmethod(validate_reassessment)

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("run must execute on the main thread so termination can reconcile active work")
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt("termination requested")
        signal.signal(signal.SIGTERM, terminate)
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error = "blocked", None
        failure = None
        try:
            self._initialize_document()
            writer_sources = self._retrieve("research.writer", "producer")
            self.sources.extend(writer_sources)
            supervisor, supervisor_ref = self._model("supervise", "command.supervisor", {
                "assignment": "Identify the precise defect in the baseline and define a surgical revision objective. "
                              "Preserve all hard requirements. Return allegation, material_impact, resolution_condition, rationale (strings).",
                "objective": self.config["objective"], "baseline": self.config["paragraph"],
                "supplied_context": self.config["supplied_context"], "sources": writer_sources,
                "required_literals": self.config["required_literals"],
            }, task_kind="service")
            self._required_strings(supervisor, ("allegation", "material_impact", "resolution_condition", "rationale"))
            self._complete("supervise")
            self._publish("command/supervision/initial", "supervision_decision", supervisor, "command.supervisor",
                          subjects=[supervisor_ref, self.baseline["artifact_ref"]])
            critique = self.issues.publish_critique(
                critique_id="paragraph", author_role="command.supervisor", target_ref=self.baseline["artifact_ref"],
                target_location=self.unit["artifact_id"], criterion_ref=self.criterion["artifact_ref"],
                allegation=supervisor["allegation"], basis={"evidence_refs": [self.context["artifact_ref"], *[s["ref"] for s in writer_sources]]},
                material_impact=supervisor["material_impact"], proposed_severity="blocking",
                resolution_condition=supervisor["resolution_condition"],
            )
            self.issues.register_issue(issue_id="paragraph", critique_ref=critique["artifact_ref"])
            self.issues.triage("paragraph", "command.controller", admissible=True, reason="specific objective bound to supplied facts and captured sources")
            verification_sources = self._retrieve("methods.verifier", "independent")
            self.sources.extend(verification_sources)
            feedback = []
            for round_number in range(1, self.config["limits"]["max_rounds"] + 1):
                accepted, feedback = self._round(round_number, supervisor, writer_sources, verification_sources, feedback)
                if accepted:
                    status = "accepted"
                    break
                if round_number < self.config["limits"]["max_rounds"]:
                    decision, decision_ref = self._model(f"reassess-{round_number}", "command.supervisor", {
                        "assignment": "Decide whether another targeted revision is justified by the failed checks. "
                                      "Return only decision (revise or pause) and rationale (substantive strings). "
                                      "For revise, also provide substantive change_focus describing a concrete change in approach. "
                                      "For pause, omit change_focus or leave it an empty string. "
                                      "Base the decision on failed checks, regressions, or unresolved material uncertainties; "
                                      "nonblocking observations alone do not justify rewriting. "
                                      "Continued work needs a concrete change in approach, not repeated output.",
                        "objective": self.config["objective"], "verification_feedback": feedback,
                        "round": round_number, "remaining_seconds": max(0, self.deadline - time.monotonic()),
                    }, task_kind="service")
                    self._validate_reassessment(decision)
                    self._publish(f"command/supervision/reassess-{round_number}", "supervision_decision", decision,
                                  "command.supervisor", subjects=[decision_ref])
                    self._complete(f"reassess-{round_number}")
                    if decision["decision"] == "pause":
                        status = "paused"
                        self.blockers.append({"reason": decision["rationale"]})
                        break
                    feedback.append({"next_revision_focus": decision["change_focus"]})
            else:
                status = "unresolved"
                self.blockers.append({"reason": "revision round limit reached", "issue_id": "paragraph"})
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
            if isinstance(exc, KeyboardInterrupt):
                status, failure = "paused", {"kind": "process_interrupted"}
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller", reason=error)
        finally:
            if hasattr(self, "baseline"):
                accepted = self.store.accepted(self.baseline["artifact_id"])
                self.incumbent = accepted["artifact_ref"] if accepted else None
                for candidate in self.candidates:
                    candidate["adopted"] = candidate["candidate_ref"] == self.incumbent
            for row in self.control._conn.execute(
                "SELECT r.reservation_id FROM reservations r WHERE r.state='reserved'"
                " AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.task_id=r.task_id)"
            ).fetchall():
                self.budget.settle(window_id="run-window", reservation_id=row[0], actual={})
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "status": status, "error": error, "incumbent_ref": self.incumbent,
                  "failure": failure,
                  "baseline_ref": getattr(self, "baseline", {}).get("artifact_ref"), "candidates": self.candidates,
                  "source_captures": [{k: v for k, v in source.items() if k != "text"} for source in self.sources],
                  "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps, "blockers": self.blockers,
                  "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result)
        self.control.close()
        return result

    def _initialize_document(self):
        self.context = self._publish("inputs/context", "results_package", {"text": self.config["supplied_context"]}, "principal")
        self.criterion = self._publish("command/criteria/paragraph", "note", {
            "objective": self.config["objective"], "required_literals": self.config["required_literals"],
            "preserve_neighbor": True, "source_support_policy": "captured_support_for_external_claims",
            "reader_facing_prose": True,
        }, "principal")
        self.unit = self.documents.publish_unit(logical_id="strategy/units/paragraph", kind="paragraph",
                                               text=self.config["paragraph"], author="principal", purpose=self.config["objective"])
        self.neighbor = self.documents.publish_unit(logical_id="strategy/units/neighbor", kind="paragraph",
                                                   text=self.config["preserved_neighbor"], author="principal")
        self.baseline = self.documents.publish_manifest(document_id="strategy/documents/manuscript", author="principal",
            tree={"units": [{"ref": self.unit["artifact_ref"], "children": []},
                            {"ref": self.neighbor["artifact_ref"], "children": []}],
                  "assembly_dependencies": [self.context["artifact_ref"], self.criterion["artifact_ref"]]})
        self.store.adopt(self.baseline["artifact_id"], target_version=self.baseline["version"], expected_accepted_version=None, actor="principal")
        self.incumbent = self.baseline["artifact_ref"]
        self._checkpoint("baseline_recorded", force=True)

    def _round(self, number, supervisor, writer_sources, verifier_sources, feedback):
        writer_task, verify_task = f"write-{number}", f"verify-{number}"
        review_reservation = f"reserved-review-{number}"
        self.budget.reserve(window_id="run-window", reservation_id=review_reservation, task_id=verify_task,
                            amount={"concurrent_calls": 1})
        author = f"strategy.writer-{number}"
        proposal, proposal_ref = self._model(writer_task, author, {
            "assignment": "Rewrite only the baseline paragraph for the objective. Return text (one single-line paragraph) and "
                          "support (list of {source_ref, quote, supports}). Each quote must be an exact short excerpt "
                          "of at most 20 words from a supplied captured source, and supports must describe the narrow claim it supports. "
                          "Support may be empty only when every assertion uses supplied facts or an interpretation permitted by "
                          "the acceptance contract. Do not insert external background merely to fill the support list. "
                          "Write manuscript prose for the scientific reader. Keep internal claim IDs and references to the user, "
                          "Principal, assignment, or acceptance checks out of the paragraph. Express the relevant scientific "
                          "finding or limitation directly; control labels belong only in separate evidence records. "
                          "Do not treat external literature as data from this study. Do not change preserved values.",
            "baseline": self.config["paragraph"], "objective": self.config["objective"],
            "resolution_condition": supervisor["resolution_condition"], "supplied_context": self.config["supplied_context"],
            "required_literals": self.config["required_literals"], "sources": writer_sources, "feedback": feedback,
        }, task_kind="production")
        self._required_strings(proposal, ("text",))
        if any(char in proposal["text"] for char in "\r\n\u2028\u2029"):
            raise ValidationError("producer exceeded the one-paragraph output scope")
        support = proposal.get("support")
        if not isinstance(support, list):
            raise ValidationError("producer must explicitly report its captured-source support list")
        source_by_ref = {s["ref"]: s for s in writer_sources}
        for item in support:
            self._required_strings(item, ("source_ref", "quote", "supports"))
            if (len(item["quote"].split()) > 20 or item["source_ref"] not in source_by_ref
                or item["quote"] not in source_by_ref[item["source_ref"]]["text"]):
                raise ValidationError("producer support is not present in its cited source capture")
        self._ensure_active()
        request = self.changes.create_change_request(
            cr_id=f"paragraph-{number}", author="command.controller", purpose=self.config["objective"],
            baseline_manifest_ref=self.baseline["artifact_ref"], scope={"units": [self.unit["artifact_id"]],
            "source_refs": list(source_by_ref)}, preservation=["neighbor identity and bytes", *self.config["required_literals"]])
        grant_id = f"paragraph-{number}"
        self.changes.issue_grant(grant_id=grant_id, request_ref=request["artifact_ref"],
            baseline_manifest_ref=self.baseline["artifact_ref"], actor=author,
            units={self.unit["artifact_id"]: {"ops": ["replace_body"]}},
            expires_in_seconds=max(0.001, self.deadline - time.monotonic()))
        staged = self.changes.stage_changeset(changeset_id=f"paragraph-{number}", grant_id=grant_id, author=author,
            expected_accepted_manifest_version=self.baseline["version"], ops=[{
                "op": "replace_body", "unit": self.unit["artifact_id"], "span": [0, len(self.config["paragraph"])],
                "new_text": proposal["text"], "expected_unit_version": self.unit["version"],
                "expected_body_hash": self.unit["body_hash"],
            }])
        candidate_ref = staged["manifest_ref"]
        candidate = {"round": number, "candidate_ref": candidate_ref, "changeset_ref": staged["changeset_ref"],
                     "text": proposal["text"], "support": support, "adopted": False}
        self.candidates.append(candidate)
        support_record = self._publish(f"kb/support/paragraph-{number}", "evidence_record",
                                      {"candidate_ref": candidate_ref, "support": support}, author,
                                      subjects=[candidate_ref, proposal_ref, *source_by_ref])
        response = self.issues.respond("paragraph", response_id=f"paragraph-{number}", author=author, stance="accept",
                                       candidate_ref=candidate_ref, supporting_refs=[support_record["artifact_ref"]])
        self._checkpoint("candidate_staged", force=True)
        verdict, verdict_ref = self._model(verify_task, "methods.verifier", {
            "assignment": "Independently verify the exact candidate against the supplied facts, objective, and captured "
                          "sources. Check the supervisor's resolution condition against those governing inputs too: report any "
                          "contradiction as a material uncertainty even if the candidate follows the supervisor's wording. "
                          "Inspect whether collection, measurement, processing, verification, and reporting states are preserved. "
                          "You have no producer private reasoning. Return checks (list of {check_id, kind, outcome, "
                          "method, result}), regressions (list of substantive strings), uncertainties (list of substantive strings), "
                          "observations (list of {observation, reason_nonblocking}), and rationale (substantive string). "
                          "Return exactly these fields; use empty lists when there are no items. Both observation fields "
                          "must be substantive strings explaining the note and why it cannot affect the assigned acceptance criteria. "
                          "Regressions are lost or weakened applicable requirements. Uncertainties are unresolved material questions "
                          "about whether the candidate meets those criteria. A limitation correctly preserved in the candidate "
                          "is not itself an unresolved acceptance question. Record notes that do not affect acceptance as observations "
                          "with their nonblocking basis; if that basis cannot be justified, retain the concern as an uncertainty. "
                          "Never downgrade a failed check, insufficient evidence, regression, or material uncertainty to an observation. "
                          "Check kinds: resolution and regression; outcomes: passed, failed, insufficient_evidence, check_failed. "
                          "Execute every entry in required_checks and copy its check_id and kind exactly into your checks list. "
                          "Do not rename required check IDs or append suffixes. Add other checks as needed. "
                          "Include both kinds and report observed comparisons. Use unique check_id values other than "
                          "mechanical-preservation. Insufficient support is not a pass.",
            "objective": self.config["objective"], "resolution_condition": supervisor["resolution_condition"],
            "required_checks": [{"check_id": check_id, "kind": "regression", "requirement": requirement}
                                for check_id, requirement in _REQUIRED_REGRESSION_CHECKS.items()],
            "baseline": self.config["paragraph"], "candidate": proposal["text"],
            "supplied_context": self.config["supplied_context"], "required_literals": self.config["required_literals"],
            "claimed_support": support, "producer_captures": writer_sources,
            "independently_fetched_sources": verifier_sources,
        }, task_kind="verification", reservation_id=review_reservation)
        candidate["verdict_ref"] = verdict_ref
        self._validate_verdict(verdict)
        for field in ("observations", "regressions", "uncertainties"):
            candidate[field] = verdict[field]
        checks = list(verdict["checks"])
        tree = self.documents.get_tree(candidate_ref)
        numbers = {match.group() for match in _NUMBER.finditer(proposal["text"])}
        literal_ok = all(
            literal in numbers if _NUMBER.fullmatch(literal) else literal in proposal["text"]
            for literal in self.config["required_literals"]
        )
        neighbor_ok = tree["units"][1]["ref"] == self.neighbor["artifact_ref"] and len(tree["units"]) == 2
        checks.append({"check_id": "mechanical-preservation", "kind": "regression",
                       "outcome": "passed" if literal_ok and neighbor_ok else "failed",
                       "method": "compare required literals and exact neighboring unit identity",
                       "result": f"required_literals_present={literal_ok}; neighbor_unchanged={neighbor_ok}"})
        binding = {"issue_id": "paragraph", "critique_ref": self.issues.get("paragraph")["critique_ref"],
                   "response_ref": response["artifact_ref"], "candidate_ref": candidate_ref,
                   "baseline_ref": self.baseline["artifact_ref"], "resolution_condition": supervisor["resolution_condition"]}
        check_refs = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                raise ValidationError("verifier checks must be structured objects")
            self._required_strings(check, ("check_id", "kind", "outcome", "method", "result"))
            check_record = self._publish(f"methods/checks/{number}-{index}", "evidence_record", {**check, **binding},
                "methods.verifier", subjects=[candidate_ref, self.baseline["artifact_ref"], verdict_ref,
                                               *[s["ref"] for s in verifier_sources]])
            check_refs.append(check_record["artifact_ref"])
        verification = self.issues.verify("paragraph", verification_id=f"paragraph-{number}", verifier="methods.verifier",
            response_ref=response["artifact_ref"], candidate_ref=candidate_ref, baseline_ref=self.baseline["artifact_ref"],
            resolution_condition=supervisor["resolution_condition"], check_refs=check_refs,
            regressions=verdict.get("regressions"), uncertainties=verdict.get("uncertainties"), rationale=verdict["rationale"])
        self._complete(verify_task)
        verified = json.loads(self.store.read_body(verification["body_hash"]))
        candidate["verification_ref"] = verification["artifact_ref"]
        candidate["resolved"] = verified["resolved"]
        self._ensure_active()
        if verified["resolved"]:
            adopted = self.changes.accept_changeset(changeset_ref=staged["changeset_ref"], verification_ref=verification["artifact_ref"],
                author=author, expected_accepted_manifest_version=self.baseline["version"],
                expected_head_refs=[self.context["artifact_ref"], self.criterion["artifact_ref"],
                                    *[source["ref"] for source in self.sources]], acceptance_guard=self._ensure_active)
            self.incumbent = adopted["manifest_ref"]
            candidate["adopted"] = True
            self.verified_changes.append({"kind": "paragraph_revision", "ref": self.incumbent,
                                          "verification_ref": verification["artifact_ref"]})
            self._complete(writer_task)
            return True, []
        self.tasks.transition(writer_task, "stale", "command.controller", reason="independent verification did not pass")
        self.budget.mark_unproductive(cause_key="paragraph-objective", window_id="run-window")
        self._checkpoint("revision_required", force=True)
        return False, [{"checks": verified["checks"], "regressions": verified["regressions"],
                        "uncertainties": verified["uncertainties"], "observations": verdict["observations"],
                        "verdict_ref": verdict_ref, "rationale": verdict["rationale"]}]

    def _export(self, result):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        lines = ["# Paragraph integration run", "", f"Status: **{result['status']}**", "",
                 "This run evaluates a bounded paragraph revision. It is not a released manuscript.", "",
                 "## Baseline", "", self.config["paragraph"], ""]
        for candidate in self.candidates:
            lines.extend([f"## Candidate {candidate['round']}", "", candidate["text"], "",
                          f"Adopted: {candidate['adopted']}. Artifact: `{candidate['candidate_ref']}`.", ""])
            if candidate.get("verdict_ref"):
                lines.extend([f"Verifier output: `{candidate['verdict_ref']}`.", ""])
            if candidate.get("observations"):
                lines.extend(["### Nonblocking observations", ""])
                for observation in candidate["observations"]:
                    lines.extend([f"- {observation['observation']} Basis: {observation['reason_nonblocking']}", ""])
            for field, heading in (("uncertainties", "Unresolved material uncertainties"), ("regressions", "Regressions")):
                if candidate.get(field):
                    lines.extend([f"### {heading}", ""])
                    lines.extend(f"- {item}" for item in candidate[field])
                    lines.append("")
        lines.extend(["## Source captures", ""])
        for source in self.sources:
            lines.append(f"- [{source['url']}]({source['url']}) — `{source['ref']}`")
        if self.blockers:
            lines.extend(["", "## Unresolved items", ""])
            lines.extend("- " + item["reason"] for item in self.blockers)
        (output / "report.md").write_text("\n".join(lines) + "\n")
