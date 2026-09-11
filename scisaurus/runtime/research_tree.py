"""Bounded, evidence-gated research branches.

This is the small control-plane piece Sci-saurus takes from progressive tree
search systems: hypotheses can be explored in parallel, every branch points to
an immutable parent, and promotion is a separate operation that requires
independent evidence.  The module deliberately does not execute model-written
code or rank branches by an uncalibrated model score.
"""
from __future__ import annotations

from copy import deepcopy
import html
import math
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


TREE_SCHEMA_VERSION = "research-tree-1"
NODE_STATUSES = {"proposed", "running", "verified", "pruned", "blocked", "promoted"}
SELECTION_POLICIES = {"evidence_gated", "principal_selected"}
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _refs(value, name, *, allow_empty=True):
    if not isinstance(value, list) or len(value) != len(set(value)):
        raise ValidationError(f"{name} must be a list of unique strings")
    if not allow_empty and not value:
        raise ValidationError(f"{name} must be nonempty")
    for item in value:
        _text(item, name)
    return value


def _metrics(value):
    if not isinstance(value, dict):
        raise ValidationError("research node metrics must be an object")
    for key, item in value.items():
        _identifier(key, "research metric id")
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            raise ValidationError("research metrics must be scalar values")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValidationError("research metrics cannot contain nonfinite values")
    return value


def _node(value, *, node_ids=None):
    fields = {"id", "parent_id", "depth", "stage", "hypothesis", "plan", "seed",
              "status", "input_refs", "evidence_refs", "metrics", "failure_reason"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"research node requires exactly {sorted(fields)}")
    _identifier(value["id"], "research node id")
    if value["parent_id"] is not None:
        _identifier(value["parent_id"], "research node parent_id")
    if type(value["depth"]) is not int or value["depth"] < 0:
        raise ValidationError("research node depth must be a nonnegative integer")
    _identifier(value["stage"], "research node stage")
    _text(value["hypothesis"], "research node hypothesis")
    _text(value["plan"], "research node plan")
    if type(value["seed"]) is not int or value["seed"] < 0:
        raise ValidationError("research node seed must be a nonnegative integer")
    if value["status"] not in NODE_STATUSES:
        raise ValidationError("research node status is unsupported")
    _refs(value["input_refs"], "research node input_refs")
    _refs(value["evidence_refs"], "research node evidence_refs")
    _metrics(value["metrics"])
    if value["failure_reason"] is not None:
        _text(value["failure_reason"], "research node failure_reason")
    if node_ids is not None and value["id"] in node_ids:
        raise ValidationError("research node IDs must be unique")
    return value


def validate_research_tree(value):
    """Validate a complete tree and return it unchanged."""
    fields = {"schema_version", "id", "revision", "objective", "selection_policy",
              "max_depth", "max_children", "nodes", "retained_alternatives", "promoted_id"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"research tree requires exactly {sorted(fields)}")
    if value["schema_version"] != TREE_SCHEMA_VERSION:
        raise ValidationError("unsupported research tree schema")
    _identifier(value["id"], "research tree id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("research tree revision must be positive")
    _text(value["objective"], "research tree objective")
    if value["selection_policy"] not in SELECTION_POLICIES:
        raise ValidationError("research tree selection_policy is unsupported")
    for key in ("max_depth", "max_children"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValidationError(f"research tree {key} must be positive")
    nodes = value["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("research tree nodes must be nonempty")
    ids = set()
    for node in nodes:
        _node(node, node_ids=ids)
        ids.add(node["id"])
    roots = [node for node in nodes if node["parent_id"] is None]
    if len(roots) != 1 or roots[0]["depth"] != 0:
        raise ValidationError("research tree requires one depth-zero root")
    by_id = {node["id"]: node for node in nodes}
    children = {node_id: [] for node_id in ids}
    for node in nodes:
        parent = node["parent_id"]
        if parent is not None:
            if parent not in by_id:
                raise ValidationError("research node references an unknown parent")
            if node["depth"] != by_id[parent]["depth"] + 1:
                raise ValidationError("research node depth does not follow its parent")
            children[parent].append(node["id"])
    if any(len(value_) > value["max_children"] for value_ in children.values()):
        raise ValidationError("research tree exceeds max_children")
    if any(node["depth"] > value["max_depth"] for node in nodes):
        raise ValidationError("research tree exceeds max_depth")
    # Following parent pointers from every node must terminate at the root.
    for node in nodes:
        seen = set()
        current = node
        while current["parent_id"] is not None:
            if current["id"] in seen:
                raise ValidationError("research tree contains a parent cycle")
            seen.add(current["id"])
            current = by_id[current["parent_id"]]
    _refs(value["retained_alternatives"], "research tree retained_alternatives")
    if set(value["retained_alternatives"]) - ids:
        raise ValidationError("retained_alternatives contains an unknown node")
    promoted = value["promoted_id"]
    if promoted is not None:
        _identifier(promoted, "research tree promoted_id")
        if promoted not in by_id or by_id[promoted]["status"] != "promoted":
            raise ValidationError("promoted_id must identify a promoted node")
    canonical_bytes(value)
    return value


def new_tree(tree_id, objective, *, root_hypothesis, root_plan, seed=0,
             max_depth=3, max_children=4, selection_policy="evidence_gated"):
    """Create a deterministic root tree; later operations return new copies."""
    tree = {
        "schema_version": TREE_SCHEMA_VERSION,
        "id": tree_id,
        "revision": 1,
        "objective": objective,
        "selection_policy": selection_policy,
        "max_depth": max_depth,
        "max_children": max_children,
        "nodes": [{"id": "root", "parent_id": None, "depth": 0, "stage": "ideation",
                   "hypothesis": root_hypothesis, "plan": root_plan, "seed": seed,
                   "status": "proposed", "input_refs": [], "evidence_refs": [],
                   "metrics": {}, "failure_reason": None}],
        "retained_alternatives": [],
        "promoted_id": None,
    }
    return validate_research_tree(tree)


def _copy(tree):
    validate_research_tree(tree)
    return deepcopy(tree)


def fork(tree, parent_id, node_id, *, stage, hypothesis, plan, seed, input_refs=None):
    """Add one isolated branch while preserving every prior node byte-for-byte."""
    result = _copy(tree)
    by_id = {node["id"]: node for node in result["nodes"]}
    if parent_id not in by_id:
        raise ValidationError("cannot fork from an unknown research node")
    parent = by_id[parent_id]
    if parent["depth"] >= result["max_depth"]:
        raise ValidationError("research tree maximum depth reached")
    if sum(node["parent_id"] == parent_id for node in result["nodes"]) >= result["max_children"]:
        raise ValidationError("research tree maximum branch count reached")
    _identifier(node_id, "research node id")
    if node_id in by_id:
        raise ValidationError("research node ID already exists")
    node = {"id": node_id, "parent_id": parent_id, "depth": parent["depth"] + 1,
            "stage": stage, "hypothesis": hypothesis, "plan": plan, "seed": seed,
            "status": "proposed", "input_refs": list(input_refs or []), "evidence_refs": [],
            "metrics": {}, "failure_reason": None}
    _node(node)
    result["nodes"].append(node)
    result["revision"] += 1
    return validate_research_tree(result)


def record_result(tree, node_id, *, status, metrics=None, evidence_refs=None, failure_reason=None):
    """Record a bounded result without mutating a prior tree revision."""
    if status not in {"running", "verified", "pruned", "blocked"}:
        raise ValidationError("record_result status must be running, verified, pruned, or blocked")
    result = _copy(tree)
    target = next((node for node in result["nodes"] if node["id"] == node_id), None)
    if target is None:
        raise ValidationError("cannot record a result for an unknown research node")
    if target["status"] in {"promoted", "pruned"}:
        raise ValidationError("a promoted or pruned research node is immutable")
    target.update(status=status, metrics=dict(metrics or {}), evidence_refs=list(evidence_refs or []),
                  failure_reason=failure_reason)
    result["revision"] += 1
    return validate_research_tree(result)


def promote(tree, node_id, *, evidence_refs):
    """Promote one independently verified branch and retain alternatives."""
    result = _copy(tree)
    target = next((node for node in result["nodes"] if node["id"] == node_id), None)
    if target is None:
        raise ValidationError("cannot promote an unknown research node")
    if target["status"] != "verified":
        raise ValidationError("only a verified research node can be promoted")
    _refs(evidence_refs, "promotion evidence_refs", allow_empty=False)
    if set(evidence_refs) - set(target["evidence_refs"]):
        raise ValidationError("promotion evidence must be recorded on the selected node")
    target["status"] = "promoted"
    result["promoted_id"] = node_id
    selected_path = set(branch_path(result, node_id))
    result["retained_alternatives"] = sorted(
        node["id"] for node in result["nodes"]
        if node["id"] not in selected_path and node["status"] in {"proposed", "verified"}
    )
    result["revision"] += 1
    return validate_research_tree(result)


def branch_path(tree, node_id):
    validate_research_tree(tree)
    by_id = {node["id"]: node for node in tree["nodes"]}
    if node_id not in by_id:
        raise ValidationError("unknown research node")
    path, current = [], by_id[node_id]
    while current is not None:
        path.append(current["id"])
        current = by_id.get(current["parent_id"])
    return list(reversed(path))


def render_tree_html(tree):
    """Render a deterministic, dependency-free audit view of the tree."""
    validate_research_tree(tree)
    rows = []
    for node in tree["nodes"]:
        rows.append("<tr>" + "".join([
            f"<td>{html.escape(node['id'])}</td>",
            f"<td>{html.escape(node['parent_id'] or '—')}</td>",
            f"<td>{node['depth']}</td>",
            f"<td>{html.escape(node['stage'])}</td>",
            f"<td>{html.escape(node['status'])}</td>",
            f"<td>{html.escape(node['hypothesis'])}</td>",
            f"<td>{html.escape(', '.join(node['evidence_refs']) or '—')}</td>",
        ]) + "</tr>")
    return (
        "<!doctype html><meta charset='utf-8'><title>Research tree</title>"
        "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #bbb;padding:.45rem;text-align:left;vertical-align:top}"
        "th{background:#f1f3f5}</style><h1>" + html.escape(tree["objective"]) + "</h1>"
        "<table><thead><tr><th>Node</th><th>Parent</th><th>Depth</th><th>Stage</th>"
        "<th>Status</th><th>Hypothesis</th><th>Evidence</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )
