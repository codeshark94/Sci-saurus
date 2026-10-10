#!/usr/bin/env python3
"""Extract complete numeric HDF5 datasets or mesh fields from a staged artifact.

Input: {"path": "inputs/field.h5", "format": "hdf5" | "netcdf" | "mesh"}.
Shape, dtype and complex components are preserved; nonfinite fields fail.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

_STDOUT = os.dup(1)
os.dup2(2, 1)

import numpy as np


def array(value):
    value = np.asarray(value)
    if value.dtype.kind not in "biufc":
        raise ValueError(f"unsupported numeric dtype: {value.dtype}")
    if not np.isfinite(value).all():
        raise ValueError("nonfinite values in retained field")
    result = {"shape": list(value.shape), "dtype": str(value.dtype)}
    if value.dtype.kind == "c":
        result.update(real=value.real.tolist(), imag=value.imag.tolist())
    else:
        result["values"] = value.tolist()
    return result


def main():
    request = json.load(sys.stdin)
    path = Path(request["path"])
    if request["format"] == "hdf5":
        import h5py
        fields, omitted = {}, []
        with h5py.File(path, "r") as source:
            def visit(name, item):
                if isinstance(item, h5py.Dataset):
                    if item.dtype.kind in "biufc":
                        fields[name] = array(item[()])
                    else:
                        omitted.append(name)
            source.visititems(visit)
        report = {"datasets": fields, "non_numeric_datasets": omitted}
    elif request["format"] == "netcdf":
        import netCDF4
        fields, omitted = {}, []
        with netCDF4.Dataset(path, "r") as source:
            dimensions = {key: len(value) for key, value in source.dimensions.items()}
            def visit(group, prefix=""):
                for name, item in group.variables.items():
                    key = prefix + name
                    if np.dtype(item.dtype).kind in "biufc":
                        value = item[:]
                        if np.ma.isMaskedArray(value) and np.any(value.mask):
                            raise ValueError("masked numeric data requires explicit missing-value handling: " + key)
                        fields[key] = {**array(value), "dimensions": list(item.dimensions)}
                    else:
                        omitted.append(key)
                for name, child in group.groups.items():
                    visit(child, prefix + name + "/")
            visit(source)
        report = {"datasets": fields, "dimensions": dimensions, "non_numeric_datasets": omitted}
    elif request["format"] == "mesh":
        import meshio
        mesh = meshio.read(path)
        report = {"points": array(mesh.points),
                  "cells": [{"type": cell.type, "data": array(cell.data)}
                            for cell in mesh.cells],
                  "point_data": {key: array(value) for key, value in mesh.point_data.items()},
                  "cell_data": {key: [array(value) for value in values]
                                for key, values in mesh.cell_data.items()},
                  "field_data": {key: array(value) for key, value in mesh.field_data.items()}}
    else:
        raise ValueError("format must be hdf5, netcdf or mesh")
    report.update(schema_version="numeric-field-extraction-1",
                  source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  complete_numeric_fields=True, sampling=False,
                  scientific_admission="not_assessed")
    payload = json.dumps(report, allow_nan=False, sort_keys=True).encode() + b"\n"
    os.write(_STDOUT, payload)
    os.close(_STDOUT)


if __name__ == "__main__":
    main()
