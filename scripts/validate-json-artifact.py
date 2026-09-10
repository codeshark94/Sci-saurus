#!/usr/bin/env python3
"""Validate a JSON artifact against a supplied Draft 2020-12 schema offline.

Input is one JSON object containing ``text`` and ``schema``. Invalid artifact
content is a completed check (exit 0, valid false); invalid requests or schemas
are checker failures (exit 2). References can resolve embedded resources only.
"""
from __future__ import annotations

import json
import math
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def _finite_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON number exceeds finite numeric representation")
    if isinstance(value, dict):
        for item in value.values():
            _finite_values(item)
    elif isinstance(value, list):
        for item in value:
            _finite_values(item)


def read_json(text):
    value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    _finite_values(value)
    return value


def _no_retrieval(uri):
    raise NoSuchResource(ref=uri)


def validate(request):
    if not isinstance(request, dict) or set(request) != {"text", "schema"}:
        raise ValueError("Request requires exactly text and schema")
    if not isinstance(request["text"], str) or not isinstance(request["schema"], dict):
        raise ValueError("Request text must be a string and schema must be an object")
    schema = request["schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, registry=Registry(retrieve=_no_retrieval))
    try:
        instance = read_json(request["text"])
    except ValueError as exc:
        return {"valid": False, "errors": [{"path": [], "schema_path": [], "message": str(exc)}]}
    errors = [{"path": list(error.absolute_path), "schema_path": list(error.absolute_schema_path),
               "message": error.message} for error in validator.iter_errors(instance)]
    return {"valid": not errors, "errors": errors}


def main():
    try:
        result = validate(read_json(sys.stdin.read()))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, SchemaError, Unresolvable, RecursionError) as exc:
        print(f"JSON artifact check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
