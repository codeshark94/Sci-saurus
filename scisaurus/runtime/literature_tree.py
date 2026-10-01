"""Evidence-driven acquisition with immutable read and query lineage."""

from copy import deepcopy
import hashlib
import os
import tempfile

from scisaurus.core.schema import canonical_bytes
from scisaurus.core.errors import ProviderRateLimitError, StateError, ValidationError
from scisaurus.core.source_spans import bind, validate as validate_span
from scisaurus.runtime.survey_config import search_query, work_id
from scisaurus.runtime.survey_records import MAP_FIELDS, authoritative_source


SEARCH_PLANNERS = ("research.search-planner", "methods.blind-search-planner")


def node_id(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_plan(value, parents, sources, *, max_branches):
    if not isinstance(value, dict) or set(value) != {"decision", "rationale", "branches"}:
        raise ValidationError("exploration plan requires decision, rationale, and branches")
    if value["decision"] not in {"expand", "stop"}:
        raise ValidationError("exploration decision must be expand or stop")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ValidationError("exploration decision requires a rationale")
    branches = value["branches"]
    if (not isinstance(branches, list) or (max_branches is not None and len(branches) > max_branches)
            or bool(branches) != (value["decision"] == "expand")):
        raise ValidationError("exploration branches must match the decision and declared bound")
    seen = set()
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict) or set(branch) != {
                "parent_id", "question", "rationale", "operation", "query", "work_id", "evidence"}:
            raise ValidationError("exploration branch has an invalid envelope")
        parent = parents.get(branch["parent_id"])
        if parent is None:
            raise ValidationError("exploration branch identifies an unassigned parent")
        for field in ("question", "rationale"):
            if not isinstance(branch[field], str) or not branch[field].strip():
                raise ValidationError("exploration branch requires a question and rationale")
        operation = branch["operation"]
        if operation == "search":
            search_query(branch["query"])
            if branch["work_id"] is not None:
                raise ValidationError("search branch cannot identify a work lookup")
        elif operation in {"work", "citing"}:
            work_id(branch["work_id"])
            if branch["query"] is not None:
                raise ValidationError("work and citing branches cannot supply a search query")
            if parent["kind"] == "root":
                if operation != "work":
                    raise ValidationError("citing branches require a concrete reviewed parent work")
            else:
                allowed = parent.get("referenced_works", []) if operation == "work" else [parent.get("work_id")]
                if branch["work_id"] not in allowed:
                    raise ValidationError("citation branch must follow its parent's actual citation metadata")
        else:
            raise ValidationError("unsupported exploration acquisition operation")
        evidence = branch["evidence"]
        if not isinstance(evidence, list) or bool(evidence) != (parent["kind"] == "read"):
            raise ValidationError("a read-driven branch requires captured parent evidence")
        allowed_refs = set(parent.get("source_refs", []))
        for proof in evidence:
            if not isinstance(proof, dict) or proof.get("source_ref") not in allowed_refs:
                raise ValidationError(f"branch {index} parent {branch['parent_id']} owns work {parent.get('work_id')} "
                                      f"and allows sources {sorted(allowed_refs)}; branch evidence is outside its reviewed parent's source scope")
            source = sources.get(proof["source_ref"])
            if (source is None or proof.get("work_id") != parent["work_id"]
                    or not authoritative_source(source)):
                raise ValidationError("branch evidence does not identify an authoritative parent source")
            validate_span(proof, source, require_span=True)
        key = (branch["parent_id"], operation, branch["query"], branch["work_id"])
        if key in seen:
            raise ValidationError("exploration plan repeats the same parent acquisition")
        seen.add(key)


def validate_reading_selection(value, candidates):
    if not isinstance(value, dict) or set(value) != {"rationale", "candidates"}:
        raise ValidationError("reading selection requires rationale and candidates")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ValidationError("reading selection requires a rationale")
    if not isinstance(value["candidates"], list):
        raise ValidationError("reading decisions must be a list")
    seen = set()
    for choice in value["candidates"]:
        if not isinstance(choice, dict) or set(choice) != {"work_id", "decision", "rationale"}:
            raise ValidationError("reading choice requires work_id, decision, and rationale")
        wid = choice["work_id"]
        if not isinstance(wid, str) or wid not in candidates or wid in seen:
            raise ValidationError("reading choice must identify one captured candidate exactly once")
        if choice["decision"] not in {"read", "defer"}:
            raise ValidationError("reading decision must be read or defer")
        if not isinstance(choice["rationale"], str) or not choice["rationale"].strip():
            raise ValidationError("reading choice requires a rationale")
        seen.add(wid)
    if seen != set(candidates):
        raise ValidationError("reading selection must account for every captured candidate")


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
            if tree.get("follow_up_ref") != self.follow_up_ref:
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
        # Text windows share the declared context surface; catalog metadata is
        # retained in full and never becomes evidence for substantive claims.
        source_limit = max(1, self.bounds["context_chars"] // max(1, len(candidates)))
        assignment = {"phase": "reading_selection", "question": self.score["question"],
            "inquiries": [{key: action.get(key) for key in ("id", "parent_id", "question", "rationale", "evidence",
                          "query_ref", "returned_work_ids")} for action in pending],
            "candidates": [{"work_id": wid, "work_ref": self.work_records[wid]["artifact_ref"],
                "work": {key: self.works[wid].get(key) for key in ("title", "year", "doi", "referenced_works")},
                "sources": [{"source_ref": ref, "representation": source["representation"],
                             "text": source["text"][:source_limit], "available_chars": len(source["text"])}
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
                "A defer decision retains the candidate for later consideration. Metadata and truncated abstracts do not establish "
                "mechanisms, measurements, novelty, or absence. Explain priority and relevance without inventing substantive findings."}
        identity = node_id({key: value for key, value in assignment.items() if key != "resources"})
        retained_input = self.store.head("kb/reading-selection-inputs/" + identity)
        if retained_input:
            assignment = self._body(retained_input)
        else:
            self._record("kb/reading-selection-inputs/" + identity, "note", assignment, "research.search-planner",
                         subjects=[action["query_ref"] for action in pending])
        if candidates:
            value, execution = self._model_checked("reading-selection-" + identity, "research.search-planner",
                assignment, lambda value: validate_reading_selection(value, candidates), stage="supervision", task_kind="service")
            subjects = [execution]
        else:
            value = {"rationale": "All returned candidates already have substantive entries or no admitted catalog record.",
                     "candidates": []}
            execution = None
            subjects = []
        record = self._record("kb/reading-selections/" + identity, "note",
            {**value, "assignment_sha256": identity, "execution_ref": execution,
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
        if reconsider:
            seen = {action["id"] for action in actions}
            actions.extend(node for node in self.exploration_tree["nodes"] if node["kind"] == "acquisition"
                           and node.get("deferred_work_ids") and node.get("follow_up_ref") == self.follow_up_ref
                           and node["id"] not in seen)
        self._tree_select_reads(actions, reconsider=reconsider)
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
            if node["kind"] == "read" and (
                    self.work_records.get(node["work_id"], {}).get("artifact_ref") != node["work_ref"]
                    or self.analysis_records.get(node["work_id"], {}).get("artifact_ref") != node["entry_ref"]
                    or self.work_reviews.get(node["work_id"], {}).get("artifact_ref") != node["review_ref"]):
                node["state"] = "superseded"
        existing = {node["id"] for node in self.exploration_tree["nodes"]}
        for action in actions:
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
                if parent.get("decision") == "expand" and parent.get("follow_up_ref") == self.follow_up_ref:
                    parent["state"] = "pending"
        self._tree_save()
        self._checkpoint("exploration_read_reviewed", force=True)

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

    def _tree_plan(self, parents, *, suggestions):
        for node in parents:
            if node["kind"] != "read":
                continue
            if (self.work_records.get(node["work_id"], {}).get("artifact_ref") != node["work_ref"]
                    or self.analysis_records.get(node["work_id"], {}).get("artifact_ref") != node["entry_ref"]
                    or self.work_reviews.get(node["work_id"], {}).get("artifact_ref") != node["review_ref"]):
                raise StateError("exploration parent read basis changed; re-read before branching")
        parent_map = {node["id"]: deepcopy(node) for node in parents}
        sources = {ref: source for ref, source in self.source_docs.items()
                   if any(ref in node.get("source_refs", []) for node in parents)}
        context = [source for source in self._source_context() if source["source_ref"] in sources]
        windows = {source["source_ref"]: source["window"] for source in context}
        assignments = [{**node, **({"work": self._body(self.store.get(node["work_ref"])), "entry": self._body(self.store.get(node["entry_ref"])),
                                  "review": self._body(self.store.get(node["review_ref"])),
                                  "allowed_evidence": {"work_id": node["work_id"], "source_refs": node["source_refs"]},
                                  "sources": [source for source in context if source["source_ref"] in node["source_refs"]]}
                                 if node["kind"] == "read" else {})} for node in parents]
        branch_limit = None
        remaining, scopes = self._remaining_model_capacity(["research.search-planner", "research.literature-mapper", "methods.work-reviewer"])
        assignment = {"phase": "exploration_plan", "question": self.score["question"],
            "parents": assignments, "sources": context, "suggestions": suggestions,
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
                "Return exactly {decision:expand|stop,rationale:string,branches:[{parent_id,question,rationale,"
                "operation:search|work|citing,query:string|null,work_id:string|null,"
                "evidence:[{work_id,source_ref,quote}]}]}. Use assigned parent IDs. "
                "For a root, choose initial searches or direct canonical OpenAlex work lookups from the scientific intake, with empty evidence. "
                "For a read, explain what its checked findings suggest investigating next, with exact parent quotations. "
                "Each branch parent_id must identify the work supplying its evidence: use that parent's allowed_evidence and sources. "
                "Another parent's source cannot support a branch attached to this parent. Incoming inquiry_evidence explains its history, "
                "not the allowed evidence for a new branch. Close irrelevant parents instead of using them to carry another work's findings. "
                "An unresolved research question is not a source-stated limitation; abstract silence cannot prove absence. "
                "For a read, work lookups follow actual parent references; citing uses the checked parent work ID. "
                "Use diverse terminology or mechanism-specific searches when needed, not only citation neighbors. "
                "Continue each parent incoming inquiry using its question, rationale and evidence. "
                "Branches are ordered by scientific priority. Stop closes only the assigned parents. "
                "Choose the number of inquiries by unresolved scientific needs, not a fixed seed count or breadth/depth quota. "
                "max_branches is null: prioritize scientifically justified inquiries. "
                "Remaining calls bound actual uncached dispatch, while captured receipts can be reused. "
                "Stop with a reason when further acquisition would not improve their evidence. No novelty verdict."}
        identity = node_id({key: value for key, value in assignment.items() if key != "resources"})
        retained_input = self.store.head("kb/exploration-inputs/" + identity)
        if retained_input:
            assignment = self._body(retained_input)
        else:
            self._record("kb/exploration-inputs/" + identity, "note", assignment, "research.search-planner",
                         subjects=[self.protocol["artifact_ref"], *[node["review_ref"] for node in parents if node.get("review_ref")]])
        normalizer = lambda value: bind(value, sources, windows=windows)
        validator = lambda value: validate_plan(value, parent_map, sources, max_branches=branch_limit)
        value, execution = self._model_checked("exploration-" + identity, "research.search-planner",
            assignment, validator, normalizer=normalizer, stage="supervision", task_kind="service")
        record = self._record("kb/exploration-plans/" + identity, "note",
            {**value, "question": self.score["question"], "parent_ids": list(parent_map),
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
        if self.resume_session and set(self.resume_session["reopened_scopes"]) & {"mapping", "focused_review", "retrieval"}:
            previous_ids = {node["id"] for node in tree["nodes"]}
            self._tree_read([node for node in tree["nodes"] if node["kind"] == "acquisition" and node["state"] == "read"])
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
                                     for role in SEARCH_PLANNERS)
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
