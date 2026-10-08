"""Lossless transport encodings for complete scientific review evidence."""
import hashlib

from scisaurus.core.schema import canonical_bytes


def review_observation_table(observations):
    """Encode every observation without repeating column names or losing missing keys."""
    schemas, schema_ids, rows = [], [], []
    for observation in observations:
        fields = sorted(observation)
        if fields not in schemas:
            schemas.append(fields)
        schema_ids.append(schemas.index(fields))
        rows.append([observation[field] for field in fields])
    table = {"encoding": "observation-table-1", "complete": True,
             "row_count": len(rows), "schemas": schemas, "rows": rows,
             "observations_sha256": hashlib.sha256(canonical_bytes(observations)).hexdigest(),
             "decoding": "For row i, zip schemas[schema_ids[i]] with rows[i] to reconstruct the exact observation object. "
                         "When schema_ids is absent use schema 0 for every row. Row order and all values are preserved; "
                         "fields absent from a schema are missing, not null."}
    if len(schemas) > 1:
        table["schema_ids"] = schema_ids
    return table

