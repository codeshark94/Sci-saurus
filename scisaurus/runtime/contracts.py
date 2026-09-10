"""Shared structured review and preservation contracts."""

import re

from scisaurus.core.errors import ValidationError
from scisaurus.review.issues import CHECK_KINDS, CHECK_OUTCOMES


_NUMBER = re.compile(r"(?<![\w.])[+\-\u2212]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+\-\u2212]?\d+)?%?(?!\w|\.\d)")

_REQUIRED_REGRESSION_CHECKS = {
    "source-support": (
        "Compare every assertion with supplied facts, claimed support, and independently captured sources. "
        "If support is empty, every assertion must use supplied facts or an interpretation permitted by the "
        "acceptance contract. Missing support for any external assertion must fail this check, including "
        "assertions absent from the producer's support list. Merely fetching a related source is not support."
    ),
    "reader-facing": (
        "The assigned output must express its substantive content directly without internal control IDs, task history, "
        "or references to the user, Principal, assignment, or acceptance checks. Distinguish leaked control "
        "context from legitimate domain identifiers and content. Control context belongs in evidence records and must "
        "fail this check if present in the delivered output. Machine-readable keys required by its format are allowed."
    ),
}

def required_strings(value, keys):
    if any((not isinstance(value.get(key), str) or not value[key].strip() for key in keys)):
        raise ValidationError('model output omitted required substantive fields')

def validate_verdict(verdict, *, required_check_kinds=None):
    fields = {'checks', 'regressions', 'uncertainties', 'observations', 'rationale'}
    missing, extra = (fields - verdict.keys(), verdict.keys() - fields)
    if missing or extra:
        raise ValidationError(f'verifier output fields invalid: missing={sorted(missing)}, extra={sorted(extra)}')
    required_strings(verdict, ('rationale',))
    for field in ('regressions', 'uncertainties'):
        values = verdict[field]
        if not isinstance(values, list) or any((not isinstance(item, str) or not item.strip() for item in values)):
            raise ValidationError(f'verifier must explicitly report {field} as a list of substantive strings')
    if not isinstance(verdict['observations'], list):
        raise ValidationError('verifier observations must be a list of structured objects')
    for observation in verdict['observations']:
        if not isinstance(observation, dict) or set(observation) != {'observation', 'reason_nonblocking'}:
            raise ValidationError('each observation requires observation and reason_nonblocking fields')
        required_strings(observation, ('observation', 'reason_nonblocking'))
    checks = verdict['checks']
    if not isinstance(checks, list) or not checks:
        raise ValidationError('verifier omitted executed checks')
    check_fields = {'check_id', 'kind', 'outcome', 'method', 'result'}
    ids, kinds = (set(), set())
    for check in checks:
        if not isinstance(check, dict) or set(check) != check_fields:
            raise ValidationError('verifier checks require exactly check_id, kind, outcome, method, and result')
        required_strings(check, check_fields)
        if check['kind'] not in CHECK_KINDS or check['outcome'] not in CHECK_OUTCOMES:
            raise ValidationError('verifier check kind or outcome is unsupported')
        if check['check_id'] in ids or check['check_id'] == 'mechanical-preservation':
            raise ValidationError('verifier check_id is duplicated or reserved for a deterministic check')
        ids.add(check['check_id'])
        kinds.add(check['kind'])
    if kinds != CHECK_KINDS:
        raise ValidationError('verifier must execute both resolution and regression checks')
    required = ({key: "regression" for key in _REQUIRED_REGRESSION_CHECKS}
                if required_check_kinds is None else required_check_kinds)
    actual = {check['check_id']: check['kind'] for check in checks}
    missing = {key for key, kind in required.items() if actual.get(key) != kind}
    if missing:
        raise ValidationError(f'verifier omitted required checks or used the wrong kind: {sorted(missing)}')

def validate_reassessment(decision):
    if decision.keys() - {'decision', 'rationale', 'change_focus'}:
        raise ValidationError('supervisor returned unsupported reassessment fields')
    required_strings(decision, ('decision', 'rationale'))
    if decision['decision'] not in {'revise', 'pause'}:
        raise ValidationError('supervisor returned an unsupported allocation decision')
    if decision['decision'] == 'revise':
        required_strings(decision, ('change_focus',))
    elif 'change_focus' in decision and (not isinstance(decision['change_focus'], str)):
        raise ValidationError('pause change_focus must be omitted or a string')

def required_checks(extra=None):
    requirements = {**_REQUIRED_REGRESSION_CHECKS, **(extra or {})}
    resolution = {"check_id": "objective-resolution", "kind": "resolution", "requirement":
        "Compare the baseline defect and exact candidate against the assigned objective and governing facts. "
        "State whether each defect in this scope is resolved, using concrete before/after evidence. "
        "A source-support or preservation check alone does not establish that the requested problem was solved."}
    return [resolution] + [{"check_id": check_id, "kind": "regression", "requirement": requirement}
                           for check_id, requirement in requirements.items()]


def preserves_literals(text, literals):
    numbers = {match.group() for match in _NUMBER.finditer(text)}
    return all(literal in numbers if _NUMBER.fullmatch(literal) else literal in text for literal in literals)
