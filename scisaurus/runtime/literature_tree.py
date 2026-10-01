"""Evidence-driven acquisition with immutable read and query lineage."""

from copy import deepcopy
import hashlib
import os
import tempfile

from scisaurus.core.schema import canonical_bytes
from scisaurus.core.errors import ProviderRateLimitError, StateError, ValidationError
from scisaurus.core.source_spans import bind, validate as validate_span
from scisaurus.runtime.survey_config import search_query
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
    if (not isinstance(branches, list) or len(branches) > max_branches
            or bool(branches) != (value["decision"] == "expand")):
        raise ValidationError("exploration branches must match the decision and declared bound")
    seen = set()
    for branch in branches:
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
            if parent["kind"] != "read" or not isinstance(branch["work_id"], str) or not branch["work_id"]:
                raise ValidationError("citation branches require a concrete reviewed parent work")
            if branch["query"] is not None:
                raise ValidationError("citation branch cannot supply a search query")
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
                raise ValidationError("branch evidence is outside its reviewed parent's source scope")
            source = sources.get(proof["source_ref"])
            if (source is None or proof.get("work_id") != parent["work_id"]
                    or not authoritative_source(source)):
                raise ValidationError("branch evidence does not identify an authoritative parent source")
            validate_span(proof, source, require_span=True)
        key = (branch["parent_id"], operation, branch["query"], branch["work_id"])
        if key in seen:
            raise ValidationError("exploration plan repeats the same parent acquisition")
        seen.add(key)


class LiteratureTree:
    """Survey orchestration mixin; scientific branch choices belong to model jobs."""

    def _tree_save(self):
        refs = {self.protocol["artifact_ref"]}
        for node in self.exploration_tree["nodes"]:
            refs.update(node[field] for field in ("work_ref", "entry_ref", "review_ref", "plan_ref", "query_ref", "follow_up_ref")
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

    def _tree_read(self, actions):
        self._full_texts()
        self._reconcile_identities()
        self._tree_admitted_reads = {self.aliases.get(wid, wid) for action in actions
                                     for wid in action.get("returned_work_ids", [])}
        try:
            self._map()
            self._review_work_claims()
        finally:
            self._tree_admitted_reads = set()
        for node in self.exploration_tree["nodes"]:
            if node["kind"] == "read" and (
                    self.work_records.get(node["work_id"], {}).get("artifact_ref") != node["work_ref"]
                    or self.analysis_records.get(node["work_id"], {}).get("artifact_ref") != node["entry_ref"]
                    or self.work_reviews.get(node["work_id"], {}).get("artifact_ref") != node["review_ref"]):
                node["state"] = "superseded"
        existing = {node["id"] for node in self.exploration_tree["nodes"]}
        for action in actions:
            for observed in action.get("returned_work_ids", []):
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
            action["state"] = "read"
        self._tree_save()
        self._checkpoint("exploration_read_reviewed", force=True)

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
        depth = min((action["depth"] for action in pending), default=1)
        rounds_left = max(1, 2 + self.bounds["expansion_rounds"] - depth)
        remaining_slots = self._tree_remaining_slots()
        batch = (remaining_slots + rounds_left - 1) // rounds_left
        capacity, _ = self._remaining_model_capacity(["research.literature-mapper", "methods.work-reviewer"])
        if capacity is not None:
            batch = min(batch, max(0, (capacity - len(self._required_model_work())) // 2))
        if batch <= 0 and pending:
            self._tree_stop("acquisition capacity reserved or exhausted")
            return
        captured_before = len(self.works)
        for action in pending:
            if self._tree_recover_action(action):
                continue
            request = action["request"]
            if len(self.works) - captured_before >= batch:
                break
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
                                  "review": self._body(self.store.get(node["review_ref"]))}
                                 if node["kind"] == "read" else {})} for node in parents]
        branch_limit = (self.bounds["queries_per_role"] if parents[0]["kind"] == "root" else
                        self.bounds["queries_per_role"] + self.bounds["references_per_work"] + 1)
        assignment = {"phase": "exploration_plan", "question": self.score["question"],
            "parents": assignments, "sources": context, "suggestions": suggestions,
            "max_branches": branch_limit, "search_syntax": self._tree_search_syntax(),
            "remaining_analysis_slots": self._tree_remaining_slots(),
            "acquisition_history": [{key: node.get(key) for key in ("request", "state", "question", "parent_id", "query_ref", "reason")}
                                    for node in self.exploration_tree["nodes"] if node["kind"] == "acquisition"],
            "instructions": "Choose prioritized inquiries that advance the declared research question. "
                "Return exactly {decision:expand|stop,rationale:string,branches:[{parent_id,question,rationale,"
                "operation:search|work|citing,query:string|null,work_id:string|null,"
                "evidence:[{work_id,source_ref,quote}]}]}. Use assigned parent IDs. "
                "For a root, choose initial searches from the scientific intake and return no evidence. "
                "For a read, explain what its checked findings suggest investigating next, with exact parent quotations. "
                "An unresolved research question is not a source-stated limitation; abstract silence cannot prove absence. "
                "Work lookups must follow actual parent references; citing must use the parent work ID. "
                "Use diverse terminology or mechanism-specific searches when needed, not only citation neighbors. "
                "Continue each parent incoming inquiry using its question, rationale and evidence. "
                "Branches are ordered by scientific priority. Stop closes only the assigned parents. "
                "Stop with a reason when further acquisition would not improve their evidence. No novelty verdict."}
        identity = node_id(assignment)
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

    def _tree_remaining_slots(self):
        reserve = self.bounds.get("challenge_reserve", 0)
        analyzed = sum(any(self._body(record)[field]["text"] is not None for field in MAP_FIELDS)
                       for record in self.analysis_records.values())
        return max(0, min(self.bounds["max_works"] - reserve - len(self.works),
                          self.bounds.get("max_analyzed_works", self.bounds["max_works"]) - reserve - analyzed))

    def _tree_stop(self, reason):
        for node in self.exploration_tree["nodes"]:
            if node["kind"] == "acquisition" and node["state"] == "pending":
                node.update(state="deferred", reason=reason)
        self.exploration_tree["termination"] = {"reason": reason,
            "remaining_analysis_slots": self._tree_remaining_slots(),
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
                if self._tree_remaining_slots() > 0 and self.api_calls < self.bounds["max_api_calls"]:
                    continue
            if not frontier:
                self._tree_stop("no unexpanded reviewed reads remain")
                return
            if self._tree_remaining_slots() <= 0:
                self._tree_stop("declared deep-analysis budget reached")
                return
            for node in frontier:
                if node["kind"] == "read" and node["depth"] >= 1 + self.bounds["expansion_rounds"]:
                    node.update(state="depth_limit", reason="declared expansion depth reached")
            frontier = [node for node in frontier if node["state"] == "pending"]
            if not frontier:
                if queued:
                    continue
                self._tree_stop("declared expansion depth reached")
                return
            expanded_parents = sum(node["kind"] == "read" and node.get("decision") == "expand"
                                   for node in tree["nodes"])
            parent_slots = max(0, self.bounds["expansion_seed_count"] - expanded_parents)
            if frontier[0]["kind"] != "root" and not parent_slots:
                if queued:
                    continue
                self._tree_stop("declared expansion parent budget reached")
                return
            remaining, _ = self._remaining_model_capacity(["research.search-planner", "research.literature-mapper",
                                                          "methods.work-reviewer"])
            if remaining is not None and remaining < len(self._required_model_work()) + 3:
                self._tree_stop("model capacity reserved for required downstream decisions")
                return
            if self.api_calls >= self.bounds["max_api_calls"]:
                self._tree_stop("declared bibliography call budget reached")
                return
            parents = ([frontier[0]] if frontier[0]["kind"] == "root" else
                       frontier[:parent_slots])
            suggestions = []
            if parents[0]["kind"] == "root":
                planning_calls = sum(self.store.head("kb/search-plans/" + self._initial_plan_id(role)) is None
                                     for role in SEARCH_PLANNERS)
                if remaining is not None and remaining < len(self._required_model_work()) + planning_calls + 3:
                    self._tree_stop("model capacity reserved for initial planning and checked reading")
                    return
                plans = self._initial_plans()
                suggestions = [*self.score["seed_queries"], *[q for _, queries, _ in plans for q in queries]]
            value, ref = self._tree_plan(parents, suggestions=suggestions)
            rounds_left = max(1, 1 + self.bounds["expansion_rounds"] - parents[0]["depth"])
            batch = max(1, (self._tree_remaining_slots() + rounds_left - 1) // rounds_left)
            remaining, _ = self._remaining_model_capacity(["research.literature-mapper", "methods.work-reviewer"])
            if remaining is not None:
                batch = min(batch, max(0, (remaining - len(self._required_model_work())) // 2))
            batch = min(batch, max(0, self.bounds["max_api_calls"] - self.api_calls))
            active_count = min(batch, len(value["branches"]))
            per_action = divmod(batch, active_count) if active_count else (0, 0)
            before = len(tree["nodes"])
            for index, branch in enumerate(value["branches"]):
                limit = min(self.bounds["results_per_query"], per_action[0] + (index < per_action[1]))
                request = {"operation": branch["operation"], "query": branch["query"],
                           "work_id": branch["work_id"], "cursor": None,
                           "limit": 1 if branch["operation"] == "work" else max(1, limit)}
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
            if not active_count:
                self._tree_stop("acquisition capacity reserved or exhausted")
                return
            if len(tree["nodes"]) == before:
                continue
