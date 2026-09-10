"""Minimal CLI for the P1 core slice: init / publish / show / status / verify."""

from __future__ import annotations

import argparse
import json
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

    args = parser.parse_args(argv)
    if args.cmd == "run-paragraph":
        from scisaurus.runtime.config import load_config
        from scisaurus.runtime.runner import ParagraphRunner
        from scisaurus.core.errors import ContractError
        try:
            result = ParagraphRunner(args.project_dir, load_config(args.config),
                on_progress=lambda state: print(json.dumps(state), flush=True)).run()
        except ContractError as exc:
            print(f"configuration rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "incumbent_ref": result["incumbent_ref"],
                          "report": str(__import__("pathlib").Path(args.project_dir).resolve() / "output" / "report.md")}, indent=2))
        return 0 if result["status"] == "accepted" else 3


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
