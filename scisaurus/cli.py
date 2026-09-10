"""Minimal CLI for the P1 core slice: init / publish / show / status / verify."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.tasks import TaskManager


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scisaurus")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="initialize a project control store")
    p_init.add_argument("project_dir")

    p_pub = sub.add_parser("publish", help="publish an artifact from a file")
    p_pub.add_argument("project_dir")
    p_pub.add_argument("logical_id")
    p_pub.add_argument("file")
    p_pub.add_argument("--type", dest="artifact_type", default="draft")
    p_pub.add_argument("--author", default="principal")
    p_pub.add_argument("--media-type", default="text/markdown")

    p_show = sub.add_parser("show", help="print an artifact manifest")
    p_show.add_argument("project_dir")
    p_show.add_argument("ref")

    p_status = sub.add_parser("status", help="project status summary")
    p_status.add_argument("project_dir")

    p_verify = sub.add_parser("verify", help="verify the event hash chain")
    p_verify.add_argument("project_dir")

    p_run = sub.add_parser("run-paragraph", help="run a public-data paragraph revision with live providers")
    p_run.add_argument("project_dir")
    p_run.add_argument("--config", required=True)

    p_project = sub.add_parser("run-project", help="run scoped artifact work from a project Score")
    p_project.add_argument("project_dir")
    p_project.add_argument("--config", required=True)
    p_project.add_argument("--deadline-seconds", type=float, help="Hard elapsed deadline within the configured limit")
    p_project.add_argument("--target-seconds", type=float, help="Target completion time; stop admitting discretionary work")
    p_project.add_argument("--first-result-seconds", type=float, help="Target time for the first verified result")

    p_survey = sub.add_parser("run-survey", help="map literature and independently test a research-gap hypothesis")
    p_survey.add_argument("project_dir")
    p_survey.add_argument("--config", required=True)
    p_survey.add_argument("--deadline-seconds", type=float)
    p_survey.add_argument("--target-seconds", type=float)
    p_survey.add_argument("--first-result-seconds", type=float)

    args = parser.parse_args(argv)
    if args.cmd in {"run-paragraph", "run-project", "run-survey"}:
        from scisaurus.core.errors import ContractError, ValidationError
        if args.cmd == "run-survey":
            from scisaurus.runtime.survey_config import load_survey_config as load_config
            from scisaurus.runtime.survey import SurveyRunner as Runner
        elif args.cmd == "run-project":
            from scisaurus.runtime.project_config import load_project_config as load_config
            from scisaurus.runtime.project import ProjectRunner as Runner
        else:
            from scisaurus.runtime.config import load_config
            from scisaurus.runtime.runner import ParagraphRunner as Runner
        try:
            config = load_config(args.config)
            if args.cmd in {"run-project", "run-survey"}:
                overrides = {name: getattr(args, flag) for name, flag in (
                    ("hard_seconds", "deadline_seconds"), ("target_seconds", "target_seconds"),
                    ("first_result_seconds", "first_result_seconds")) if getattr(args, flag) is not None}
                if overrides:
                    if "score" not in config and "survey" not in config:
                        raise ValidationError(
                            "Time planning options require a versioned Score configuration")
                    config["time_policy"] = {**(config.get("time_policy") or {}), **overrides}
            result = Runner(args.project_dir, config,
                on_progress=lambda state: print(json.dumps(state), flush=True)).run()
        except ContractError as exc:
            print(f"configuration rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "incumbent_ref": result["incumbent_ref"],
                          "report": str(Path(args.project_dir).resolve() / "output" / (
                              "survey.md" if args.cmd == "run-survey" else "report.md"))}, indent=2))
        return 0 if result["status"] in {"accepted", "completed"} else 3


    if args.cmd == "init":
        control = ControlStore(args.project_dir)
        ArtifactStore(control).init_project(principal_note="cli init")
        print(f"initialized project at {args.project_dir}")
        return 0

    control = ControlStore(args.project_dir)
    store = ArtifactStore(control)

    if args.cmd == "publish":
        with open(args.file, "rb") as fh:
            body = fh.read()
        manifest = store.publish_artifact(
            logical_id=args.logical_id,
            artifact_type=args.artifact_type,
            author=args.author,
            body=body,
            media_type=args.media_type,
        )
        print(manifest["artifact_ref"])
        return 0

    if args.cmd == "show":
        print(json.dumps(store.get(args.ref), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "status":
        bus = MessageBus(control)
        tasks = TaskManager(control)
        print(f"project_dir: {control.dir}")
        print(f"events: {control._conn.execute('SELECT COUNT(*) c FROM events').fetchone()['c']}")
        print(f"trusted_head: {control.trusted_head()[:16]}…")
        print(f"pending_messages: {bus.pending()}")
        for item in bus.quarantined():
            print(f"quarantined_message {item['message_id']}: {item['reason']}")
        for row in control._conn.execute("SELECT task_id, state FROM tasks"):
            print(f"task {row['task_id']}: {row['state']}")
        return 0

    if args.cmd == "verify":
        ok, reason = control.verify_chain()
        print("ok" if ok else f"FAIL: {reason}")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
