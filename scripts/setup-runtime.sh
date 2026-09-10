#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin=${SCISAURUS_PYTHON:-python3}
command -v node >/dev/null 2>&1 || { echo "Node.js is required for the pinned source extractor." >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm is required for the pinned source extractor." >&2; exit 1; }

"$python_bin" -m venv "$project_root/.venv"
venv_python="$project_root/.venv/bin/python"
"$venv_python" -m pip install -r "$project_root/requirements-runtime.txt"
extractor_dir=$("$venv_python" -c 'from importlib.metadata import distribution; print(distribution("readabilipy").locate_file("readabilipy/javascript"))')

# ReadabiliPy otherwise installs JavaScript dependencies during the first MCP
# request, allowing npm output to corrupt the protocol's stdout stream.
cp "$project_root/scripts/readabilipy-package-lock.json" "$extractor_dir/package-lock.json"
npm ci --prefix "$extractor_dir" --ignore-scripts --no-audit --no-fund
"$venv_python" - <<'PY'
from importlib.metadata import version
from readabilipy.simple_json import simple_json_from_html_string
page = '<html><body><article><p>Source extraction preserves the supplied observation.</p></article></body></html>'
result = simple_json_from_html_string(page, use_readability=True)
if 'preserves the supplied observation' not in (result.get('content') or ''):
    raise SystemExit('Pinned source extractor failed its content probe')
for package in ('mcp-server-fetch', 'mcp', 'readabilipy'):
    print(f'{package}=={version(package)}')
PY
