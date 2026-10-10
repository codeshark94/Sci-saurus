"""Evidence-driven acquisition with immutable read and query lineage."""

from copy import deepcopy
import hashlib
import json
import os
import tempfile

from scisaurus.core.schema import canonical_bytes
from scisaurus.core.errors import ModelContractError, ProviderRateLimitError, StateError, ValidationError
from scisaurus.core.source_spans import (LEGACY_EVIDENCE_FIELDS, SPAN_EVIDENCE_FIELDS,
                                         bind, expand_evidence, index_evidence, validate as validate_span)
from scisaurus.runtime.survey_config import query_identity, search_query, work_id
from scisaurus.runtime.survey_records import MAP_FIELDS, authoritative_source


SEARCH_PLANNERS = ("research.search-planner", "methods.blind-search-planner")
PLAN_FIELDS = {"decision", "rationale", "branches"}
BRANCH_FIELDS = {"parent_id", "question", "rationale", "operation", "query", "work_id", "evidence"}


def exploration_response_contract():
    """Expose the same branch envelope enforced by the local validator."""
    return {
        "required_fields": sorted(PLAN_FIELDS),
        "decision_values": ["expand", "stop"],
        "branches": {"required_fields": sorted(BRANCH_FIELDS),
            "operation_values": ["search", "work", "citing"],
            "inactive_parameters": {"search": {"work_id": None},
                                    "work": {"query": None}, "citing": {"query": None}},
            "work_id_binding": {
                "root_parent": "For work, a canonical work ID from the scientific intake.",
                "read_parent": "For work or citing, select a handle from this parent's citation_targets with the same operation. Work follows a reference; citing follows incoming citations. Neither operation re-reads the parent itself.",
                "unlisted_target": "Do not reuse an unlisted ID or substitute an unrelated permitted target. Use a justified search with this parent's captured evidence, or close the inquiry with an explicit reason.",
            },
            "evidence": {"root_parent": "An explicitly empty list; no source evidence is available.",
                         "read_parent": "A nonempty list of captured evidence owned by that parent."}},
    }


def node_id(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def bind_reading_candidates(value, aliases):
    """Bind exact candidate handles without inferring a paper identity."""
    validate_reading_proposal(value, require_priority=isinstance(value, dict) and "read_priority" in value)
    value = deepcopy(value)
    for row in value.get("candidates", []):
        if isinstance(row, dict) and isinstance(row.get("work_id"), str):
            row["work_id"] = aliases.get(row["work_id"], row["work_id"])
    if isinstance(value.get("read_priority"), list):
        value["read_priority"] = [aliases.get(wid, wid) if isinstance(wid, str) else wid
                                  for wid in value["read_priority"]]
    return value


def selection_basis(assignment):
    """Identify scientific inputs independently of their model presentation."""
    return {"question": assignment["question"], "inquiries": assignment["inquiries"],
            "candidates": [{"work_id": item["work_id"], "work_ref": item["work_ref"],
                            "source_refs": sorted(source["source_ref"] for source in item["sources"])}
                           for item in assignment["candidates"]],
            "checked_entries": assignment["checked_entries"]}


def planning_parent(node, alias, work=None, entry=None, review=None):
    """Expose checked findings and evidence ownership without repeating captures."""
    result = {key: value for key, value in node.items() if key not in {"id", "parent_id", "inquiry_evidence"}}
    result["id"] = alias
    if node["kind"] == "read":
        result.update(work={key: work.get(key) for key in ("title", "year", "doi")}, entry=entry,
            review={"entry_ref": node["entry_ref"], "checks": [
                {key: check[key] for key in ("check_id", "outcome") if key in check} for check in review["checks"]]},
            allowed_evidence={"work_id": node["work_id"], "source_refs": node["source_refs"]})
    return result


def citation_targets(parents, parent_aliases):
    """Expose acquisition handles bound to one reviewed parent and direction."""
    targets = {}
    for alias, parent_id in parent_aliases.items():
        parent = parents[parent_id]
        if parent["kind"] != "read":
            continue
        for index, wid in enumerate(dict.fromkeys(parent.get("referenced_works", []))):
            work_id(wid)
            targets[f"{alias}-reference-{index}"] = {
                "parent_id": parent_id, "operation": "work", "work_id": wid}
        work_id(parent["work_id"])
        targets[f"{alias}-citing"] = {
            "parent_id": parent_id, "operation": "citing", "work_id": parent["work_id"]}
    return targets


def bind_plan_citations(value, targets):
    """Resolve exact handles; never infer a reference from prose or another parent."""
    value = deepcopy(value)
    for index, branch in enumerate(_plan_branches(value)):
        handle = branch.get("work_id")
        target = targets.get(handle) if isinstance(handle, str) else None
        if target is None:
            continue
        if (branch["parent_id"] != target["parent_id"]
                or branch["operation"] != target["operation"]):
            raise ModelContractError(
                f"branch {index} citation handle {handle!r} does not belong to "
                "the assigned parent and acquisition direction")
        branch["work_id"] = target["work_id"]
    return value


def bind_plan_parents(value, parent_aliases):
    """Bind assigned parents and canonicalize inactive acquisition parameters."""
    value = deepcopy(value)
    if not isinstance(value, dict) or not isinstance(value.get("branches"), list):
        raise ModelContractError("exploration plan must provide a branches list")
    for index, branch in enumerate(value["branches"]):
        alias = branch.get("parent_id") if isinstance(branch, dict) else None
        if not isinstance(alias, str) or (alias not in parent_aliases and alias not in parent_aliases.values()):
            raise ModelContractError(f"branch {index} parent_id {alias!r} is unassigned; "
                                     f"use one of {list(parent_aliases)}")
        branch["parent_id"] = parent_aliases.get(alias, alias)
        operation = branch.get("operation")
        if operation == "search":
            branch.setdefault("work_id", None)
        elif operation in ("work", "citing"):
            branch.setdefault("query", None)
    return value


def _plan_branches(value, *, max_branches=None):
    if not isinstance(value, dict):
        raise ModelContractError("exploration plan must be an object")
    if set(value) != PLAN_FIELDS:
        raise ModelContractError("exploration plan has invalid fields: "
                                 f"missing {sorted(PLAN_FIELDS - set(value))}, "
                                 f"extra {sorted(set(value) - PLAN_FIELDS)}")
    if value["decision"] not in ("expand", "stop"):
        raise ModelContractError("decision: must be expand or stop")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ModelContractError("rationale: requires a nonempty string")
    branches = value["branches"]
    if not isinstance(branches, list):
        raise ModelContractError("branches: must be a list")
    if max_branches is not None and len(branches) > max_branches:
        raise ModelContractError(f"branches: received {len(branches)} items, exceeding declared bound {max_branches}")
    if bool(branches) != (value["decision"] == "expand"):
        raise ModelContractError(f"branches: decision={value['decision']!r} requires "
                                 + ("a nonempty list" if value["decision"] == "expand" else "an empty list"))
    for index, branch in enumerate(branches):
        fields = BRANCH_FIELDS
        if not isinstance(branch, dict):
            raise ModelContractError(f"branch {index} must be an object")
        if set(branch) != fields:
            raise ModelContractError(f"branch {index} has invalid fields: "
                                     f"missing {sorted(fields - set(branch))}, "
                                     f"extra {sorted(set(branch) - fields)}")
        if not isinstance(branch["parent_id"], str):
            raise ModelContractError(f"branch {index} parent_id must be an assigned string handle")
    return branches


def _plan_evidence_source(branch, index, proof, parent, sources):
    """Resolve a quotation only within its reviewed parent's source ownership."""
    allowed_refs = set(parent.get("source_refs", []))
    if (not isinstance(proof, dict) or not isinstance(proof.get("source_ref"), str)
            or proof["source_ref"] not in allowed_refs):
        supplied = proof.get("source_ref") if isinstance(proof, dict) else None
        raise ModelContractError(f"branch {index} parent {branch['parent_id']} owns work {parent.get('work_id')} "
                                 f"and allows sources {sorted(allowed_refs)}; supplied source {supplied!r} "
                                 "is outside its reviewed parent's source scope")
    source = sources.get(proof["source_ref"])
    if (source is None or proof.get("work_id") != parent["work_id"]
            or not authoritative_source(source)):
        raise ModelContractError(f"branch {index} parent {branch['parent_id']} requires an authoritative "
                                 f"source for work {parent['work_id']}; supplied work {proof.get('work_id')!r} "
                                 f"and source {proof['source_ref']!r} do not satisfy that ownership")
    return source


def normalize_plan(value, parent_aliases, sources, *, windows=None, parents=None, evidence_catalog=None):
    """Bind only exact assigned identifiers and captured evidence spans."""
    try:
        value = bind_plan_parents(value, parent_aliases)
        if parents is not None:
            value = bind_plan_citations(value, citation_targets(parents, parent_aliases))
        errors = []
        for index, branch in enumerate(_plan_branches(value)):
            evidence = branch.get("evidence")
            if not isinstance(evidence, list):
                raise ModelContractError(f"branch {index} evidence must be a list")
            evidence = expand_evidence({"evidence": evidence}, evidence_catalog or [], sources, windows=windows)["evidence"]
            for proof in evidence:
                if not isinstance(proof, dict) or not isinstance(proof.get("source_ref"), str):
                    raise ModelContractError(f"branch {index} evidence requires a string source_ref")
                if set(proof) not in (LEGACY_EVIDENCE_FIELDS, SPAN_EVIDENCE_FIELDS):
                    raise ModelContractError(f"branch {index} evidence fields must match the quote contract")
                if any(not isinstance(proof.get(field), str) for field in ("work_id", "quote")):
                    raise ModelContractError(f"branch {index} evidence requires string work_id and quote")
            normalized = []
            for proof_index, proof in enumerate(evidence):
                try:
                    if parents is not None:
                        parent = parents.get(branch["parent_id"])
                        if parent is None:
                            raise ModelContractError("exploration branch identifies an unassigned parent")
                        _plan_evidence_source(branch, index, proof, parent, sources)
                    normalized.append(bind(proof, sources, windows=windows))
                except ValidationError as exc:
                    errors.append(f"branches[{index}].evidence[{proof_index}]: {exc}")
            branch["evidence"] = normalized
        if errors:
            raise ModelContractError("; ".join(errors))
        return value
    except ValidationError as exc:
        raise ModelContractError(str(exc)) from exc


def validate_plan(value, parents, sources, *, max_branches):
    branches = _plan_branches(value, max_branches=max_branches)
    seen = set()
    for index, branch in enumerate(branches):
        parent = parents.get(branch["parent_id"])
        if parent is None:
            raise ModelContractError(f"branches[{index}].parent_id: identifies an unassigned parent")
        for field in ("question", "rationale"):
            if not isinstance(branch[field], str) or not branch[field].strip():
                raise ModelContractError(f"branches[{index}].{field}: requires a nonempty string")
        operation = branch["operation"]
        if operation == "search":
            try:
                search_query(branch["query"])
            except ValidationError as exc:
                raise ModelContractError(f"branch {index} query: {exc}") from exc
            if branch["work_id"] is not None:
                raise ModelContractError(f"branches[{index}].work_id: search requires null")
        elif operation in ("work", "citing"):
            try:
                work_id(branch["work_id"])
            except ValidationError as exc:
                raise ModelContractError(f"branch {index} work_id: {exc}") from exc
            if branch["query"] is not None:
                raise ModelContractError(f"branches[{index}].query: work and citing require null")
            if parent["kind"] == "root":
                if operation != "work":
                    raise ModelContractError(f"branches[{index}].operation: citing requires a concrete reviewed parent work")
            else:
                allowed = parent.get("referenced_works", []) if operation == "work" else [parent.get("work_id")]
                if branch["work_id"] not in allowed:
                    raise ModelContractError(f"branch {index} {operation} work_id {branch['work_id']!r} must follow "
                                             f"its parent's actual citation metadata; allowed work_ids={allowed}")
        else:
            raise ModelContractError(f"branches[{index}].operation: unsupported exploration acquisition operation")
        evidence = branch["evidence"]
        if not isinstance(evidence, list) or bool(evidence) != (parent["kind"] == "read"):
            requirement = "a nonempty list of captured evidence owned by this parent" if parent["kind"] == "read" else "an explicitly empty list"
            raise ModelContractError(f"branches[{index}].evidence: parent {branch['parent_id']!r} "
                f"(kind={parent['kind']}, work_id={parent.get('work_id')!r}) requires {requirement}")
        for proof in evidence:
            source = _plan_evidence_source(branch, index, proof, parent, sources)
            try:
                validate_span(proof, source, require_span=True)
            except ValidationError as exc:
                raise ModelContractError(f"branch {index} evidence: {exc}") from exc
        key = (branch["parent_id"], operation, branch["query"], branch["work_id"])
        if key in seen:
            raise ModelContractError(f"branches[{index}]: repeats the same parent acquisition")
        seen.add(key)


def validate_reading_proposal(value, *, require_priority=False):
    fields = {"rationale", "candidates"} | ({"read_priority"} if require_priority else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ModelContractError("reading selection requires rationale and candidates")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ModelContractError("reading selection requires a rationale")
    if not isinstance(value["candidates"], list):
        raise ModelContractError("reading decisions must be a list")
    if require_priority and (not isinstance(value["read_priority"], list)
            or any(not isinstance(wid, str) for wid in value["read_priority"])):
        raise ModelContractError("read_priority must be a list of captured work IDs")


def reading_selection_parts(value, candidates):
    """Keep unique valid decisions; unresolved identities require model judgment."""
    validate_reading_proposal(value, require_priority=isinstance(value, dict) and "read_priority" in value)
    grouped = {wid: [] for wid in candidates}
    invalid, unknown = [], []
    for index, choice in enumerate(value["candidates"]):
        wid = choice.get("work_id") if isinstance(choice, dict) else None
        identifiable = isinstance(wid, str) and wid in grouped
        if identifiable:
            grouped[wid].append((index, choice))
        else:
            unknown.append({"index": index, "choice": choice})
        if not isinstance(choice, dict) or set(choice) != {"work_id", "decision", "rationale"}:
            invalid.append({"index": index, "choice": choice, "reason": "required fields"})
    accepted, unresolved = [], []
    for wid, rows in grouped.items():
        if len(rows) != 1:
            unresolved.append({"work_id": wid, "reason": "missing" if not rows else "duplicate",
                               "rows": [{"index": i, "choice": row} for i, row in rows]})
            continue
        index, choice = rows[0]
        if (set(choice) != {"work_id", "decision", "rationale"}
                or not isinstance(choice.get("decision"), str) or choice["decision"] not in {"read", "defer"}
                or not isinstance(choice["rationale"], str) or not choice["rationale"].strip()):
            unresolved.append({"work_id": wid, "reason": "invalid decision or rationale",
                               "rows": [{"index": index, "choice": choice}]})
        else:
            accepted.append((index, deepcopy(choice)))
    accepted.sort(key=lambda item: item[0])
    return [choice for _, choice in accepted], {
        "unresolved": unresolved, "unknown": unknown, "invalid": invalid}


def validate_reading_selection(value, candidates):
    validate_reading_proposal(value)
    _, issues = reading_selection_parts(value, candidates)
    if any(issues.values()):
        raise ModelContractError("reading selection identity violations: " + json.dumps(issues, ensure_ascii=False))


class LiteratureTree:
    """Survey orchestration mixin; scientific branch choices belong to model jobs."""

    def _tree_save(self):
        refs = {self.protocol["artifact_ref"]}
        for node in self.exploration_tree["nodes"]:
            refs.update(node[field] for field in ("work_ref", "entry_ref", "review_ref", "plan_ref", "query_ref", "selection_ref", "follow_up_ref")
                        if node.get(field))
            refs.update(node.get("source_refs", []))
        record = self._record("kb/exploration-tree", "note", self.exploration_tree,
                              "research.search-planner", subjects=sorted(refs))
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=output, prefix=".exploration-tree-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(canonical_bytes(self.exploration_tree))
            os.replace(temporary, output / "exploration-tree.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record

    def _tree_load(self):
        retained = self.store.head("kb/exploration-tree")
        if retained:
            tree = self._body(retained)
            if (tree.get("schema_version") != "literature-exploration-1"
                    or tree.get("question") != self.score["question"]
                    or tree.get("protocol_ref") != self.protocol["artifact_ref"]):
                raise ValidationError("retained exploration tree does not match the declared survey")
            self.exploration_tree = tree
            retained_discovery = (
                getattr(self, "follow_up_discovery_current", False)
                and self.resume_session is not None
                and not {"retrieval", "production"}.intersection(self.resume_session["reopened_scopes"])
            )
            if tree.get("follow_up_ref") != self.follow_up_ref and not retained_discovery:
                root = {"kind": "root", "question": self.score["question"],
                        "protocol_ref": self.protocol["artifact_ref"], "follow_up_ref": self.follow_up_ref}
                for node in tree["nodes"]:
                    if node["kind"] in {"root", "read"} and node["state"] == "pending":
                        node.update(state="deferred_request", reason="a newer scoped evidence request is active")
                if tree["termination"]:
                    tree.setdefault("termination_history", []).append({"root_id": tree.get("root_id", tree["nodes"][0]["id"]),
                                                                     **tree["termination"]})
                tree["nodes"].append({**root, "id": node_id(root), "depth": 0, "state": "pending"})
                tree.update(root_id=node_id(root), follow_up_ref=self.follow_up_ref, termination=None)
                self._tree_save()
        else:
            root = {"kind": "root", "question": self.score["question"],
                    "protocol_ref": self.protocol["artifact_ref"], "follow_up_ref": self.follow_up_ref}
            self.exploration_tree = {"schema_version": "literature-exploration-1",
                "question": self.score["question"], "protocol_ref": self.protocol["artifact_ref"],
                "root_id": node_id(root), "follow_up_ref": self.follow_up_ref, "nodes": [{**root, "id": node_id(root), "depth": 0, "state": "pending"}],
                "round": 0, "termination": None}
            self._tree_save()

    def _tree_select_reads(self, actions, *, reconsider=False):
        pending = [action for action in actions if not action.get("selection_ref")
                   or (reconsider and action.get("deferred_work_ids"))]
        if not pending:
            return
        candidate_ids = {self.aliases.get(wid, wid) for action in pending
                         for wid in action.get("returned_work_ids", [])} & set(self.works)
        existing = {wid for wid, record in self.analysis_records.items()
                    if any(self._body(record)[field]["text"] is not None for field in MAP_FIELDS)}
        candidates = candidate_ids - existing
        remaining, scopes = self._remaining_model_capacity(["research.search-planner", "research.literature-mapper",
                                                           "methods.work-reviewer"])
        assignment = {"phase": "reading_selection", "question": self.score["question"],
            "inquiries": [{key: action.get(key) for key in ("id", "parent_id", "question", "rationale", "evidence",
                          "query_ref", "returned_work_ids")} for action in pending],
            "candidates": [{"work_id": wid, "work_ref": self.work_records[wid]["artifact_ref"],
                "work": {key: self.works[wid].get(key) for key in ("title", "year", "doi")},
                "sources": [{"source_ref": ref, "representation": source["representation"],
                             "text": source["text"], "available_chars": len(source["text"])}
                            for ref, source in self.source_docs.items()
                            if source["work_id"] == wid and authoritative_source(source)]}
                           for wid in sorted(candidates)],
            "checked_entries": [self._body(self.analysis_records[wid]) for wid in sorted(existing)],
            "resources": {"remaining_model_calls": remaining, "owned_scopes": scopes,
                          "required_decisions": self._required_model_work()},
            "instructions": "Compare the captured candidates against the incoming inquiries and existing checked findings. "
                "Return exactly {rationale:string,candidates:[{work_id,decision:read|defer,rationale:string}]}. "
                "Account for each candidate once. Put read decisions first, ordered by scientific priority. "
                "Keep each rationale a concise selection reason; do not write a literature summary in this decision. "
                "Choose how many and which works need substantive reading to advance the question. "
                "There is no paper-count quota, fixed seed count, or per-depth allocation. Actual calls, tokens, and time are finite; "
                "leave capacity for verification, further inquiry, and required downstream decisions. "
                "A read decision requests source capture, analysis, and independent checking; it is not scientific inclusion or verification. "
                "A defer decision retains the candidate for later consideration. Metadata does not establish "
                "mechanisms, measurements, novelty, or absence. Explain priority and relevance without inventing substantive findings."}
        if self.score.get("design_brief") is not None:
            limit = self.bounds.get("max_analyzed_works", self.bounds["max_works"])
            retained_reads = self._tree_retained_implementation_reads()
            assignment["resources"]["deep_reading"] = {
                "declared_limit": limit, "retained_substantive_count": len(existing),
                "retained_read_count": len(retained_reads),
                "remaining_new_reads": max(0, limit - len(retained_reads)),
            }
            assignment["instructions"] += (
                " This is implementation evidence for a first pilot. The declared deep_reading allowance "
                "applies cumulatively across discovery and counter-search. Prioritize only missing physical "
                "inputs, verification/baseline definitions and closest competing mechanisms. "
                "Defer other candidates; captured catalog records remain available. Order READ decisions by "
                "scientific priority within remaining_new_reads; do not expand a publication bibliography.")
        basis = selection_basis(assignment)
        identity = node_id(basis)
        retained_input = self.store.head("kb/reading-selection-inputs/" + identity)
        if retained_input is None:
            for record in self._heads("kb/reading-selection-inputs/"):
                previous = self._body(record)
                if selection_basis(previous) == basis and self.store.head(
                        "kb/reading-selection-progress/" + record["artifact_id"].split("/")[-1]):
                    retained_input = record
                    identity = record["artifact_id"].split("/")[-1]
                    break
        if retained_input is None:
            self._record("kb/reading-selection-inputs/" + identity, "note", assignment, "research.search-planner",
                         subjects=[action["query_ref"] for action in pending])
        assignment["checked_entries"] = [{"work_id": wid,
            "entry_ref": self.analysis_records[wid]["artifact_ref"],
            "inclusion": entry["inclusion"], "reason": entry["reason"],
            "findings": {field: entry[field]["text"] for field in MAP_FIELDS}}
            for wid in sorted(existing) for entry in [self._body(self.analysis_records[wid])]]
        assignment["inquiries"] = [{key: value for key, value in inquiry.items() if key != "returned_work_ids"}
                                    for inquiry in assignment["inquiries"]]
        for item in assignment["candidates"]:
            abstracts = [source for source in item["sources"] if source["representation"] == "abstract"]
            if abstracts:
                item["sources"] = abstracts
        progress_id = "kb/reading-selection-progress/" + identity
        retained = self.store.head(progress_id)
        progress = self._body(retained) if retained else {
            "rationale": None, "candidates": [], "execution_refs": [], "issues": None,
            "read_priority": [], "priority_complete": not candidates}
        completed = {choice["work_id"] for choice in progress["candidates"]}
        subjects = list(progress["execution_refs"])
        pending_ids = candidates - completed
        for attempt in range(self.config["limits"]["max_rounds"]):
            if not pending_ids and progress["priority_complete"]:
                break
            scoped = deepcopy(assignment)
            repairing = bool(progress["execution_refs"])
            if repairing:
                scoped["candidates"] = [item for item in assignment["candidates"] if item["work_id"] in pending_ids]
                scoped["retained_decisions"] = [deepcopy(choice) for choice in progress["candidates"]
                                                if choice["decision"] == "read"]
                scoped["retained_deferred_count"] = sum(choice["decision"] == "defer"
                                                        for choice in progress["candidates"])
                scoped["validation_feedback"] = {"issues": progress["issues"],
                    "previous_execution_ref": progress["execution_refs"][-1],
                    "scope": "Decide only the candidates supplied in this assignment. Unique valid earlier decisions are retained. "
                             "Resolve contradictory prior decisions explicitly; do not repeat retained or unknown work IDs."}
                scoped["instructions"] += (
                    " Return exactly {rationale:string,candidates:[{work_id,decision:read|defer,rationale:string}],read_priority:[work_id]}. "
                    "Decide only the supplied unresolved candidates; retained_decisions are immutable. "
                    "read_priority must order every retained or newly selected READ work exactly once, by scientific importance. "
                    "Do not put deferred IDs in read_priority. Resolve placement of repaired reads explicitly. "
                    "If no unresolved candidate remains, return candidates:[] and repair only read_priority.")
            candidate_aliases = {f"candidate-{index}": item["work_id"]
                                 for index, item in enumerate(scoped["candidates"])}
            for alias, item in zip(candidate_aliases, scoped["candidates"]):
                item["canonical_work_id"] = item["work_id"]
                item["work_id"] = alias
            reverse = {wid: alias for alias, wid in candidate_aliases.items()}
            if repairing:
                scoped["validation_feedback"]["issues"] = {"unresolved": [
                    {"work_id": reverse.get(row["work_id"], row["work_id"]), "reason": row["reason"]}
                    for row in progress["issues"]["unresolved"]]}
            scoped["instructions"] += (
                " Candidate work_id values are assignment-local handles. Copy them exactly in candidates; "
                "checked_entries are prior context, not candidates to decide. For read_priority, retained READ IDs "
                "remain canonical and new READ IDs use their supplied handles. Never copy citation metadata into a decision.")
            repair_identity = identity if not progress["execution_refs"] else identity + "-repair-" + node_id(
                {key: value for key, value in scoped.items() if key != "resources"})
            model_input_id = "kb/reading-selection-model-inputs/" + repair_identity
            model_input = self.store.head(model_input_id)
            if model_input is not None:
                scoped = self._body(model_input)
            else:
                self._record(model_input_id, "note", scoped, "research.search-planner",
                    subjects=[*progress["execution_refs"], *[action["query_ref"] for action in pending]])
            proposal, execution = self._model_checked("reading-selection-" + repair_identity,
                "research.search-planner", scoped,
                lambda value: validate_reading_proposal(value, require_priority=repairing),
                normalizer=lambda value: bind_reading_candidates(value, candidate_aliases),
                stage="supervision", task_kind="service")
            proposal = bind_reading_candidates(proposal, candidate_aliases)
            valid, issues = reading_selection_parts(proposal, pending_ids)
            progress["rationale"] = progress["rationale"] or proposal["rationale"]
            progress["candidates"].extend(valid)
            progress["execution_refs"].append(execution)
            expected_reads = {choice["work_id"] for choice in progress["candidates"] if choice["decision"] == "read"}
            priority = proposal.get("read_priority", [choice["work_id"] for choice in valid if choice["decision"] == "read"])
            if repairing:
                progress["priority_complete"] = len(priority) == len(set(priority)) and set(priority) == expected_reads
                if not progress["priority_complete"]:
                    issues["priority"] = {"received": priority, "expected_read_ids": sorted(expected_reads)}
            else:
                progress["priority_complete"] = not issues["unresolved"]
            if progress["priority_complete"] or not repairing:
                progress["read_priority"] = priority
            progress["issues"] = issues
            completed.update(choice["work_id"] for choice in valid)
            pending_ids = candidates - completed
            self._record(progress_id, "note", progress, "research.search-planner",
                subjects=[*progress["execution_refs"], *[action["query_ref"] for action in pending]])
            subjects = list(progress["execution_refs"])
            if not pending_ids and (issues["unknown"] or issues["invalid"]):
                # Unknown rows carry no admitted decision. Record the rejection
                # without inferring an identity from spelling or source titles.
                self._record("kb/reading-selection-rejections/" + identity, "note", issues,
                             "command.controller", subjects=[execution])
        if pending_ids or not progress["priority_complete"]:
            raise ModelContractError("reading selection has unresolved decisions or priority: " + json.dumps(
                {"work_ids": sorted(pending_ids), "issues": progress["issues"]}, ensure_ascii=False))
        value = {"rationale": progress["rationale"] or
                 "All returned candidates already have substantive entries or no admitted catalog record.",
                 "candidates": sorted(progress["candidates"], key=lambda choice:
                    (choice["decision"] != "read", progress["read_priority"].index(choice["work_id"])
                     if choice["decision"] == "read" else 0))}
        validate_reading_selection(value, candidates)
        execution = subjects[-1] if subjects else None
        record = self._record("kb/reading-selections/" + identity, "note",
            {**value, "assignment_sha256": identity, "execution_ref": execution, "execution_refs": subjects,
             "candidate_work_refs": [self.work_records[wid]["artifact_ref"] for wid in sorted(candidate_ids)]},
            "research.search-planner", subjects=[*subjects, *[action["query_ref"] for action in pending],
                *[self.work_records[wid]["artifact_ref"] for wid in sorted(candidate_ids)]])
        selected = [choice["work_id"] for choice in value["candidates"] if choice["decision"] == "read"]
        selected.extend(sorted(existing))
        for action in pending:
            available = {self.aliases.get(wid, wid) for wid in action.get("returned_work_ids", [])}
            action.update(selection_ref=record["artifact_ref"], selected_work_ids=[wid for wid in selected if wid in available],
                          deferred_work_ids=sorted(available - set(selected)))
        self._tree_save()
        self._checkpoint("exploration_candidates_selected", force=True)

    def _tree_read(self, actions):
        actions = list(actions)
        reconsider = any(not action.get("selection_ref") for action in actions)
        if reconsider and self.score.get("design_brief") is None:
            seen = {action["id"] for action in actions}
            actions.extend(node for node in self.exploration_tree["nodes"] if node["kind"] == "acquisition"
                           and node.get("deferred_work_ids") and node.get("follow_up_ref") == self.follow_up_ref
                           and node["id"] not in seen)
        self._tree_select_reads(actions, reconsider=reconsider)
        self._tree_admit_implementation_reads(actions)
        self._tree_admitted_reads = {self.aliases.get(wid, wid) for action in actions
                                     for wid in action.get("selected_work_ids", [])}
        decisions = [choice["work_id"] for ref in dict.fromkeys(action["selection_ref"] for action in actions)
                     for choice in self._body(self.store.get(ref))["candidates"] if choice["decision"] == "read"]
        self._tree_read_order = list(dict.fromkeys([*decisions, *[wid for action in actions for wid in action.get("selected_work_ids", [])]]))
        try:
            self._full_texts()
            self._reconcile_identities()
            self._map()
            self._review_work_claims()
        finally:
            self._tree_admitted_reads = None
            self._tree_read_order = None
        for node in self.exploration_tree["nodes"]:
            if node["kind"] == "read" and not self._tree_parent_current(node):
                node["state"] = "superseded"
        existing = {node["id"] for node in self.exploration_tree["nodes"]}
        retained = {node["id"]: node for node in self.exploration_tree["nodes"]
                    if node["kind"] == "acquisition" and node["state"] == "read"}
        retained.update({action["id"]: action for action in actions})
        for action in retained.values():
            for observed in action.get("selected_work_ids", []):
                wid = self.aliases.get(observed, observed)
                entry = self.analysis_records.get(wid)
                review = self.work_reviews.get(wid)
                if not entry or not review:
                    continue
                body = self._body(entry)
                checks = self._body(review)
                if (checks.get("entry_ref") != entry["artifact_ref"]
                        or checks.get("verification_kind") == "deterministic_abstention"
                        or not checks.get("checks")
                        or any(check["outcome"] != "passed" for check in checks["checks"])
                        or not any(body[field]["text"] is not None for field in MAP_FIELDS)):
                    continue
                sources = [ref for ref, source in self.source_docs.items()
                           if source["work_id"] == wid and authoritative_source(source)]
                pin = {"kind": "read", "parent_id": action["id"], "work_id": wid,
                       "work_ref": self.work_records[wid]["artifact_ref"],
                       "entry_ref": entry["artifact_ref"], "review_ref": review["artifact_ref"],
                       "source_refs": sources, "question": action["question"],
                       "inquiry_rationale": action["rationale"], "inquiry_evidence": action.get("evidence", []),
                       "follow_up_ref": action.get("follow_up_ref")}
                identity = node_id(pin)
                if identity not in existing:
                    self.exploration_tree["nodes"].append({**pin, "id": identity,
                        "depth": action["depth"], "state": ("duplicate_basis" if any(
                            node.get("work_ref") == pin["work_ref"] and node.get("entry_ref") == pin["entry_ref"]
                            and node.get("review_ref") == pin["review_ref"]
                            and node.get("follow_up_ref") == pin["follow_up_ref"]
                            for node in self.exploration_tree["nodes"]) else "pending"),
                        "referenced_works": self.works[wid]["referenced_works"]})
                    if pin["follow_up_ref"] != self.follow_up_ref:
                        self.exploration_tree["nodes"][-1].update(
                            state="deferred_request", reason="a newer scoped evidence request is active")
                    existing.add(identity)
            action["state"] = "read" if action.get("selected_work_ids") else "screened"
        if not self._tree_admitted_read_success(actions):
            for action in actions:
                parent = next(node for node in self.exploration_tree["nodes"] if node["id"] == action["parent_id"])
                if (parent.get("decision") == "expand" and parent.get("follow_up_ref") == self.follow_up_ref
                        and self._tree_parent_current(parent)):
                    parent["state"] = "pending"
        self._tree_save()
        self._checkpoint("exploration_read_reviewed", force=True)

    def _tree_admit_implementation_reads(self, actions):
        """Apply the cumulative workload allowance without editing model decisions."""
        if self.score.get("design_brief") is None:
            return
        existing = {wid for wid, record in self.analysis_records.items()
                    if any(self._body(record)[field]["text"] is not None for field in MAP_FIELDS)}
        retained = self._tree_retained_implementation_reads()
        limit = self.bounds.get("max_analyzed_works", self.bounds["max_works"])
        priority = list(dict.fromkeys(choice["work_id"]
            for ref in dict.fromkeys(action["selection_ref"] for action in actions)
            for choice in self._body(self.store.get(ref))["candidates"] if choice["decision"] == "read"))
        admitted = retained | set([wid for wid in priority if wid not in retained][:max(0, limit - len(retained))])
        for action in actions:
            available = {self.aliases.get(wid, wid) for wid in action["returned_work_ids"]}
            requested = list(dict.fromkeys([*[wid for wid in priority if wid in available],
                                             *sorted(retained & available)]))
            deferred = sorted(set(requested) - admitted)
            action.update(selected_work_ids=[wid for wid in requested if wid in admitted],
                deferred_work_ids=sorted(available - admitted),
                reading_admission={"declared_limit": limit,
                    "retained_substantive_refs": sorted(self.analysis_records[wid]["artifact_ref"] for wid in existing),
                    "retained_read_work_ids": sorted(retained),
                    "admitted_work_ids": sorted(set(requested) & admitted),
                    "selection_ref": action["selection_ref"], "budget_deferred_work_ids": deferred})
        self._tree_save()

    def _tree_retained_implementation_reads(self):
        """Count admitted reads, including unsuccessful reads and interrupted siblings."""
        retained = {wid for wid, record in self.analysis_records.items()
                    if any(self._body(record)[field]["text"] is not None for field in MAP_FIELDS)}
        for node in self.exploration_tree["nodes"]:
            if node["kind"] != "acquisition":
                continue
            admission = node.get("reading_admission")
            if admission is not None:
                retained.update(admission["admitted_work_ids"])
            elif node["state"] == "read":
                retained.update(node.get("selected_work_ids", []))
        return {self.aliases.get(wid, wid) for wid in retained}

    def _tree_read_countersearch(self, record, plan):
        """Screen the exact owned search receipts through the ordinary reading path."""
        from scisaurus.runtime.survey import acquisition_succeeded
        root = self.exploration_tree.get("root_id", self.exploration_tree["nodes"][0]["id"])
        retained = {node["id"]: node for node in self.exploration_tree["nodes"]}
        actions = []
        queries = {query_identity(query) for query in plan["queries"]}
        for ref in self.query_refs:
            receipt = self.store.get(ref)
            body = self._body(receipt)
            if (body.get("plan_ref") != record["artifact_ref"]
                    or body.get("role") != "methods.novelty-challenger"
                    or not acquisition_succeeded(body)):
                continue
            request = body.get("request", {})
            if (receipt.get("score_ref") != self.score_ref
                    or receipt.get("author") != "methods.novelty-challenger"
                    or request.get("operation") != "search"
                    or query_identity(request.get("query", "")) not in queries):
                raise StateError("counter-search reading receipt changed its owned score or query")
            pin = {"kind": "acquisition", "parent_id": root,
                   "plan_ref": record["artifact_ref"], "query_ref": ref}
            identity = node_id(pin)
            action = retained.get(identity)
            if action is None:
                action = {**pin, "id": identity, "depth": 1, "state": "captured",
                    "request": deepcopy(body["request"]), "question": self.score["question"],
                    "rationale": plan["rationale"], "follow_up_ref": self.follow_up_ref,
                    "returned_work_ids": list(body["returned_work_ids"]),
                    "new_unique_works": body["new_unique_works"]}
                self.exploration_tree["nodes"].append(action)
            actions.append(action)
        if not actions:
            raise StateError("counter-search has no owned captured receipts to screen")
        self._tree_save()
        self._tree_read(actions)

    def _tree_admitted_read_success(self, actions):
        identifiers = {action["id"] for action in actions}
        return any(node["kind"] == "read" and node["parent_id"] in identifiers
                   and node["state"] != "superseded" for node in self.exploration_tree["nodes"])

    def _tree_recover_action(self, action):
        from scisaurus.runtime.survey import acquisition_succeeded
        request = action["request"]
        matching = []
        for ref, row in zip(self.query_refs, self.search_log):
            if (row.get("request") != request
                    or row.get("provider", "openalex") != self.bibliography_mode
                    or row.get("role") == "methods.novelty-challenger"
                    or not acquisition_succeeded(row)):
                continue
            if request["operation"] == "search":
                plan = self.store.get(row["plan_ref"])
                if self._body(plan).get("follow_up_ref") != action.get("follow_up_ref"):
                    continue
            matching.append((ref, row))
        uncertain = [self._body(record) for record in self._heads("command/api-calls/")
                     if self._body(record).get("tree_action_id") == action["id"]
                     and self._body(record)["number"] not in action.get("failed_reservations", [])]
        owned = [(ref, row) for ref, row in matching if row.get("tree_action_id") == action["id"]
                 or any(self.store.get(ref)["artifact_id"] == f"kb/queries/{reservation['number']}"
                        for reservation in uncertain)]
        if uncertain and not owned:
            raise StateError("exploration acquisition has an unresolved charged reservation; redispatch prohibited")
        if matching:
            ref, result = (owned or matching)[-1]
            action.update(state="captured", query_ref=ref,
                          returned_work_ids=result["returned_work_ids"],
                          receipt_new_unique_works=result["new_unique_works"],
                          new_unique_works=0)
            self._tree_save()
            return True
        return False

    def _tree_acquire(self, actions):
        from scisaurus.runtime.survey import acquisition_succeeded
        # Durable effects are reconciled before admission of any fresh work.
        # Captures already paid for remain readable even at the catalog limit.
        pending = []
        for action in actions:
            if action["state"] != "pending":
                continue
            if self._tree_recover_action(action):
                continue
            if action.get("follow_up_ref") != self.follow_up_ref:
                action.update(state="deferred_request", reason="a newer scoped evidence request is active")
                self._tree_save()
            else:
                pending.append(action)
        recovered = [action for action in actions if action["state"] == "captured"]
        if recovered:
            self._tree_finish_acquisitions(recovered)
        for action in pending:
            if self._tree_recover_action(action):
                continue
            parent = next(node for node in self.exploration_tree["nodes"] if node["id"] == action["parent_id"])
            if not self._tree_parent_current(parent):
                action.update(state="deferred", reason="the planning parent no longer has a current reviewed basis")
                self._tree_save()
                continue
            request = action["request"]
            if self._tree_catalog_capacity() <= 0:
                action.update(state="deferred", reason="declared catalog storage allowance reached")
                self._tree_save()
                continue
            if self.api_calls >= self.bounds["max_api_calls"]:
                action.update(state="deferred", reason="declared bibliography call budget reached")
                self._tree_save()
                continue
            before = len(self.query_refs)
            self._active_tree_action = action["id"]
            try:
                result = self._bibliographic_call(
                    request["operation"], role="research.search-planner",
                    query=request["query"], work_id=request["work_id"], cursor=request["cursor"],
                    result_limit=request["limit"], plan_ref=action["plan_ref"])
            except ProviderRateLimitError:
                action.setdefault("failed_reservations", []).append(self.api_calls)
                self._tree_save()
                raise
            finally:
                self._active_tree_action = None
            ref = self.query_refs[-1] if len(self.query_refs) > before else None
            if not acquisition_succeeded(result):
                action["state"] = "unresolved"
                action["reason"] = "acquisition did not produce a successful query receipt"
                self._tree_save()
                raise ValidationError("exploration acquisition remains unresolved")
            action.update(state="captured", query_ref=ref,
                          returned_work_ids=result["returned_work_ids"],
                          receipt_new_unique_works=result["new_unique_works"],
                          new_unique_works=result["new_unique_works"])
            self._tree_save()
        captured = [action for action in actions if action["state"] == "captured"]
        if captured:
            self._tree_finish_acquisitions(captured)

    def _tree_finish_acquisitions(self, captured):
        self.expansion_log.append({"round": self.exploration_tree["round"],
            "seed_work_ids": sorted({next((node.get("work_id") for node in self.exploration_tree["nodes"]
                                          if node["id"] == action["parent_id"]), None)
                                     for action in captured} - {None}),
            "new_unique_works": sum(action["new_unique_works"] for action in captured),
            "completed": True, "quiet_rounds": 0, "selection": "reviewed_parent_inquiries",
            "query_refs": [action["query_ref"] for action in captured]})
        self._tree_read(captured)

    def _tree_work_current(self, wid):
        return (wid in self.analyzed_basis and wid in self.reviewed_basis
                and self.analyzed_basis[wid] == self._analysis_basis(wid)
                and self._work_review_current(wid))

    def _tree_parent_current(self, node):
        if node["kind"] != "read":
            return node["kind"] == "root"
        wid = node["work_id"]
        return (self._tree_work_current(wid)
                and self.work_records.get(wid, {}).get("artifact_ref") == node["work_ref"]
                and self.analysis_records.get(wid, {}).get("artifact_ref") == node["entry_ref"]
                and self.work_reviews.get(wid, {}).get("artifact_ref") == node["review_ref"])

    def _tree_plan(self, parents, *, suggestions):
        for node in parents:
            if node["kind"] != "read":
                continue
            if not self._tree_parent_current(node):
                raise StateError("exploration parent read basis changed; re-read before branching")
        parent_map = {node["id"]: deepcopy(node) for node in parents}
        sources = {ref: source for ref, source in self.source_docs.items()
                   if any(ref in node.get("source_refs", []) for node in parents)}
        parent_aliases = {f"parent-{index}": node["id"] for index, node in enumerate(parents)}
        assignments = [planning_parent(node, alias, **{
            field: self._body(self.store.get(node[ref])) for field, ref in (
                ("work", "work_ref"), ("entry", "entry_ref"), ("review", "review_ref"))}
            if node["kind"] == "read" else {}) for alias, node in zip(parent_aliases, parents)]
        targets = citation_targets(parent_map, parent_aliases)
        reverse_parents = {identity: alias for alias, identity in parent_aliases.items()}
        for parent in assignments:
            if parent["kind"] == "read":
                parent["citation_targets"] = [
                    {"handle": handle, "operation": target["operation"], "work_id": target["work_id"]}
                    for handle, target in targets.items()
                    if reverse_parents[target["parent_id"]] == parent["id"]]
                parent.pop("referenced_works", None)
        evidence = [proof for parent in assignments if parent["kind"] == "read"
                    for field in MAP_FIELDS for proof in parent["entry"][field]["evidence"]]
        for proof in evidence:
            source = sources.get(proof["source_ref"])
            if source is None:
                raise ValidationError("checked exploration finding has no assigned captured source")
            validate_span(proof, source, require_span=True)
        context = self._project_assessment_sources(
            [source for source in self._assessment_source_context() if source["source_ref"] in sources],
            full_text_chars=self.bounds["context_chars"], abstract_chars=self.bounds["context_chars"],
            unverified_chars=self.bounds["context_chars"], evidence=evidence)
        windows = {source["source_ref"]: source["window"] for source in context}
        assignments, evidence_catalog = index_evidence(assignments, sources)
        branch_limit = None
        remaining, scopes = self._remaining_model_capacity(["research.search-planner", "research.literature-mapper", "methods.work-reviewer"])
        assignment = {"phase": "exploration_plan", "question": self.score["question"],
            "response_contract": exploration_response_contract(),
            "parents": assignments, "sources": context, "evidence_catalog": evidence_catalog,
            "suggestions": suggestions,
            "max_branches": branch_limit, "search_syntax": self._tree_search_syntax(),
            "allowed_operations": {"root": ["search", "work"], "read": ["search", "work", "citing"]},
            "remaining_catalog_capacity": self._tree_catalog_capacity(),
            "resources": {"remaining_model_calls": remaining, "owned_scopes": scopes,
                          "remaining_bibliography_calls": max(0, self.bounds["max_api_calls"] - self.api_calls),
                          "required_decisions": self._required_model_work()},
            "acquisition_history": [{key: node.get(key) for key in ("request", "state", "question", "parent_id", "query_ref", "reason",
                                                                 "selection_ref", "selected_work_ids")}
                                    for node in self.exploration_tree["nodes"] if node["kind"] == "acquisition"],
            "instructions": "Choose prioritized inquiries that advance the declared research question. "
                "Return exactly the fields and enum values in response_contract. Include every required branch field, "
                "including evidence:[] for a root and null for an inactive query or work_id. "
                "Copy parent_id exactly from the assigned parent's id handle. "
                "These handles are local to this assignment; work IDs and acquisition history IDs are not parent handles. "
                "For a root, choose initial searches or direct canonical OpenAlex work lookups from the scientific intake, with empty evidence. "
                "For a read, explain what its checked findings suggest investigating next, with exact parent quotations. "
                "Each branch parent_id must identify the work supplying its evidence: use that parent's allowed_evidence "
                "and its source_refs in the shared sources table. Each captured source is supplied once. "
                "Select checked evidence as [{evidence_id:ID}] from evidence_catalog; exact quotations and source "
                "locators are attached deterministically. Each ID must belong to the assigned parent. "
                "For new evidence absent from the catalog, supply an exact {work_id,source_ref,quote} object "
                "copied from the displayed parent source window. "
                "Source windows retain every checked finding's cited span; copy new quotations only from these displayed windows. "
                "Another parent's source cannot support a branch attached to this parent. Incoming question and inquiry_rationale "
                "describe how the work was found; they do not grant evidence ownership. Close irrelevant parents instead of using "
                "them to carry another work's findings. Use each parent's entry for its checked findings. "
                "An unresolved research question is not a source-stated limitation; abstract silence cannot prove absence. "
                "For a read, copy work_id from the matching parent's citation_targets.handle for work or citing. "
                "Each handle binds the parent, canonical work and acquisition direction; never use another parent's handle. "
                "A work mentioned in prose but absent from citation_targets requires a justified search query, "
                "not an invented citation link. Root direct lookups still use canonical work IDs from scientific intake. "
                "Use diverse terminology or mechanism-specific searches when needed, not only citation neighbors. "
                "Continue or close each parent's incoming inquiry using its question, inquiry_rationale and its own checked findings. "
                "Branches are ordered by scientific priority. Stop closes only the assigned parents. "
                "Choose the number of inquiries by unresolved scientific needs, not a fixed seed count or breadth/depth quota. "
                "max_branches is null: prioritize scientifically justified inquiries. "
                "Remaining calls bound actual uncached dispatch, while captured receipts can be reused. "
                "Stop with a reason when further acquisition would not improve their evidence. No novelty verdict."}
        assignment = self._follow_up_assignment(assignment)
        identity = node_id({"assignment": {key: value for key, value in assignment.items() if key != "resources"},
                            "parent_ids": list(parent_map)})
        retained_input = self.store.head("kb/exploration-inputs/" + identity)
        if retained_input:
            assignment = self._body(retained_input)
        else:
            self._record("kb/exploration-inputs/" + identity, "note", assignment, "research.search-planner",
                         subjects=[self.protocol["artifact_ref"], *[node["review_ref"] for node in parents if node.get("review_ref")]])
        normalizer = lambda value: normalize_plan(value, parent_aliases, sources, windows=windows,
            parents=parent_map, evidence_catalog=assignment.get("evidence_catalog", []))
        validator = lambda value: validate_plan(value, parent_map, sources, max_branches=branch_limit)
        value, execution = self._model_checked("exploration-" + identity, "research.search-planner",
            assignment, validator, normalizer=normalizer, stage="supervision", task_kind="service")
        record = self._record("kb/exploration-plans/" + identity, "note",
            {**value, "question": self.score["question"], "parent_ids": list(parent_map),
             "parent_bindings": parent_aliases,
             "queries": [branch["query"] for branch in value["branches"] if branch["operation"] == "search"],
             **({"follow_up_ref": self.follow_up_ref} if self.work_orders else {}),
             "assignment_sha256": identity, "execution_ref": execution},
            "research.search-planner", subjects=[execution, *([self.follow_up_ref] if self.work_orders else []),
                *[node["entry_ref"] for node in parents if node.get("entry_ref")],
                *[node["review_ref"] for node in parents if node.get("review_ref")], *sources])
        return value, record["artifact_ref"]

    @staticmethod
    def _tree_search_syntax():
        from scisaurus.runtime.survey import SEARCH_SYNTAX
        return SEARCH_SYNTAX

    def _tree_catalog_capacity(self):
        reserve = self.bounds.get("challenge_reserve", 0)
        return max(0, self.bounds["max_works"] - reserve - len(self.works))

    def _tree_stop(self, reason):
        for node in self.exploration_tree["nodes"]:
            if node["kind"] == "acquisition" and node["state"] == "pending":
                node.update(state="deferred", reason=reason)
        self.exploration_tree["termination"] = {"reason": reason,
            "remaining_catalog_capacity": self._tree_catalog_capacity(),
            "exhaustive_coverage": False}
        self._tree_save()

    def _explore(self):
        self._tree_load()
        tree = self.exploration_tree
        reopened = (set(self.resume_session["reopened_scopes"]) if self.resume_session else set())
        stale_parents = {node["parent_id"] for node in tree["nodes"]
                         if node["kind"] == "read" and node["state"] != "superseded"
                         and not self._tree_parent_current(node)}
        reads = [node for node in tree["nodes"] if node["kind"] == "acquisition" and node["state"] == "read"
                 and (reopened & {"mapping", "focused_review", "retrieval"} or node["id"] in stale_parents
                      or any(not self._tree_work_current(self.aliases.get(wid, wid))
                             for wid in node.get("selected_work_ids", [])))]
        if reads:
            previous_ids = {node["id"] for node in tree["nodes"]}
            self._tree_read(reads)
            if any(node["id"] not in previous_ids and node["kind"] == "read" for node in tree["nodes"]):
                tree["termination"] = None
                self._tree_save()
        if tree["termination"]:
            return
        root = next(node for node in tree["nodes"] if node["id"] == tree.get("root_id", tree["nodes"][0]["id"]))
        if root["state"] == "pending" and self.score["seed_work_ids"]:
            for wid in self.score["seed_work_ids"]:
                pin = {"kind": "acquisition", "parent_id": root["id"],
                       "request": {"operation": "work", "query": None, "work_id": wid,
                                   "cursor": None, "limit": 1}}
                if not any(node["id"] == node_id(pin) for node in tree["nodes"]):
                    tree["nodes"].append({**pin, "id": node_id(pin), "depth": 1,
                        "state": "pending", "plan_ref": self.protocol["artifact_ref"], "follow_up_ref": self.follow_up_ref,
                        "question": self.score["question"], "rationale": "Declared seed work"})
            self._tree_save()
        while True:
            self._ensure_active()
            if any(node["state"] == "unresolved" for node in tree["nodes"]):
                raise StateError("exploration contains unresolved acquisition effects")
            actions = [node for node in tree["nodes"] if node["kind"] == "acquisition"
                       and node["state"] in {"pending", "captured"}]
            if actions:
                self._tree_acquire(actions)
                if tree["termination"]:
                    return
            frontier = [node for node in tree["nodes"] if node["kind"] in {"root", "read"}
                        and node["state"] == "pending"
                        and node.get("follow_up_ref") == self.follow_up_ref]
            if self.score.get("design_brief") is not None:
                for node in frontier:
                    if node["depth"] > self.bounds["expansion_rounds"]:
                        node.update(state="deferred", reason="declared implementation evidence expansion depth reached")
                frontier = [node for node in frontier if node["state"] == "pending"]
                self._tree_save()
            queued = any(node["kind"] == "acquisition" and node["state"] == "pending" for node in tree["nodes"])
            if not frontier and any(node["kind"] == "acquisition" and node["state"] == "pending" for node in tree["nodes"]):
                if self._tree_catalog_capacity() > 0 and self.api_calls < self.bounds["max_api_calls"]:
                    continue
            if not frontier:
                self._tree_stop("no unexpanded reviewed reads remain")
                return
            if self._tree_catalog_capacity() <= 0:
                self._tree_stop("declared catalog storage allowance reached")
                return
            remaining, _ = self._remaining_model_capacity(["research.search-planner", "research.literature-mapper",
                                                          "methods.work-reviewer"])
            if remaining is not None and remaining < len(self._required_model_work()) + 4:
                self._tree_stop("model capacity reserved for required downstream decisions")
                return
            if self.api_calls >= self.bounds["max_api_calls"]:
                self._tree_stop("declared bibliography call budget reached")
                return
            parents = ([frontier[0]] if frontier[0]["kind"] == "root" else
                       frontier)
            suggestions = []
            if parents[0]["kind"] == "root":
                planning_calls = sum(self.store.head("kb/search-plans/" + self._initial_plan_id(role)) is None
                                     for role in self._initial_search_roles())
                if remaining is not None and remaining < len(self._required_model_work()) + planning_calls + 4:
                    self._tree_stop("model capacity reserved for initial planning and checked reading")
                    return
                plans = self._initial_plans()
                suggestions = list(dict.fromkeys([*self.score["seed_queries"], *[q for _, queries, _ in plans for q in queries]]))
            value, ref = self._tree_plan(parents, suggestions=suggestions)
            for branch in value["branches"]:
                request = {"operation": branch["operation"], "query": branch["query"],
                           "work_id": branch["work_id"], "cursor": None,
                           "limit": 1 if branch["operation"] == "work" else self.bounds["results_per_query"]}
                pin = {"kind": "acquisition", "parent_id": branch["parent_id"], "request": request}
                if not any(node["id"] == node_id(pin) for node in tree["nodes"]):
                    parent = next(node for node in parents if node["id"] == branch["parent_id"])
                    tree["nodes"].append({**pin, "id": node_id(pin), "depth": parent["depth"] + 1,
                        "state": "pending", "follow_up_ref": self.follow_up_ref,
                        "plan_ref": ref, "question": branch["question"],
                        "rationale": branch["rationale"], "evidence": branch["evidence"]})
            used_parents = {branch["parent_id"] for branch in value["branches"]}
            for parent in parents:
                if parent["id"] in used_parents or value["decision"] == "stop":
                    parent.update(state="decided", plan_ref=ref, decision=value["decision"], rationale=value["rationale"])
            tree["round"] += 1
            self._tree_save()
            self._checkpoint("exploration_branches_planned", force=True)
            if value["decision"] == "stop":
                continue
