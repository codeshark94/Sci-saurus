"""Design obligations shared by concept intake and implementation evidence."""
from copy import deepcopy
import math

from scisaurus.core.errors import ValidationError

DESIGN_BRIEF_REVISION = "material-design-brief-1"
CONCEPT_CANDIDATE_COUNT = 3


def design_brief_contract():
    return {
        "schema_version": DESIGN_BRIEF_REVISION,
        "use_case": "specific useful device or material function",
        "differentiation_hypothesis": (
            "proposed functional or physical departure from the baseline, why ordinary parameter tuning "
            "may not explain it, and what would disconfirm it; novelty is unverified"),
        "design_family_id": "exact declared laboratory design family id",
        "physics_family_ids": "nonempty list of exact declared physics family ids",
        "parameters": [{"name": "editable geometry or material variable", "unit": "physical unit or dimensionless",
                        "lower": "finite proposed lower bound", "upper": "finite proposed upper bound"}],
        "target_response": {"quantity": "measurable response", "unit": "physical unit or dimensionless",
                            "direction": "minimize|maximize|match", "rationale": "why the response is useful"},
        "baseline": "concrete reference design compared under equal constraints",
        "constraints": "nonempty list of manufacturing, size, material or resource constraints",
        "first_pilot": {"calculation": "smallest informative established-solver calculation",
                        "observable": "raw field and derived performance measure",
                        "verification": "analytic limit or benchmark to check before interpreting performance",
                        "editable_design": "geometry representation retained for subsequent revisions"},
    }


def validate_design_brief(value, laboratory=None):
    contract = design_brief_contract()
    if not isinstance(value, dict) or set(value) != set(contract):
        raise ValidationError("design_brief requires exactly " + ", ".join(sorted(contract)))
    if value["schema_version"] != DESIGN_BRIEF_REVISION:
        raise ValidationError("unsupported design_brief schema_version")

    def text(item, label):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(label + " must be nonempty text")

    for key in ("use_case", "differentiation_hypothesis", "design_family_id", "baseline"):
        text(value[key], "design_brief." + key)
    for key in ("physics_family_ids", "constraints"):
        items = value[key]
        if not isinstance(items, list) or not items or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items):
            raise ValidationError("design_brief." + key + " must contain unique nonempty strings")
        for item in items:
            text(item, "design_brief." + key)
    for key in ("target_response", "first_pilot"):
        if not isinstance(value[key], dict) or set(value[key]) != set(contract[key]):
            raise ValidationError("design_brief." + key + " has invalid fields")
        for field, item in value[key].items():
            text(item, "design_brief." + key + "." + field)
    if value["target_response"]["direction"] not in {"minimize", "maximize", "match"}:
        raise ValidationError("design_brief target direction must be minimize, maximize or match")
    parameters = value["parameters"]
    if not isinstance(parameters, list) or not parameters:
        raise ValidationError("design_brief requires editable parameters")
    names = set()
    for item in parameters:
        if not isinstance(item, dict) or set(item) != {"name", "unit", "lower", "upper"}:
            raise ValidationError("design_brief parameter has invalid fields")
        for field in ("name", "unit"):
            text(item[field], "design_brief parameter " + field)
        if item["name"] in names:
            raise ValidationError("design_brief parameter names must be unique")
        names.add(item["name"])
        try:
            finite = all(type(item[key]) in (int, float) and math.isfinite(item[key]) for key in ("lower", "upper"))
        except OverflowError:
            finite = False
        if not finite:
            raise ValidationError("design_brief parameter bounds must be finite nonboolean numbers")
        if not item["lower"] < item["upper"]:
            raise ValidationError("design_brief parameter lower must be less than upper")
    if isinstance(laboratory, dict):
        designs = {item["id"] for item in laboratory.get("design_families", [])}
        physics = {item["id"] for item in laboratory.get("physics_families", [])}
        if value["design_family_id"] not in designs or not set(value["physics_family_ids"]) <= physics:
            raise ValidationError("design_brief uses an undeclared laboratory family")
    return value


def validate_concept_candidates(candidates, selected_id, laboratory=None):
    """Check a concept comparison's identity and declared implementation boundary.

    Textual distinction is only a structural check. Scientific reviewers must
    determine whether proposed mechanisms differ substantively.
    """
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ValidationError("concept intake requires one to eight candidate designs")
    ids = set()
    mechanisms = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValidationError("concept candidate must be an object")
        identifier = candidate.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
            raise ValidationError("concept candidate IDs must be nonempty and unique")
        ids.add(identifier)
        validate_design_brief(candidate.get("design_brief"), laboratory)
        mechanism = candidate.get("mechanism")
        if not isinstance(mechanism, str) or not mechanism.strip():
            if len(candidates) > 1:
                raise ValidationError("concept comparison requires an explicit mechanism for every design")
            continue
        mechanisms.add(" ".join(mechanism.casefold().split()))
    if selected_id not in ids:
        raise ValidationError("selected concept must belong to the candidate designs")
    if len(candidates) > 1 and len(mechanisms) < 2:
        raise ValidationError("concept comparison must explore different physical mechanisms, not only parameters")
    return candidates


def implementation_evidence_scope(brief):
    validate_design_brief(brief)
    return {
        "purpose": "implement_and_challenge_material_concept",
        "design_brief": deepcopy(brief),
        "evidence_obligations": [
            "physical model and its validity range",
            "material parameters with units and provenance",
            "geometry, boundary conditions and established-solver implementation",
            "equal-constraint baseline and published or analytic verification case",
            "closest competing designs and evidence that challenges the proposed mechanism",
        ],
        "completion_boundary": (
            "Acquire evidence needed for the first informative pilot and record unresolved inputs. "
            "Stop expanding when further acquisition does not improve these obligations. "
            "Do not demand final optimized performance, convergence studies or proven novelty before implementation. "
            "Missing critical physical inputs remain blockers; source integrity and evidence review remain required. "
            "Literature completion does not establish novelty or validate the proposed performance."),
    }


def concept_intake_contract():
    return {
        "sequence": ["compare_physical_concepts", "closest_design_screen", "select_one_design", "implementation_literature", "baseline_and_small_pilot",
                     "diagnose_and_revise", "final_validation"],
        "current_scope": (
            "Compare useful physical concepts with substantive functional or mechanism differences, "
            "then select one design for implementation evidence within the currently attested laboratory. "
            "A scoped repair preserves its admitted question instead of reopening concept discovery."),
        "exploration_rules": [
            "Unconventional use cases and surprising target functions are welcome; explain a plausible physical mechanism and practical utility.",
            "Explore the declared thermal, fluid, acoustic, electromagnetic and other supported physics; do not assume the objective is a mechanical lattice or structural stress reduction.",
            "Choose the useful function and physical principle before its solver or geometry. A CAD representation is not a scientific objective.",
            "Every proposed concept must have a small discriminating test executable with currently attested tools, inputs and local resources. An installed solver does not verify every module or coupling.",
            "Do not assume additional installations, unsupported constitutive laws or unverified coupled physics. Explain field exchanges and verification for any proposed coupling.",
            "Compare different mechanisms or functions, not renamed versions of the same geometry, material, gradient or parameter sweep.",
            "Explain each differentiation hypothesis against its conventional baseline and the strongest simple alternative explanation. Mere parameter tuning is not evidence of a new principle.",
            "Select for useful functional differentiation, explanatory depth and a decisive first test; ease of implementation alone does not justify selection.",
            "Prioritize a surprising useful function or a specific unresolved physical tradeoff. A familiar device with renamed geometry, a different application label or a weak homogeneous baseline is insufficient differentiation.",
            "Treat proposed differentiation as unverified. Familiarity does not make an idea invalid, and unusual wording or absence of a matching citation does not establish novelty.",
            "Generate and compare the configured candidates in the same bounded response. Screen the proposed selection against a bounded closest-design search before implementation literature; do not restart a broad frontier survey.",
        ],
        "review_rule": (
            "Challenge cosmetic diversity, selection based only on convenience, unsupported physics and "
            "differentiation explained completely by an ordinary baseline change. Bind any objection to "
            "the actual candidate and propose a discriminating test. Do not require proven novelty or final "
            "performance at this provisional stage, and do not reinterpret a scoped repair as free topic selection."),
        "evidence_boundary": (
            "Physical mechanism and differentiation are testable hypotheses. Closest designs are checked after "
            "proposal and before final selection; full literature, constants and novelty remain later obligations. Unsupported claims of "
            "verified novelty, measured performance or established parameter provenance are not allowed."),
        "later_scope": (
            "Principal objective requirements for literature support, parameter sweeps, convergence, "
            "robustness and publication apply at their subsequent stages; they are not completed-result "
            "requirements for choosing the concept or specifying a small first pilot."),
    }
