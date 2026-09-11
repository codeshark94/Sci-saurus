#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime_dir="$repo_dir/.runs/experiment-runtime"

python3 -m venv "$runtime_dir"
"$runtime_dir/bin/python" -m pip install --disable-pip-version-check -r "$repo_dir/requirements-experiment.txt"
"$runtime_dir/bin/python" - <<'PY'
import matplotlib
import numpy
print(f"numpy=={numpy.__version__}")
print(f"matplotlib=={matplotlib.__version__}")
PY
