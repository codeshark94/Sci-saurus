"""Command-line entry points for Sci-saurus projects and the local console."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.tasks import TaskManager


def _default_dashboard_project_dir():
    current = Path.cwd()
    workspace = current / "local-private"
    if workspace.is_dir():
        return str(workspace)
    autolab = current / "local-private" / "autolab"
    return str(autolab if autolab.is_dir() else current)


def _composer_progress_line(state):
    """Render a bounded live line instead of dumping the entire checkpoint.

    Durable progress checkpoints intentionally retain the complete stage and
    assignment ledger for the dashboard and resume logic.  Sending that whole
    object to stdout on every heartbeat made a long Composer run emit the
    entire historical attempt list repeatedly, obscuring the current action
    and creating needless I/O.  The CLI stream is operational telemetry; the
    artifact store remains the source of full detail.
    """
    state = state if isinstance(state, dict) else {}
    stages = state.get("stages") if isinstance(state.get("stages"), dict) else {}
    active = []
    current = None
    phase = state.get("phase")
    phase_stage = phase.split(":", 1)[0] if isinstance(phase, str) else None
    if phase_stage in stages and isinstance(stages.get(phase_stage), dict):
        # The phase prefix is the controller's current boundary even when the
        # checkpoint is the short completed/failed transition between stages.
        # Do not fall back to an older retrying stage and report its workers as
        # live during that transition.
        phase_record = stages[phase_stage]
        current = phase_stage
        if phase_record.get("status") in {"running", "retrying", "paused"}:
            active.extend(phase_record.get("active_agents", []))
    for stage_id, record in stages.items():
        if not isinstance(record, dict):
            continue
        if record.get("status") in {"running", "retrying", "paused"}:
            if current is None:
                current = stage_id
                active.extend(record.get("active_agents", []))
            elif stage_id == current:
                continue
    usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
    active_blockers = state.get("active_blockers")
    if not isinstance(active_blockers, list):
        active_blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
    blocker_counts = state.get("blocker_counts") if isinstance(state.get("blocker_counts"), dict) else {}
    compact = {
        "phase": state.get("phase"),
        "stage": current,
        "elapsed_seconds": state.get("elapsed_seconds"),
        "remaining_seconds": state.get("remaining_seconds"),
        "active_agents": list(dict.fromkeys(item for item in active if isinstance(item, str))),
        "usage": {key: usage.get(key, 0) for key in (
            "model_calls", "input_tokens", "output_tokens", "openalex_requests")},
        "blockers": len(active_blockers),
        "historical_blockers": blocker_counts.get("historical", len(state.get("blockers", [])))
        if isinstance(state.get("blockers"), list) else blocker_counts.get("historical", 0),
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


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

    p_dashboard = sub.add_parser("dashboard", help="serve a read-only dashboard for any project")
    p_dashboard.add_argument("project_dir", nargs="?", default=None)
    p_dashboard.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost)")
    p_dashboard.add_argument("--port", type=int, default=0, help="bind port (default: choose a free local port)")
    p_dashboard.add_argument("--no-open", action="store_true", help="print the URL without opening a browser")

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

    p_resume_survey = sub.add_parser(
        "resume-survey", help="resume an interrupted survey from durable evidence and explicit accounting policy")
    p_resume_survey.add_argument("project_dir")
    p_resume_survey.add_argument("--config", required=True)
    p_resume_survey.add_argument("--additional-seconds", required=True, type=float)
    p_resume_survey.add_argument("--reconcile-unknown", action="store_true",
                                 help="charge one conservative model call for each uncertain prior attempt")
    p_resume_survey.add_argument("--reopen-scope", action="append", default=[],
                                 choices=["operations", "retrieval", "mapping", "focused_review",
                                          "integrated_review", "gap_assessment", "production", "rendering"])

    p_visual = sub.add_parser(
        "run-visual-review", help="evaluate hash-pinned figures or visual concepts with multimodal review")
    p_visual.add_argument("project_dir")
    p_visual.add_argument("--config", required=True)
    p_visual.add_argument("--deadline-seconds", type=float)
    p_visual.add_argument("--target-seconds", type=float)
    p_visual.add_argument("--first-result-seconds", type=float)

    p_experiment = sub.add_parser(
        "run-experiment", help="execute, replay, independently recalculate, and review a frozen study")
    p_experiment.add_argument("project_dir")
    p_experiment.add_argument("--config", required=True)
    p_experiment.add_argument("--deadline-seconds", type=float)
    p_experiment.add_argument("--target-seconds", type=float)
    p_experiment.add_argument("--first-result-seconds", type=float)

    p_interpretation = sub.add_parser(
        "run-interpretation", help="derive an evidence-bound scientific interpretation before manuscript writing")
    p_interpretation.add_argument("--input", required=True, help="JSON evidence packet")
    p_interpretation.add_argument("--config", required=True, help="JSON model configuration")
    p_interpretation.add_argument("--output", required=True, help="JSON interpretation output")

    p_argument = sub.add_parser(
        "run-argument", help="discover and independently adjudicate the scientific argument before writing")
    p_argument.add_argument("--input", required=True, help="JSON evidence packet")
    p_argument.add_argument("--config", required=True, help="JSON model configuration")
    p_argument.add_argument("--output", required=True, help="JSON research-argument package output")
    p_argument.add_argument("--deadline-seconds", type=float, default=900.0)
    p_argument.add_argument("--min-figures", type=int, default=2)
    p_argument.add_argument("--min-tables", type=int, default=1)
    p_argument.add_argument("--min-experiments", type=int, default=2)

    p_blind = sub.add_parser("prepare-evaluation", help="export label-free cases from a frozen judgment corpus")
    p_blind.add_argument("--corpus", required=True)
    p_blind.add_argument("--output", required=True)

    p_score = sub.add_parser("score-evaluation", help="score predictions against a frozen judgment corpus")
    p_score.add_argument("--corpus", required=True)
    p_score.add_argument("--predictions", required=True)
    p_score.add_argument("--output", required=True)
    p_score.add_argument("--min-accuracy", type=float, default=0.8)
    p_score.add_argument("--min-coverage", type=float, default=0.8)
    p_score.add_argument("--max-decisive-false-positive-rate", type=float, default=0.05)

    p_paper = sub.add_parser("build-paper", help="build a provenance-bound LaTeX/PDF release candidate")
    p_paper.add_argument("release_dir")
    p_paper.add_argument("--config", required=True)
    p_paper.add_argument("--compile-script", default=(
        "/Users/seungyeop/.codex/plugins/cache/openai-bundled/latex/0.2.6/scripts/compile_latex.py"))

    p_run_paper = sub.add_parser("run-paper", help="autonomously write, surgically repair, review, and release a paper")
    p_run_paper.add_argument("--packet", required=True, help="JSON writer packet")
    p_run_paper.add_argument("--model-config", required=True, help="JSON model configuration")
    p_run_paper.add_argument("--paper-config", required=True, help="paper release configuration")
    p_run_paper.add_argument("--output-dir", required=True, help="new pipeline output directory")
    p_run_paper.add_argument("--draft", help="resume from an existing validated structured manuscript draft")
    p_run_paper.add_argument("--initial-review-package", help="reuse a completed review package when resuming a run")
    p_run_paper.add_argument("--argument-package", help="reuse an independently accepted research-argument package")
    p_run_paper.add_argument("--image", action="append", default=[], help="PNG/JPEG passed to every reviewer")
    p_run_paper.add_argument("--review-deadline-seconds", type=float, default=1200.0,
                             help="hard wall-clock limit for one independent-review batch")
    p_run_paper.add_argument("--release-on-review-limit", action="store_true",
                             help="publish the incumbent as candidate_needs_review when the final review remains unresolved")
    p_run_paper.add_argument("--pipeline-deadline-seconds", type=float, default=3600.0,
                             help="hard wall-clock limit for the whole paper run")
    p_run_paper.add_argument("--argument-deadline-seconds", type=float, default=900.0,
                             help="hard wall-clock limit for argument discovery and adjudication")
    p_run_paper.add_argument("--min-argument-figures", type=int, default=2,
                             help="minimum argument-linked figures")
    p_run_paper.add_argument("--min-argument-tables", type=int, default=1,
                             help="minimum argument-linked tables")
    p_run_paper.add_argument("--min-argument-experiments", type=int, default=2,
                             help="minimum discriminating experiments in the argument map")
    p_run_paper.add_argument("--compile-script", default=(
        "/Users/seungyeop/.codex/plugins/cache/openai-bundled/latex/0.2.6/scripts/compile_latex.py"))
    p_run_paper.add_argument("--max-review-rounds", type=int, default=3)

    p_composer = sub.add_parser(
        "run-composer", help="run a project-scoped end-to-end research workflow under Executive Command")
    p_composer.add_argument("--workflow", required=True, help="immutable composer workflow JSON")
    p_composer.add_argument("--resume", action="store_true", help="resume the matching composer project")
    p_composer.add_argument(
        "--extend-deadline-seconds", type=float,
        help="extend the existing Composer mission wall before resuming (requires --resume)")
    p_composer.add_argument(
        "--env-file", action="append", default=[],
        help="owner-local runtime env file; may be repeated and is loaded before model dispatch")
    p_composer.add_argument(
        "--watch", action="store_true",
        help="keep supervising the mission and automatically resume recoverable exits")
    p_composer.add_argument(
        "--watch-interval", type=float, default=5.0,
        help="minimum seconds between automatic Composer resumes")

    p_review_article = sub.add_parser("run-review-article", help="scout, synthesize, render and independently review a critical review article")
    p_review_article.add_argument("--config", required=True)
    p_review_article.add_argument("--env-file", action="append", default=[])

    p_prepare_review = sub.add_parser("prepare-review-article", help="prepare a review-only Composer workflow from existing provider settings without model calls")
    p_prepare_review.add_argument("--from-workflow", required=True)
    p_prepare_review.add_argument("--output-dir", required=True)
    p_prepare_review.add_argument("--brief", required=True)

    p_interim = sub.add_parser(
        "composer-interim-report", help="print the latest concise Composer stop/progress report")
    p_interim.add_argument("project_dir")

    args = parser.parse_args(argv)
    if args.cmd in {"run-review-article", "prepare-review-article"}:
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.composer import load_runtime_environment_files
        from scisaurus.runtime.review_article import ReviewArticleRunner, prepare_review_workflow
        try:
            if args.cmd == "prepare-review-article":
                result = prepare_review_workflow(args.from_workflow, args.output_dir, args.brief)
            else:
                load_runtime_environment_files(args.env_file)
                result = ReviewArticleRunner(json.loads(Path(args.config).read_text())).run()
        except (OSError, ValueError, ValidationError) as exc:
            print(f"review article rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") in {"prepared", "candidate_needs_review"} else 2
    if args.cmd == "dashboard":
        from scisaurus.dashboard import run_dashboard
        try:
            project_dir = args.project_dir or _default_dashboard_project_dir()
            run_dashboard(project_dir, host=args.host, port=args.port,
                          open_browser=not args.no_open)
        except (OSError, ValueError) as exc:
            print(f"dashboard rejected: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "run-composer":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.composer import ComposerRunner, load_runtime_environment_files
        try:
            workflow = json.loads(Path(args.workflow).read_text())
            load_runtime_environment_files(args.env_file)
            on_progress = lambda state: print(_composer_progress_line(state), flush=True)
            if args.watch:
                from scisaurus.runtime.composer_supervisor import supervise_composer
                if args.extend_deadline_seconds is not None:
                    raise ValidationError("--extend-deadline-seconds cannot be combined with --watch")
                result = supervise_composer(
                    workflow, initial_resume=args.resume,
                    poll_seconds=args.watch_interval, on_progress=on_progress)
            else:
                result = ComposerRunner(workflow, resume=args.resume,
                                        additional_seconds=args.extend_deadline_seconds,
                                        on_progress=on_progress).run()
        except (OSError, ValueError, ValidationError) as exc:
            print(f"composer workflow rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "elapsed_seconds": result["elapsed_seconds"],
                          "deadline_seconds": result.get("deadline_seconds"),
                          "stages": result["stages"], "release_status": result["release_status"],
                          "continuation_policy": result.get("continuation_policy"),
                          "continuation_cycles": result.get("continuation_cycles", 0),
                          "active_research_requests": result.get("active_research_requests", []),
                          "organization": result.get("organization"),
                          "report": str(Path(workflow["project_id"]).resolve() / "output" / "run.json"),
                          "interim_report": result.get("interim_report_path")}, indent=2))
        return 0 if result["status"] == "completed" else 3
    if args.cmd == "composer-interim-report":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.composer import read_interim_report
        try:
            report = read_interim_report(args.project_dir)
        except (OSError, ValueError, ValidationError) as exc:
            print(f"composer interim report unavailable: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run-paper":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.paper_pipeline import PaperPipelineRunner
        try:
            packet = json.loads(Path(args.packet).read_text())
            model_config = json.loads(Path(args.model_config).read_text())
            paper_config = json.loads(Path(args.paper_config).read_text())
            draft = json.loads(Path(args.draft).read_text()) if args.draft else None
            initial_review_package = (json.loads(Path(args.initial_review_package).read_text())
                                      if args.initial_review_package else None)
            argument_package = (json.loads(Path(args.argument_package).read_text())
                                if args.argument_package else None)
            result = PaperPipelineRunner(packet=packet, model_config=model_config, paper_config=paper_config,
                                         output_dir=args.output_dir, image_paths=args.image,
                                         compile_script=args.compile_script,
                                         max_review_rounds=args.max_review_rounds, draft=draft,
                                         initial_review_package=initial_review_package,
                                         initial_argument_package=argument_package,
                                         review_deadline_seconds=args.review_deadline_seconds,
                                         release_on_review_limit=args.release_on_review_limit,
                                         pipeline_deadline_seconds=args.pipeline_deadline_seconds,
                                         argument_deadline_seconds=args.argument_deadline_seconds,
                                         min_argument_figures=args.min_argument_figures,
                                         min_argument_tables=args.min_argument_tables,
                                         min_argument_experiments=args.min_argument_experiments).run()
        except (OSError, ValueError, ValidationError) as exc:
            print(f"paper pipeline rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "word_count": result.get("word_count", 0),
                          "review_rounds": result.get("review_rounds", 0),
                          "review_status": result.get("review_status"),
                          "elapsed_seconds": result.get("elapsed_seconds"), "pdf": result.get("pdf"),
                          "preflight": result.get("preflight_path"),
                          "research_expansion_requests": result.get("research_expansion_requests", [])}, indent=2))
        return 0 if result["status"] in {"completed", "accepted"} else 3
    if args.cmd == "run-interpretation":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.scientific_interpretation import ScientificInterpretationRunner
        try:
            model_config = json.loads(Path(args.config).read_text())
            packet = json.loads(Path(args.input).read_text())
            evidence_ids = packet.get("evidence_ids") if isinstance(packet, dict) else None
            result = ScientificInterpretationRunner(model_config).run(packet, evidence_ids=evidence_ids)
            # The durable interpretation file is the reader-facing scientific
            # object consumed by manuscript assembly.  Transport usage and
            # model-call accounting stay in the command output/ledger rather
            # than becoming manuscript input.
            Path(args.output).write_bytes(json.dumps(result["interpretation"], sort_keys=True,
                                                   separators=(",", ":"), ensure_ascii=False).encode())
        except (OSError, ValueError, ValidationError) as exc:
            print(f"scientific interpretation rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "output": str(Path(args.output).resolve())}, indent=2))
        return 0
    if args.cmd == "run-argument":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.research_argument import ResearchArgumentRunner, argument_evidence_packet
        try:
            model_config = json.loads(Path(args.config).read_text())
            packet = json.loads(Path(args.input).read_text())
            evidence_packet = argument_evidence_packet(packet)
            result = ResearchArgumentRunner(model_config, deadline_seconds=args.deadline_seconds).run(
                evidence_packet,
                evidence_ids=evidence_packet.get("evidence_ids", []),
                min_figures=args.min_figures,
                min_tables=args.min_tables,
                min_experiments=args.min_experiments)
            Path(args.output).write_bytes(json.dumps(result, sort_keys=True, separators=(",", ":"),
                                                    ensure_ascii=False).encode())
        except (OSError, ValueError, ValidationError) as exc:
            print(f"research argument rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "output": str(Path(args.output).resolve()),
                          "argument_sha256": result["argument_sha256"]}, indent=2))
        return 0
    if args.cmd == "build-paper":
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.paper import PaperReleaseBuilder
        try:
            config = json.loads(Path(args.config).read_text())
            result = PaperReleaseBuilder(args.release_dir, config).build(
                compile_script=Path(args.compile_script).resolve())
        except (OSError, ValueError, ValidationError) as exc:
            print(f"paper build rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "release_ref": result["release_ref"],
                          "pdf": str(Path(args.release_dir).resolve() / "output" / "pdf" /
                                     f"{config['paper_id']}.pdf")}, indent=2))
        return 0
    if args.cmd in {"prepare-evaluation", "score-evaluation"}:
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.evaluation import JudgmentEvaluation, load_corpus
        try:
            evaluation = JudgmentEvaluation(load_corpus(args.corpus))
            if args.cmd == "prepare-evaluation":
                result = evaluation.blind_packet()
            else:
                submission = json.loads(Path(args.predictions).read_text())
                result = evaluation.score(submission, thresholds={
                    "min_accuracy": args.min_accuracy, "min_coverage": args.min_coverage,
                    "max_decisive_false_positive_rate": args.max_decisive_false_positive_rate,
                })
            Path(args.output).write_bytes(
                json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode())
        except (OSError, ValueError, ValidationError) as exc:
            print(f"evaluation rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result.get("status", "prepared"),
                          "output": str(Path(args.output).resolve())}, indent=2))
        return 0
    if args.cmd in {"run-paragraph", "run-project", "run-survey", "resume-survey", "run-visual-review", "run-experiment"}:
        from scisaurus.core.errors import ContractError, ValidationError
        if args.cmd == "run-experiment":
            from scisaurus.runtime.experiment_config import load_experiment_config as load_config
            from scisaurus.runtime.experiment import ExperimentRunner as Runner
        elif args.cmd == "run-visual-review":
            from scisaurus.runtime.visual_review_config import load_visual_review_config as load_config
            from scisaurus.runtime.visual_review import VisualReviewRunner as Runner
        elif args.cmd in {"run-survey", "resume-survey"}:
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
            if args.cmd in {"run-project", "run-survey", "run-visual-review", "run-experiment"}:
                overrides = {name: getattr(args, flag) for name, flag in (
                    ("hard_seconds", "deadline_seconds"), ("target_seconds", "target_seconds"),
                    ("first_result_seconds", "first_result_seconds")) if getattr(args, flag) is not None}
                if overrides:
                    if not any(key in config for key in ("score", "survey", "visual_review", "experiment")):
                        raise ValidationError(
                            "Time planning options require a versioned Score configuration")
                    existing_policy = config.get("time_policy") or {}
                    # Preserve the stored numeric representation when a CLI
                    # override is semantically identical (21600 and 21600.0).
                    # Resume compares the exact configuration bytes; changing
                    # only JSON number spelling must not make a run look like
                    # a different mission.
                    normalized_overrides = {
                        key: (int(value) if type(existing_policy.get(key)) is int
                              and isinstance(value, float) and value.is_integer() else value)
                        for key, value in overrides.items()
                    }
                    config["time_policy"] = {**existing_policy, **normalized_overrides}
            kwargs = {"on_progress": lambda state: print(json.dumps(state), flush=True)}
            if args.cmd == "resume-survey":
                scopes = list(dict.fromkeys(args.reopen_scope))
                kwargs["resume_policy"] = {
                    "additional_seconds": args.additional_seconds,
                    "unknown_outcomes": {
                        "mode": "charge_and_retry" if args.reconcile_unknown else "block",
                        "usage_per_attempt": {"model_calls": 1} if args.reconcile_unknown else {},
                    },
                    "source_changes": {
                        "mode": "reopen" if scopes else "reject",
                        "reopen_scopes": scopes,
                    },
                }
            result = Runner(args.project_dir, config, **kwargs).run()
        except ContractError as exc:
            print(f"configuration rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"status": result["status"], "incumbent_ref": result["incumbent_ref"],
                          "report": str(Path(args.project_dir).resolve() / "output" / (
                              "survey.md" if args.cmd in {"run-survey", "resume-survey"}
                              else "visual-assessment.md" if args.cmd == "run-visual-review"
                              else "experiment.md" if args.cmd == "run-experiment"
                              else "report.md"))}, indent=2))
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
