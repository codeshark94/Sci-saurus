#!/usr/bin/env python3
"""Exercise all external solver runtimes through the agent workbench contract."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scisaurus.runtime.laboratory import LaboratoryBinding
from scisaurus.runtime.software_workbench import SoftwareWorkbench
from scisaurus.core.schema import canonical_bytes


STEPS = [("moose", "moose", "heat.e"), ("elmer", "elmer", "mesh/heat_t0001.vtu"),
         ("calculix", "flow_structure", "bar.frd"), ("openfoam", "flow_structure", None),
         ("code_aster", "code_aster", "bar.med")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--laboratory", required=True)
    parser.add_argument("--attestation", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tool", choices=[x[0] for x in STEPS], action="append")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    binding = LaboratoryBinding.load(args.laboratory, attestation_path=args.attestation)
    source = (ROOT / "scripts/laboratory/probe_external_solver.py").read_text()
    records = []
    for tool, runtime, field in STEPS:
        if args.tool and tool not in args.tool:
            continue
        replays = []
        try:
            for replay in range(2):
                workbench = SoftwareWorkbench(output / tool / str(replay), deadline=time.monotonic() + 900,
                                             laboratory=binding)
                result = workbench.execute({"operation": "run", "arguments": {
                    "runtime": runtime, "source": source, "input": {"tool": tool},
                    "purpose": "scientific_computation", "documentation_refs": [], "expected": None,
                    "inputs": [], "outputs": [{"name": "solver-case.zip"}] + ([{"name": field}] if field else [])}})
                (output / f"{tool}-replay-{replay}.json").write_bytes(canonical_bytes(result))
                if result.get("outcome") != "ok":
                    raise ValueError(result.get("error", "solver operation failed without a diagnostic"))
                replays.append(result)
            numeric = [{k: v for k, v in r["result"]["output"].items() if k != "files"} for r in replays]
            if numeric[0] != numeric[1]:
                raise ValueError("fresh identical-input numerical replay differs")
            row = {"tool": tool, "runtime": runtime, "status": "passed", "replays": 2,
                   "max_absolute_error": numeric[0]["max_absolute_error"],
                   "scientific_admission": "not_assessed"}
        except Exception as error:
            row = {"tool": tool, "runtime": runtime, "status": "failed", "error": str(error)}
        records.append(row)
        (output / "summary.json").write_bytes(canonical_bytes({"config_sha256": binding.identity,
            "checks": records, "model_calls": 0, "scientific_admission": "not_assessed"}))
        print(json.dumps(row), flush=True)
    if any(row["status"] != "passed" for row in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
