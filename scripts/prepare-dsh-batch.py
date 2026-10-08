#!/usr/bin/env python3
"""Prepare a pinned, single-model unattended DSH composition without dispatch."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

repository = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository))
from scisaurus.runtime.dsh_batch import validate_batch_config

parser = argparse.ArgumentParser()
parser.add_argument("--dsh-root", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--base-url", required=True)
parser.add_argument("--auth-env", required=True)
parser.add_argument("--reasoning", choices=["off", "low", "medium", "high", "max"], default="off")
parser.add_argument("--max-output-tokens", type=int, required=True)
parser.add_argument("--context-window", type=int, required=True)
parser.add_argument("--timeout-seconds", type=float, required=True)
args = parser.parse_args()
root = Path(args.dsh_root).resolve()
output = Path(args.output).resolve()
output.parent.mkdir(parents=True, exist_ok=True)
node = Path(shutil.which("node") or "").resolve()
entry = root / "packages/examples/jsonrpc-demo/lib/packaged-bin.js"
if not entry.is_file() or not node.is_file():
    parser.error("build the DSH JSON-RPC runtime and install Node before preparing its deployment")
model = {"id": args.model, "contextWindow": args.context_window,
         "maxTokens": args.max_output_tokens,
         "reasoningEfforts": {"off": None, ("high" if args.reasoning == "off" else args.reasoning):
                              "high" if args.reasoning == "off" else args.reasoning}}
composition = [
    {"id": "sdk", "name": "@deepseek-ai/dsh-sdk-jsonrpc-server", "config": {"maxTokensAsSuccess": False}},
    {"id": "llm", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {"sciwhale-fixed": {
        "apiKeyEnv": args.auth_env, "api": "openai-completions", "baseURL": args.base_url,
        "reasoning": args.reasoning, "models": [model],
        "compat": {"thinkingFormat": "deepseek", "supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
        "retryPolicy": {"mode": "normal", "maxRetries": 0},
    }}}},
    {"id": "subprocess", "name": "@deepseek-ai/dsh-subprocess-local"},
    {"id": "bash", "name": "@deepseek-ai/dsh-bash-local"},
    {"id": "agent", "name": "@deepseek-ai/dsh-agent-spine-demo", "config": {
        "persona": "You are an engineering worker. Read the task files, edit files, execute, inspect errors and repair. Preserve scientific inputs and report limitations.",
        "workspaceContext": False, "skills": {"enabled": False},
        "toolBash": {"enableRunInBackground": False}, "toolJobs": False}},
    {"id": "sessions", "name": "@deepseek-ai/dsh-session-persistence-jsonl", "config": {"root": ".sessions"}},
    {"id": "checkpoints", "name": "@deepseek-ai/dsh-session-checkpoint-policy"},
    {"id": "fs", "name": "@deepseek-ai/dsh-fs-local"},
    {"id": "fs-policy", "name": "@deepseek-ai/dsh-fs-observation-policy"},
    {"id": "tools", "name": "@deepseek-ai/dsh-tool-fs"},
    {"id": "todo", "name": "@deepseek-ai/dsh-tool-todo", "config": {"allowParallelInProgress": False}},
]
composition_path = output.with_suffix(".cordis.json")
# Source-checkout deployments resolve plugins from their built package entries;
# packaged runtime distributions can use bare names from their closed closure.
packages = {}
for path in (root / "packages").glob("*/**/package.json"):
    if "node_modules" in path.parts:
        continue
    package = json.loads(path.read_text())
    if isinstance(package.get("main"), str):
        packages[package["name"]] = path.parent / package["main"]
for entry_config in composition:
    path = packages.get(entry_config["name"])
    if path is None or not path.is_file():
        parser.error(f"DSH plugin is not built: {entry_config['name']}")
    entry_config["name"] = str(path)
composition_path.write_text(json.dumps(composition, indent=2) + "\n")
files = {node, entry, composition_path, root / "package.json", root / "pnpm-lock.yaml"}
files.update((root / "packages").glob("*/**/lib/**/*.js"))
files.update((root / "packages").glob("*/**/package.json"))
files.update((root / "vendor").rglob("*.js"))
files.update((root / "vendor").rglob("package.json"))
for suffix in ("*.js", "*.mjs", "*.cjs", "*.json", "*.node", "*.wasm"):
    files.update((root / "node_modules/.pnpm").rglob(suffix))
pins = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)
        if path.is_file()}
config = {"schema_version": "dsh-batch-1", "command": [str(node), str(entry)],
          "pinned_files": pins, "read_roots": [str(root), str(composition_path.parent)],
          "composition": str(composition_path), "provider": "sciwhale-fixed", "model": args.model,
          "auth_env": args.auth_env, "timeout_seconds": args.timeout_seconds,
          "max_output_tokens": args.max_output_tokens}
validate_batch_config(config)
output.write_text(json.dumps(config, indent=2) + "\n")
print(json.dumps({"config": str(output), "model": args.model, "reasoning": args.reasoning,
                  "pinned_files": len(pins), "model_calls": 0}))
