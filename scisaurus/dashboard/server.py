"""A small localhost console for live Sci-saurus project state.

The dashboard reads the same durable JSON checkpoints, artifact objects, and
SQLite ledgers used by the runtime. Snapshot and file inspection are
read-only. The only mutations are local, allowlisted Composer workflow
creation and start/resume actions under the configured workspace. The default
bind address is localhost because the console can expose private project paths
and research records.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from scisaurus.core.schema import SCHEMA_VERSION, GENESIS_HASH, canonical_bytes, sha256_hex


APP_VERSION = "1"
MAX_FILE_PREVIEW_BYTES = 800_000
MAX_RAW_FILE_BYTES = 25_000_000
MAX_FILES = 600
MAX_WALK_FILES = 8_000
MAX_DIRECTORIES = 1_500
MAX_ARTIFACTS = 300
MAX_ACTIVITY = 100
MAX_DB_EVENTS = 80
MAX_NOTICES = 12
MAX_LOG_FILES = 10
MAX_LOG_LINES = 180
MAX_LOG_BYTES = 600_000
MAX_RECENT_WORK = 32
MAX_MODEL_CALLS = 8
MAX_MODEL_HISTORY_CALLS = 8
MAX_MODEL_CONTEXT_BYTES = 512_000
MAX_RESEARCH_BRANCHES = 8
MAX_RESEARCH_CLAIMS = 24
MAX_RESEARCH_WEAK_POINTS = 12
MAX_RESEARCH_EVIDENCE_REFS = 16
MAX_ACTION_BYTES = 64_000
MAX_PROJECTS = 64
MAX_WORKSPACE_RECENT_PROJECTS = 12
MIN_PROJECT_HARD_SECONDS = 3_600
MAX_PROJECT_HARD_SECONDS = 604_800

STAGE_LABELS = {
    "topic": "Topic discovery",
    "survey": "Literature survey",
    "experiment": "Experiment",
    "interpretation": "Interpretation",
    "argument": "Argument",
    "paper": "Paper release",
}
STAGE_ORDER = tuple(STAGE_LABELS)
SKIP_DIRECTORIES = {
    ".git", ".venv", "__pycache__", "node_modules", "objects", ".staging",
}
TEXT_SUFFIXES = {
    ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".py", ".toml",
    ".ini", ".cfg", ".sh", ".tex", ".csv", ".log", ".svg", ".html",
    ".css", ".js",
}
CHECKPOINT_NAMES = {"progress.json", "interim_report.json", "checkpoint.json", "run.json"}
ACTIVE_STATES = {"proposed", "queued", "running", "awaiting_review", "started"}
MODEL_LIVE_STATES = {"queued", "running", "started"}
TERMINAL_STATES = {"completed", "failed", "rejected", "cancelled", "stale"}


def _json(value, default=None):
    if not isinstance(value, (str, bytes, bytearray)):
        return value if value is not None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return default


def _read_json(path: Path):
    try:
        return _json(path.read_text(encoding="utf-8"), None)
    except (OSError, UnicodeError, ValueError):
        return None


def _iso_timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _safe_int(value, default=0):
    return value if type(value) is int and value >= 0 else default


def _safe_float(value, default=None):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _short(value, limit=180):
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _bounded_notice(value):
    if not isinstance(value, dict):
        return _short(value, 420)
    fields = (
        "stage_id", "task_id", "attempt_id", "attempts", "status", "outcome",
        "reason", "error", "scope", "action", "next_action",
    )
    result = {}
    for key in fields:
        item = value.get(key)
        if item in (None, "", [], {}):
            continue
        result[key] = _short(item, 420) if not isinstance(item, (int, float, bool)) else item
    return result or _short(value, 420)


def _bounded_notices(value):
    if not isinstance(value, list):
        return []
    return [_bounded_notice(item) for item in value[:MAX_NOTICES]]


def _redact_log_text(value):
    text = str(value)
    return re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization|password)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[redacted]",
        text,
    )


def _display_status(value):
    value = str(value or "unknown").lower()
    aliases = {"accepted": "completed", "success": "completed", "succeeded": "completed"}
    return aliases.get(value, value)


def _file_kind(path: Path):
    name = path.name.lower()
    if name in CHECKPOINT_NAMES or "checkpoint" in name:
        return "checkpoint"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}:
        return "media"
    if path.suffix.lower() in {".sqlite", ".db"}:
        return "database"
    if path.suffix.lower() in TEXT_SUFFIXES:
        return "document"
    return "file"


def _ref(root_key: str, relative: Path) -> str:
    return f"{root_key}::{relative.as_posix()}"


def _relative_file(root: Path, candidate: Path):
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        return resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None


def _verify_chain(conn):
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        trusted_row = conn.execute("SELECT value FROM meta WHERE key='event_head'").fetchone()
        if trusted_row is None:
            return {"status": "unknown", "reason": "event head is not recorded"}
        previous = GENESIS_HASH
        for row in rows:
            record = {
                "schema_version": SCHEMA_VERSION,
                "seq": row["seq"],
                "ts": row["ts"],
                "actor": row["actor"],
                "event_type": row["event_type"],
                "payload": _json(row["payload_json"], {}),
                "causal": _json(row["causal_json"], []),
            }
            if row["seq"] < 1 or row["prev_event_hash"] != previous:
                return {"status": "failed", "reason": f"broken chain at seq={row['seq']}"}
            expected = sha256_hex(canonical_bytes({**record, "prev_event_hash": previous}))
            if row["event_hash"] != expected:
                return {"status": "failed", "reason": f"hash mismatch at seq={row['seq']}"}
            previous = row["event_hash"]
        if previous != trusted_row["value"]:
            return {"status": "failed", "reason": "trusted head does not match the event chain"}
        return {"status": "ok", "head": previous, "events": len(rows)}
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        return {"status": "unknown", "reason": f"verification unavailable: {type(exc).__name__}"}


class DashboardSnapshot:
    """Build a bounded, JSON-safe read model from a project directory."""

    def __init__(self, project_dir):
        self.root = Path(project_dir).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"dashboard project directory does not exist: {self.root}")
        if self.root == Path("/"):
            raise ValueError("dashboard refuses to inspect the filesystem root")
        self.workflow_path, self.workflow = self._load_workflow()
        self.roots = self._discover_roots()

    def _load_workflow(self):
        candidates = [
            self.root / "workflow.json",
            self.root / "composer" / "workflow.json",
            self.root.parent / "workflow.json",
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            value = _read_json(candidate)
            if isinstance(value, dict) and isinstance(value.get("stages"), list):
                return candidate.resolve(), value
        return None, {}

    def _discover_roots(self):
        roots = {"project": self.root}
        workflow_project = self.workflow.get("project_id")
        composer_root = None
        if isinstance(workflow_project, str):
            candidate = Path(workflow_project).expanduser()
            if candidate.is_dir():
                composer_root = candidate.resolve()
                roots["composer"] = composer_root
        stages = self.workflow.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                stage_id = stage.get("id")
                stage_dir = stage.get("project_dir")
                if isinstance(stage_id, str) and isinstance(stage_dir, str):
                    candidate = Path(stage_dir).expanduser()
                    if candidate.is_dir():
                        roots[f"stage:{stage_id}"] = candidate.resolve()
        # A resumed Composer writes each retry into an attempt-specific stage
        # directory.  The workflow keeps the stable stage root, while the
        # live checkpoint is the authority for the directory actually being
        # executed.  Include that directory as a separate read-only root so
        # provider tasks and capacity windows are not mistaken for stale data
        # from the stable stage root.
        progress_candidates = []
        if composer_root is not None:
            progress_candidates.append(composer_root / "output" / "progress.json")
        progress_candidates.extend((self.root / relative) for relative in (
            Path("composer/output/progress.json"), Path("output/progress.json")))
        seen_progress = set()
        for progress_path in progress_candidates:
            if progress_path in seen_progress or not progress_path.is_file():
                continue
            seen_progress.add(progress_path)
            progress = _read_json(progress_path)
            progress_stages = progress.get("stages") if isinstance(progress, dict) else None
            if not isinstance(progress_stages, dict):
                continue
            for stage_id, record in progress_stages.items():
                if not isinstance(stage_id, str) or not isinstance(record, dict):
                    continue
                if record.get("status") != "running":
                    continue
                project_dir = record.get("project_dir")
                if not isinstance(project_dir, str) or not project_dir:
                    continue
                candidate = Path(project_dir).expanduser()
                if candidate.is_dir():
                    roots[f"stage:{stage_id}:active"] = candidate.resolve()
        if not self.workflow:
            projects = self.root / "projects"
            if projects.is_dir():
                for child in sorted(projects.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        roots[f"stage:{child.name}"] = child.resolve()
        deduplicated = {}
        for key, path in roots.items():
            deduplicated.setdefault(str(path), (key, path))
        return {key: path for key, path in deduplicated.values()}

    def _checkpoint_files(self):
        seen = set()
        names = ("output/progress.json", "output/interim_report.json", "output/run.json",
                 "progress.json", "interim_report.json", "run.json")
        for root_key, base in self.roots.items():
            for relative_name in names:
                path = base / relative_name
                identity = str(path)
                if identity in seen or not path.is_file():
                    continue
                seen.add(identity)
                value = _read_json(path)
                if isinstance(value, dict):
                    try:
                        modified = path.stat().st_mtime
                    except OSError:
                        modified = 0
                    yield {
                        "root_key": root_key,
                        "path": path,
                        "relative": Path(relative_name),
                        "value": value,
                        "modified": modified,
                    }

    def _live_checkpoint(self):
        records = list(self._checkpoint_files())
        if not records:
            return None
        def score(record):
            value = record["value"]
            status = _display_status(value.get("status"))
            name = record["path"].name
            priority = {"progress.json": 3, "interim_report.json": 2, "run.json": 1}.get(name, 0)
            return (status == "running", priority, record["modified"])
        return max(records, key=score)

    def _stage_specs(self):
        specs = []
        for stage in self.workflow.get("stages", []):
            if isinstance(stage, dict) and isinstance(stage.get("id"), str):
                specs.append(stage)
        if not specs:
            stage_ids = set()
            for record in self._checkpoint_files():
                stages = record["value"].get("stages")
                if isinstance(stages, dict):
                    stage_ids.update(key for key in stages if isinstance(key, str))
                elif isinstance(stages, list):
                    stage_ids.update(item.get("id") for item in stages if isinstance(item, dict))
            for stage_id in sorted(stage_ids):
                if stage_id:
                    specs.append({"id": stage_id, "kind": stage_id})
        order = {stage_id: index for index, stage_id in enumerate(STAGE_ORDER)}
        return sorted(specs, key=lambda item: (order.get(item["id"], len(order)), item["id"]))

    @staticmethod
    def _stage_record(value, stage_id):
        stages = value.get("stages") if isinstance(value, dict) else None
        if isinstance(stages, dict) and isinstance(stages.get(stage_id), dict):
            return stages[stage_id]
        if isinstance(stages, list):
            for item in stages:
                if isinstance(item, dict) and item.get("id") == stage_id:
                    return item
        return None

    def _stage_summary(self, spec, live):
        stage_id = spec["id"]
        candidates = []
        if live is not None:
            record = self._stage_record(live["value"], stage_id)
            if isinstance(record, dict):
                candidates.append((live["modified"], record, live["root_key"]))
        stage_root = self.roots.get(f"stage:{stage_id}")
        if stage_root is not None:
            for record in self._checkpoint_files():
                if record["root_key"] != f"stage:{stage_id}":
                    continue
                value = record["value"]
                stage_record = self._stage_record(value, stage_id) or value
                if isinstance(stage_record, dict):
                    candidates.append((record["modified"], stage_record, record["root_key"]))
        if candidates:
            _, raw, source_root = max(candidates, key=lambda item: item[0])
        else:
            raw, source_root = {}, f"stage:{stage_id}"
        status = _display_status(raw.get("status"))
        if status == "unknown":
            if raw.get("active_agents") or raw.get("attempt_id") or raw.get("attempt_number"):
                status = "running"
            elif raw.get("attempts"):
                status = "pending"
        attempts = raw.get("attempts")
        if isinstance(attempts, list):
            attempt_count = len(attempts)
        else:
            attempt_count = _safe_int(raw.get("attempt_count"), 0)
        active_agents = raw.get("active_agents") if isinstance(raw.get("active_agents"), list) else []
        required_agents = raw.get("required_agents") if isinstance(raw.get("required_agents"), list) else []
        return {
            "id": stage_id,
            "kind": spec.get("kind", stage_id),
            "label": STAGE_LABELS.get(stage_id, stage_id.replace("_", " ").title()),
            "status": status,
            "attempts": attempt_count,
            "active_agents": [str(item) for item in active_agents if isinstance(item, str)],
            "required_agents": [str(item) for item in required_agents if isinstance(item, str)],
            "verifier_agent": raw.get("verifier_agent"),
            "attempt_id": raw.get("attempt_id"),
            "attempt_number": raw.get("attempt_number"),
            "assignment_plan_ref": raw.get("assignment_plan_ref"),
            "assignment_task_ids": raw.get("assignment_task_ids", []),
            "specialist_live": raw.get("specialist_live", {}),
            "error": _short(raw.get("error"), 280),
            "source_root": source_root,
            "project_dir": spec.get("project_dir") or str(stage_root or ""),
        }

    def _db_connection(self, path):
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.35)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _db_records(self):
        dbs = []
        seen = set()
        for root_key, base in self.roots.items():
            path = base / "state" / "control.sqlite"
            if path.is_file() and str(path) not in seen:
                seen.add(str(path))
                dbs.append((root_key, path))
        all_artifacts, all_tasks, all_attempts, all_events = [], [], [], []
        integrity, pools, windows = [], [], []
        counts = {"events": 0, "artifacts": 0, "tasks": 0, "attempts": 0,
                  "active_tasks": 0, "completed_tasks": 0, "failed_tasks": 0,
                  "unknown_attempts": 0}
        for root_key, path in dbs:
            try:
                conn = self._db_connection(path)
                try:
                    integrity.append({"root_key": root_key, "path": str(path), **_verify_chain(conn)})
                    event_rows = conn.execute(
                        "SELECT seq, ts, actor, event_type, payload_json, causal_json "
                        "FROM events ORDER BY seq DESC LIMIT ?", (MAX_DB_EVENTS,)
                    ).fetchall()
                    for row in event_rows:
                        all_events.append({
                            "root_key": root_key, "seq": row["seq"], "ts": row["ts"],
                            "actor": row["actor"], "event_type": row["event_type"],
                            "payload": _json(row["payload_json"], {}),
                            "causal": _json(row["causal_json"], []),
                        })
                    for table, key in (("events", "events"), ("artifacts", "artifacts"),
                                       ("tasks", "tasks"), ("attempts", "attempts")):
                        try:
                            counts[key] += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                        except sqlite3.Error:
                            pass
                    task_rows = conn.execute(
                        "SELECT task_id, kind, state, generation, payload_json, updated_at "
                        "FROM tasks ORDER BY updated_at DESC LIMIT ?", (MAX_ARTIFACTS,)
                    ).fetchall()
                    for row in task_rows:
                        state = row["state"]
                        if state in ACTIVE_STATES:
                            counts["active_tasks"] += 1
                        if state == "completed":
                            counts["completed_tasks"] += 1
                        if state in {"failed", "rejected", "cancelled", "stale"}:
                            counts["failed_tasks"] += 1
                        payload = _json(row["payload_json"], {})
                        all_tasks.append({
                            "root_key": root_key, "task_id": row["task_id"], "kind": row["kind"],
                            "state": state, "generation": row["generation"],
                            "updated_at": row["updated_at"], "payload": payload,
                            "role": payload.get("role") or payload.get("agent") if isinstance(payload, dict) else None,
                            "stage_id": payload.get("stage_id") if isinstance(payload, dict) else None,
                        })
                    attempt_rows = conn.execute(
                        "SELECT attempt_id, task_id, state, lease_owner, external_ref, usage_json, "
                        "payload_json, created_at, finished_at FROM attempts ORDER BY created_at DESC LIMIT ?",
                        (MAX_ARTIFACTS,),
                    ).fetchall()
                    for row in attempt_rows:
                        usage = _json(row["usage_json"], {})
                        if isinstance(usage, dict) and usage.get("outcome") == "result_unknown":
                            counts["unknown_attempts"] += 1
                        all_attempts.append({
                            "root_key": root_key, "attempt_id": row["attempt_id"],
                            "task_id": row["task_id"], "state": row["state"],
                            "lease_owner": row["lease_owner"], "external_ref": row["external_ref"],
                            "usage": usage, "payload": _json(row["payload_json"], {}),
                            "created_at": row["created_at"], "finished_at": row["finished_at"],
                        })
                    artifact_rows = conn.execute(
                        "SELECT logical_id, version, artifact_ref, artifact_type, owner, author, "
                        "body_hash, body_media_type, body_size_bytes, task_id, attempt_id, created_at, "
                        "manifest_hash FROM artifacts ORDER BY created_at DESC LIMIT ?", (MAX_ARTIFACTS,)
                    ).fetchall()
                    for row in artifact_rows:
                        body_hash = row["body_hash"]
                        body_ref = None
                        if isinstance(body_hash, str) and re.fullmatch(r"[0-9a-f]{64}", body_hash):
                            body_path = self.roots[root_key] / "objects" / "sha256" / body_hash
                            if body_path.is_file():
                                body_ref = _ref(root_key, Path("objects") / "sha256" / body_hash)
                        all_artifacts.append({
                            "root_key": root_key, "logical_id": row["logical_id"],
                            "version": row["version"], "artifact_ref": row["artifact_ref"],
                            "artifact_type": row["artifact_type"], "owner": row["owner"],
                            "author": row["author"], "body_hash": body_hash,
                            "body_media_type": row["body_media_type"],
                            "body_size_bytes": row["body_size_bytes"], "task_id": row["task_id"],
                            "attempt_id": row["attempt_id"], "created_at": row["created_at"],
                            "manifest_hash": row["manifest_hash"], "file_ref": body_ref,
                            "kind": "checkpoint" if row["artifact_type"] == "progress_checkpoint" else "artifact",
                        })
                    try:
                        for row in conn.execute(
                                "SELECT policy_id, capacity_json, cumulative_usage_json "
                                "FROM resource_pools"):
                            pools.append({"root_key": root_key, "policy_id": row["policy_id"],
                                          "capacity": _json(row["capacity_json"], {}),
                                          "usage": _json(row["cumulative_usage_json"], {})})
                    except sqlite3.Error:
                        pass
                    try:
                        for row in conn.execute(
                                "SELECT window_id, policy_id, state, capacity_json, reserved_json, "
                                "cumulative_usage_json, opened_at, closed_at "
                                "FROM allocation_windows ORDER BY opened_at DESC LIMIT 20"):
                            windows.append({"root_key": root_key, "window_id": row["window_id"],
                                            "policy_id": row["policy_id"], "state": row["state"],
                                            "capacity": _json(row["capacity_json"], {}),
                                            "reserved": _json(row["reserved_json"], {}),
                                            "usage": _json(row["cumulative_usage_json"], {}),
                                            "opened_at": row["opened_at"], "closed_at": row["closed_at"]})
                    except sqlite3.Error:
                        pass
                finally:
                    conn.close()
            except (OSError, sqlite3.Error):
                integrity.append({"root_key": root_key, "path": str(path), "status": "unavailable",
                                  "reason": "database is busy or unreadable"})
        all_events.sort(key=lambda item: (item.get("ts") or "", item.get("seq", 0)), reverse=True)
        all_tasks.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        all_attempts.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        all_artifacts.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {
            "db_paths": [{"root_key": key, "path": str(path)} for key, path in dbs],
            "counts": counts,
            "integrity": integrity,
            "artifacts": all_artifacts[:MAX_ARTIFACTS],
            "tasks": all_tasks[:MAX_ARTIFACTS],
            "attempts": all_attempts[:MAX_ARTIFACTS],
            "events": all_events[:MAX_ACTIVITY],
            "pools": pools,
            "windows": windows,
        }

    def _files(self):
        found = []
        log_candidates = []
        directories = []
        root_stats = []
        total = 0
        scanned_bases = []
        for root_key, base in self.roots.items():
            if not base.is_dir():
                continue
            covered_by = next((item[0] for item in scanned_bases
                               if base.is_relative_to(item[1])), None)
            root_stat = {"root_key": root_key, "path": str(base), "files": 0,
                         "directories": 0, "covered_by": covered_by}
            root_stats.append(root_stat)
            if covered_by is not None:
                continue
            scanned_bases.append((root_key, base))
            try:
                for directory, dirnames, filenames in os.walk(base, followlinks=False):
                    dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRECTORIES)
                    relative_directory = Path(directory).relative_to(base)
                    if relative_directory != Path(".") and len(directories) < MAX_DIRECTORIES:
                        directories.append({
                            "root_key": root_key,
                            "path": relative_directory.as_posix(),
                            "depth": len(relative_directory.parts),
                        })
                        root_stat["directories"] += 1
                    for filename in filenames:
                        total += 1
                        root_stat["files"] += 1
                        path = Path(directory) / filename
                        if filename.lower().endswith(".log") and len(log_candidates) < MAX_LOG_FILES * 3:
                            relative_log = _relative_file(base, path)
                            try:
                                log_stat = path.stat()
                            except OSError:
                                log_stat = None
                            if relative_log is not None and log_stat is not None:
                                log_candidates.append({
                                    "root_key": root_key,
                                    "ref": _ref(root_key, relative_log),
                                    "path": relative_log.as_posix(),
                                    "name": filename,
                                    "size": log_stat.st_size,
                                    "updated_at": datetime.fromtimestamp(log_stat.st_mtime, timezone.utc).isoformat(),
                                })
                        if total > MAX_WALK_FILES:
                            continue
                        relative = _relative_file(base, path)
                        if relative is None:
                            continue
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        found.append({
                            "root_key": root_key,
                            "ref": _ref(root_key, relative),
                            "path": relative.as_posix(),
                            "name": filename,
                            "kind": _file_kind(path),
                            "media_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                            "size": stat.st_size,
                            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        })
            except OSError:
                continue
        found.sort(key=lambda item: item["updated_at"], reverse=True)
        directories.sort(key=lambda item: (item["root_key"], item["path"]))
        log_candidates.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "items": found[:MAX_FILES], "total": total, "truncated": total > MAX_WALK_FILES,
            "directories": directories, "roots": root_stats, "log_files": log_candidates[:MAX_LOG_FILES],
        }

    def _log_stream(self, file_data):
        entries = []
        log_files = list(file_data.get("log_files", []))
        known_refs = {item.get("ref") for item in log_files}
        log_files.extend(item for item in file_data["items"]
                         if item.get("path", "").lower().endswith(".log") and item.get("ref") not in known_refs)
        for file_item in log_files[:MAX_LOG_FILES]:
            base = self.roots.get(file_item.get("root_key"))
            if base is None:
                continue
            path = base / file_item["path"]
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - MAX_LOG_BYTES))
                    raw_lines = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, raw_line in enumerate(raw_lines[-MAX_LOG_LINES:], start=1):
                value = _json(raw_line, None)
                if isinstance(value, dict):
                    phase = value.get("phase") or value.get("event_type") or value.get("status") or "checkpoint"
                    detail = []
                    for key in ("status", "stage_id", "stop_reason", "reason", "error"):
                        if value.get(key) not in (None, ""):
                            detail.append(f"{key.replace('_', ' ')}: {_short(value[key], 420)}")
                    message = str(phase).replace("_", " ")
                    timestamp = value.get("ts") or value.get("timestamp") or file_item.get("updated_at")
                else:
                    message = _redact_log_text(raw_line).strip()
                    detail = []
                    timestamp = file_item.get("updated_at")
                if not message:
                    continue
                entries.append({
                    "id": f"log-{file_item['ref']}-{line_number}",
                    "ts": timestamp,
                    "kind": "runtime_log",
                    "title": _short(message, 180),
                    "detail": " · ".join(detail) or f"source: {file_item['path']}",
                    "stage_id": None,
                    "role": file_item["root_key"],
                    "status": _display_status(value.get("status")) if isinstance(value, dict) else "event",
                    "refs": [file_item["ref"]],
                    "source": file_item["path"],
                })
        entries.sort(key=lambda item: item.get("ts") or "", reverse=True)
        return entries[:MAX_LOG_LINES]

    def _processes(self):
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,etime=,command="],
                check=False, capture_output=True, text=True, timeout=0.7,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        markers = {str(self.root)}
        for path in self.roots.values():
            try:
                markers.add(path.relative_to(Path.cwd().resolve()).as_posix())
            except ValueError:
                continue
        if self.workflow_path is not None:
            markers.add(str(self.workflow_path))
            try:
                markers.add(self.workflow_path.relative_to(Path.cwd().resolve()).as_posix())
            except ValueError:
                pass
        processes = []
        for line in result.stdout.splitlines():
            if not any(marker in line for marker in markers) or "dashboard" in line.lower():
                continue
            match = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
            if not match:
                continue
            processes.append({"pid": int(match.group(1)), "elapsed": match.group(2),
                              "command": _short(match.group(3), 240)})
        return processes[:12]

    @staticmethod
    def _activity_from_department(items):
        result = []
        if not isinstance(items, list):
            return result
        for index, item in enumerate(items[-MAX_ACTIVITY:]):
            if not isinstance(item, dict):
                continue
            action = item.get("action") or item.get("event_type") or "activity"
            stage = item.get("stage_id") or item.get("stage")
            role = item.get("agent") or item.get("role")
            refs = [value for key, value in item.items()
                    if key.endswith("_ref") and isinstance(value, str)]
            detail_parts = []
            for key in ("attempt_number", "verifier_outcome", "reason", "error", "outcome"):
                if item.get(key) not in (None, ""):
                    detail_parts.append(f"{key.replace('_', ' ')}: {_short(item[key], 420)}")
            result.append({
                "id": f"department-{index}", "ts": item.get("ts") or item.get("created_at"),
                "kind": "department", "title": action.replace("_", " "),
                "detail": " · ".join(detail_parts), "stage_id": stage, "role": role,
                "status": _display_status(item.get("status") or item.get("verifier_outcome")),
                "refs": refs,
            })
        return result

    @staticmethod
    def _activity_from_events(events):
        result = []
        for item in events:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            stage = payload.get("stage_id") or payload.get("stage")
            task_id = payload.get("task_id")
            refs = [value for key, value in payload.items()
                    if key.endswith("_ref") and isinstance(value, str)]
            detail = []
            for key in ("task_id", "artifact_ref", "reason", "state", "to"):
                if payload.get(key) not in (None, ""):
                    detail.append(f"{key.replace('_', ' ')}: {_short(payload[key], 420)}")
            result.append({
                "id": f"event-{item.get('root_key')}-{item.get('seq')}",
                "ts": item.get("ts"), "kind": "event",
                "title": str(item.get("event_type") or "event").replace(".", " / "),
                "detail": " · ".join(detail), "stage_id": stage,
                "role": item.get("actor"), "status": "event", "refs": refs,
                "task_id": task_id,
            })
        return result

    @staticmethod
    def _organization_view(organization):
        if not isinstance(organization, dict):
            return None

        def pick(item, fields):
            return {key: item[key] for key in fields if isinstance(item, dict) and key in item}

        departments = []
        for item in organization.get("departments", []):
            if isinstance(item, dict):
                departments.append(pick(item, ("id", "label", "chief", "adversary", "stage_kinds",
                                               "capability_scope", "subscriptions")))
        agents = []
        for item in organization.get("agents") or organization.get("role_pool") or []:
            if isinstance(item, dict):
                agents.append(pick(item, ("id", "role_id", "agent", "label", "department", "appointment",
                                          "execution_kind", "model_role", "independent_review", "reviewer_agent",
                                          "stage_kinds", "internal_role_aliases")))
        assignments = []
        for item in organization.get("active_assignments", []):
            if isinstance(item, dict):
                assignments.append(pick(item, ("assignment_id", "task_id", "stage_id", "stage_kind", "attempt_number",
                                               "department", "role_id", "agent", "assigned_role", "appointment",
                                               "execution_kind", "model_role", "task_state", "attempt_state",
                                               "artifact_ref", "verifier_agent", "reviewer_agent", "deadline_seconds")))
        agent_activity = []
        for item in organization.get("agent_activity", [])[:MAX_ACTIVITY]:
            if isinstance(item, dict):
                agent_activity.append(pick(item, ("assignment_id", "task_id", "stage_id", "attempt_number",
                                                  "department", "role_id", "agent", "assigned_role", "appointment",
                                                  "execution_kind", "model_role", "task_state", "attempt_state",
                                                  "artifact_ref", "verifier_agent", "reviewer_agent")))
        stage_routes = []
        for item in organization.get("stage_routes", []):
            if isinstance(item, dict):
                stage_routes.append(pick(item, ("stage_kind", "department", "functional_role", "chief", "adversary",
                                                "max_active_agents", "required_agents", "verifier_agent")))
        return {
            "schema_version": organization.get("schema_version"),
            "project_id": organization.get("project_id"),
            "template": organization.get("template"),
            "allow_dynamic_proposals": organization.get("allow_dynamic_proposals"),
            "command_agents": organization.get("command_agents") or {},
            "departments": departments,
            "agents": agents,
            "active_assignments": assignments,
            "agent_activity": agent_activity,
            "assignment_counts": organization.get("assignment_counts") or {},
            "backlog_counts": organization.get("backlog_counts") or {},
            "open_work_orders": organization.get("open_work_orders") or [],
            "manifest_refs": organization.get("manifest_refs") or {},
            "stage_routes": stage_routes,
        }

    def _specialists(self, stages, db, organization):
        task_by_id = {item["task_id"]: item for item in db["tasks"] if item.get("task_id")}
        organization = organization if isinstance(organization, dict) else {}
        roster = organization.get("agents") or organization.get("role_pool") or []
        active_assignments = organization.get("active_assignments") or []
        recent_activity = organization.get("agent_activity") or []
        assignment_by_role = {
            item.get("assigned_role"): item for item in active_assignments
            if isinstance(item, dict) and isinstance(item.get("assigned_role"), str)
        }
        latest_by_role = {}
        for item in recent_activity:
            if not isinstance(item, dict):
                continue
            role = item.get("assigned_role")
            if not isinstance(role, str):
                continue
            previous = latest_by_role.get(role)
            if previous is None or _safe_int(item.get("attempt_number")) >= _safe_int(previous.get("attempt_number")):
                latest_by_role[role] = item

        def role_address(item):
            role_id = item.get("id") or item.get("role_id")
            if not isinstance(role_id, str):
                return None
            if "." in role_id:
                return role_id
            department = item.get("department")
            return f"{department}.{role_id}" if isinstance(department, str) else role_id

        specialists = []
        roster_roles = set()
        for item in roster:
            if not isinstance(item, dict):
                continue
            role = role_address(item)
            if not role:
                continue
            roster_roles.add(role)
            assignment = assignment_by_role.get(role)
            previous = latest_by_role.get(role)
            task_id = (assignment or {}).get("task_id") or (previous or {}).get("task_id")
            task = task_by_id.get(task_id) if task_id else None
            task_state = ((assignment or {}).get("task_state") or
                          (task or {}).get("state") or
                          (previous or {}).get("task_state") or
                          (previous or {}).get("attempt_state") or "idle")
            specialists.append({
                "role": role,
                "label": item.get("label") or item.get("agent") or item.get("role_id") or role,
                "department": item.get("department"),
                "appointment": item.get("appointment"),
                "model_role": item.get("model_role"),
                "stage_id": (assignment or {}).get("stage_id") or (previous or {}).get("stage_id"),
                "status": _display_status(task_state),
                "task_id": task_id,
                "task_state": task_state,
                "attempt_state": (assignment or {}).get("attempt_state") or (previous or {}).get("attempt_state"),
                "updated_at": (task or {}).get("updated_at") or (previous or {}).get("updated_at"),
                "reviewer": (assignment or {}).get("reviewer_agent") or item.get("reviewer_agent"),
                "verifier": bool((assignment or {}).get("verifier_agent") or item.get("independent_review")),
                "engaged": task_state in ACTIVE_STATES,
            })

        for assignment in active_assignments:
            if not isinstance(assignment, dict):
                continue
            role = assignment.get("assigned_role")
            if not isinstance(role, str) or role in roster_roles:
                continue
            task_state = assignment.get("task_state") or assignment.get("attempt_state") or "unknown"
            specialists.append({
                "role": role, "label": role, "department": assignment.get("department"),
                "appointment": assignment.get("appointment"), "model_role": assignment.get("model_role"),
                "stage_id": assignment.get("stage_id"), "status": _display_status(task_state),
                "task_id": assignment.get("task_id"), "task_state": task_state,
                "attempt_state": assignment.get("attempt_state"),
                "updated_at": None, "reviewer": assignment.get("reviewer_agent"),
                "verifier": bool(assignment.get("verifier_agent")), "engaged": task_state in ACTIVE_STATES,
            })

        for stage in stages:
            task_ids = stage.get("assignment_task_ids") if isinstance(stage.get("assignment_task_ids"), list) else []
            for index, role in enumerate(stage.get("active_agents", [])):
                if role in roster_roles:
                    continue
                task = task_by_id.get(task_ids[index]) if index < len(task_ids) else None
                specialists.append({
                    "role": role, "label": role, "department": role.split(".", 1)[0] if "." in role else None,
                    "appointment": "specialist", "model_role": None, "stage_id": stage["id"],
                    "status": "running", "task_id": task.get("task_id") if task else None,
                    "task_state": task.get("state") if task else "running",
                    "attempt_state": None, "updated_at": task.get("updated_at") if task else None,
                    "reviewer": None, "verifier": role == stage.get("verifier_agent"), "engaged": True,
                })
        specialists.sort(key=lambda item: (not item.get("engaged"), item.get("department") or "", item.get("role") or ""))
        return specialists

    def _execution_view(self, stages, db, organization):
        """Expose role-assignment and provider-capacity counts separately.

        Department assignments are logical scopes around a stage runner. They
        are deliberately not presented as one provider process per role. The
        durable allocation window is the authority for provider capacity,
        especially after a config file changes while a run is being resumed.
        """
        organization = organization if isinstance(organization, dict) else {}
        running_stage = next((item for item in stages if item.get("status") == "running"), None)
        stage_id = running_stage.get("id") if running_stage else None
        route = next((item for item in organization.get("stage_routes") or []
                      if isinstance(item, dict) and item.get("stage_kind") == (running_stage or {}).get("kind")), None)
        if route is None and stage_id:
            route = next((item for item in organization.get("stage_routes") or []
                          if isinstance(item, dict) and item.get("stage_kind") == stage_id), None)

        assignment_tasks = []
        provider_tasks = []
        for task in db.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            if isinstance(payload.get("assignment_id"), str):
                assignment_tasks.append(task)
            elif payload.get("operation") in {"model", "retrieval"}:
                provider_tasks.append(task)
        active_assignment_tasks = [item for item in assignment_tasks
                                   if item.get("state") in ACTIVE_STATES]
        running_assignments = [item for item in active_assignment_tasks if item.get("state") == "running"]
        queued_assignments = [item for item in active_assignment_tasks if item.get("state") == "queued"]
        specialist_assignments = [item for item in active_assignment_tasks
                                  if (item.get("payload") or {}).get("assignment_phase") == "specialist"]
        verifier_assignments = [item for item in active_assignment_tasks
                                if (item.get("payload") or {}).get("assignment_phase") == "verifier"]

        config_limits = {}
        stage_spec = next((item for item in self._stage_specs()
                           if item.get("id") == stage_id), None)
        config_path = stage_spec.get("config_path") if isinstance(stage_spec, dict) else None
        if isinstance(config_path, str):
            config = _read_json(Path(config_path).expanduser())
            if isinstance(config, dict) and isinstance(config.get("limits"), dict):
                config_limits = config["limits"]
        configured_calls = config_limits.get("concurrent_calls")
        configured_workers = config_limits.get("worker_concurrency")
        if type(configured_calls) is int and configured_workers is None:
            configured_workers = max(1, configured_calls - 1)

        preferred_root_keys = []
        if stage_id:
            preferred_root_keys.extend((f"stage:{stage_id}:active", f"stage:{stage_id}"))
        pools = [item for item in db.get("pools") or [] if isinstance(item, dict)]
        pool = next((item for key in preferred_root_keys
                     for item in pools if item.get("root_key") == key), None)
        if pool is None:
            pool = next((item for item in pools
                         if isinstance(item.get("capacity"), dict)
                         and type(item["capacity"].get("concurrent_calls")) is int), None)
        pool_capacity = pool.get("capacity") if isinstance(pool, dict) else None
        durable_capacity = (pool_capacity or {}).get("concurrent_calls")
        if type(durable_capacity) is not int:
            durable_capacity = None
        provider_running = [item for item in provider_tasks if item.get("state") == "running"]
        provider_queued = [item for item in provider_tasks if item.get("state") == "queued"]
        provider_review = [item for item in provider_tasks if item.get("state") == "awaiting_review"]
        total_capacity = durable_capacity if durable_capacity is not None else configured_calls
        worker_capacity = max(1, total_capacity - 1) if type(total_capacity) is int else configured_workers
        window = next((item for key in preferred_root_keys
                       for item in db.get("windows") or []
                       if isinstance(item, dict) and item.get("root_key") == key), None)
        if window is None:
            window = next((item for item in db.get("windows") or []
                           if isinstance(item, dict) and item.get("window_id") == "run-window"), None)
        return {
            "stage_id": stage_id,
            "role_assignments": {
                "running": len([item for item in specialist_assignments
                                 if item.get("state") == "running"]),
                "queued": len(queued_assignments),
                "active": len(active_assignment_tasks),
                "verifier_active": len(verifier_assignments),
                "limit": route.get("max_active_agents") if isinstance(route, dict) else None,
            },
            "provider": {
                "durable_window_capacity": durable_capacity,
                "configured_capacity": configured_calls,
                "configured_worker_concurrency": configured_workers,
                "worker_capacity": worker_capacity,
                "running_tasks": len(provider_running),
                "queued_tasks": len(provider_queued),
                "awaiting_review_tasks": len(provider_review),
                "ledger_active_tasks": len(provider_running) + len(provider_queued),
                "ledger_active_task_ids": [item.get("task_id") for item in provider_running + provider_queued][:12],
                "within_capacity": (type(total_capacity) is not int
                                     or len(provider_running) + len(provider_queued) <= total_capacity),
                "pool_limits": {
                    name: {"max_concurrent": pool["max_concurrent"]}
                    for name, pool in (config_limits.get("provider_pools") or {}).items()
                    if isinstance(pool, dict) and type(pool.get("max_concurrent")) is int
                },
                "source_root": (pool or {}).get("root_key"),
                "capacity_source": "durable allocation window" if durable_capacity is not None else "stage config",
                "window_state": (window or {}).get("state"),
            },
            "interpretation": "role assignments are logical scopes; provider capacity is enforced separately",
        }

    def _artifact_body(self, artifact):
        """Read a bounded JSON object from the content-addressed store.

        Model context and result artifacts can contain prompts or long
        responses.  The dashboard only needs their structured routing and
        accounting fields, so it reads a small, read-only projection and
        never returns the prompt through the snapshot.
        """
        if not isinstance(artifact, dict):
            return {}
        root_key = artifact.get("root_key")
        body_hash = artifact.get("body_hash")
        root = self.roots.get(root_key)
        if (root is None or not isinstance(body_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", body_hash)):
            return {}
        path = root / "objects" / "sha256" / body_hash
        try:
            if path.stat().st_size > MAX_MODEL_CONTEXT_BYTES:
                return {}
            value = _json(path.read_bytes(), {})
        except (OSError, ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _model_endpoint(base_url):
        """Return a credential-free provider and host label for a route."""
        if not isinstance(base_url, str) or not base_url.strip():
            return {"provider": "unknown", "host": None}
        raw = base_url.strip()
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            host, port = None, None
        host = host or raw.split("/", 1)[0]
        host = str(host)
        host_lower = host.lower()
        if "taila57d41.ts.net" in host_lower or "tailnet" in host_lower:
            provider = "Tailnet"
        elif host_lower in {"127.0.0.1", "localhost"} or "ollama" in host_lower:
            provider = "Ollama"
        else:
            provider = host
        host_label = host if port is None else f"{host}:{port}"
        return {"provider": provider, "host": host_label}

    def _model_calls(self, db, live_value=None):
        """Project real provider model calls into bounded, inspectable cards.

        Assignment tasks represent logical work scopes; these records are
        different.  Only tasks explicitly dispatched with operation=model are
        included, and their model/route comes from the immutable call context
        or the recorded execution result.  Composer specialists do not create
        a second provider task in the control ledger, so their live projection
        is joined from the atomic progress checkpoint below.
        """
        tasks = [item for item in db.get("tasks") or []
                 if isinstance(item, dict)
                 and isinstance(item.get("payload"), dict)
                 and item["payload"].get("operation") == "model"]
        attempts = {}
        for item in db.get("attempts") or []:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            key = (item.get("root_key"), item.get("task_id"))
            previous = attempts.get(key)
            if previous is None or str(item.get("created_at") or "") > str(previous.get("created_at") or ""):
                attempts[key] = item

        artifacts_by_logical_id = {}
        for item in db.get("artifacts") or []:
            if not isinstance(item, dict) or not isinstance(item.get("logical_id"), str):
                continue
            key = (item.get("root_key"), item["logical_id"])
            previous = artifacts_by_logical_id.get(key)
            if previous is None or (
                _safe_int(item.get("version")), str(item.get("created_at") or "")
            ) > (
                _safe_int(previous.get("version")), str(previous.get("created_at") or "")
            ):
                artifacts_by_logical_id[key] = item

        now = datetime.now(timezone.utc)
        state_priority = {"running": 0, "queued": 1, "started": 2}
        calls = []
        for task in tasks:
            root_key = task.get("root_key")
            task_id = task.get("task_id")
            if not isinstance(task_id, str):
                continue
            attempt = attempts.get((root_key, task_id), {})
            context_artifact = artifacts_by_logical_id.get((root_key, f"command/contexts/{task_id}"))
            execution_artifact = artifacts_by_logical_id.get((root_key, f"command/executions/{task_id}"))
            context = self._artifact_body(context_artifact)
            execution = self._artifact_body(execution_artifact)
            client = context.get("client") if isinstance(context.get("client"), dict) else {}
            role = context.get("role") or attempt.get("lease_owner") or task.get("role")
            role_config = {}
            role_models = client.get("role_models") if isinstance(client.get("role_models"), dict) else {}
            if isinstance(role, str) and isinstance(role_models.get(role), dict):
                role_config = role_models[role]
            model = execution.get("model") if isinstance(execution.get("model"), str) else None
            if not model:
                model = role_config.get("model") if isinstance(role_config.get("model"), str) else None
            if not model and isinstance(client.get("model"), str):
                model = client["model"]
            base_url = execution.get("base_url") if isinstance(execution.get("base_url"), str) else None
            if not base_url:
                base_url = role_config.get("base_url") if isinstance(role_config.get("base_url"), str) else None
            if not base_url and isinstance(client.get("base_url"), str):
                base_url = client["base_url"]
            endpoint = self._model_endpoint(base_url)
            provider_pool = context.get("provider_pool")
            route_id = context.get("route_id")
            cache_prompt_value = role_config.get("cache_prompt", client.get("cache_prompt"))
            cache_prompt_requested = cache_prompt_value is True

            state = _display_status(task.get("state"))
            usage = execution.get("usage") if isinstance(execution.get("usage"), dict) else None
            if usage is None and isinstance(attempt.get("usage"), dict):
                usage = attempt["usage"].get("actual") or attempt["usage"].get("reserved")
            usage = usage if isinstance(usage, dict) else {}
            cache_read_tokens = usage.get("cache_read_tokens")
            cache_write_tokens = usage.get("cache_write_tokens")
            input_tokens = usage.get("input_tokens")
            if type(cache_read_tokens) is not int or cache_read_tokens < 0:
                cache_read_tokens = None
            if type(cache_write_tokens) is not int or cache_write_tokens < 0:
                cache_write_tokens = None
            if type(input_tokens) is not int or input_tokens <= 0:
                input_tokens = None
            cache_read_ratio = (
                cache_read_tokens / input_tokens
                if cache_read_tokens is not None and input_tokens is not None else None
            )
            cache_status = (
                "hit" if (cache_read_tokens is not None and cache_read_tokens > 0
                          and input_tokens is not None and cache_read_tokens >= input_tokens)
                else "partial" if cache_read_tokens is not None and cache_read_tokens > 0
                else "primed" if cache_write_tokens is not None and cache_write_tokens > 0
                else "miss" if cache_read_tokens is not None
                else "enabled · unreported" if cache_prompt_requested
                else "not requested"
            )
            response_status = {
                "queued": "queued",
                "running": "awaiting response",
                "awaiting_review": "response recorded · awaiting review",
            }.get(state)
            if response_status is None:
                response_status = "response recorded" if execution_artifact else (
                    "result unknown" if (attempt.get("usage") or {}).get("outcome") == "result_unknown"
                    else "not recorded"
                )

            started_at = _iso_timestamp(attempt.get("created_at"))
            finished_at = _iso_timestamp(attempt.get("finished_at"))
            elapsed_seconds = _safe_float(execution.get("elapsed_seconds"))
            if elapsed_seconds is None and started_at:
                try:
                    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    end = datetime.fromisoformat(finished_at.replace("Z", "+00:00")) if finished_at else now
                    elapsed_seconds = max(0, (end - started).total_seconds())
                except ValueError:
                    elapsed_seconds = None
            stage_id = (context.get("stage_id") if isinstance(context.get("stage_id"), str)
                        else (task.get("stage_id") or None))
            if not isinstance(stage_id, str) or not stage_id:
                stage_id = root_key.split(":", 2)[1] if isinstance(root_key, str) and root_key.startswith("stage:") else None
            calls.append({
                "root_key": root_key,
                "task_id": task_id,
                "attempt_id": attempt.get("attempt_id"),
                "state": state,
                "role": role if isinstance(role, str) else None,
                "stage_id": stage_id,
                "model": model or "model not recorded",
                "provider": endpoint["provider"],
                "endpoint": endpoint["host"],
                "provider_pool": provider_pool if isinstance(provider_pool, str) else None,
                "route_id": route_id if isinstance(route_id, str) else None,
                "cache": {
                    "requested": cache_prompt_requested,
                    "status": cache_status,
                    "read_tokens": cache_read_tokens,
                    "write_tokens": cache_write_tokens,
                    "read_ratio": cache_read_ratio,
                },
                "response_status": response_status,
                "response_ref": execution_artifact.get("file_ref") if execution_artifact else None,
                "artifact_ref": execution_artifact.get("artifact_ref") if execution_artifact else None,
                "started_at": started_at,
                "finished_at": finished_at,
                "updated_at": _iso_timestamp(task.get("updated_at")),
                "elapsed_seconds": elapsed_seconds,
                "usage": {key: value for key, value in usage.items()
                          if key in {"model_calls", "input_tokens", "output_tokens",
                                     "cache_read_tokens", "cache_write_tokens"}
                          and type(value) is int and value >= 0},
            })

        # Specialist assignments are logical ledger scopes around a short
        # provider call.  Read their redacted progress projection so the
        # console reflects the actual in-flight pool instead of showing only
        # the parent stage task.  Completed entries remain bounded history;
        # deterministic/service assignments are intentionally excluded.
        existing_task_ids = {item.get("task_id") for item in calls}
        live_value = live_value if isinstance(live_value, dict) else {}
        for stage_id, stage_record in (live_value.get("stages") or {}).items():
            if not isinstance(stage_id, str) or not isinstance(stage_record, dict):
                continue
            live_records = stage_record.get("specialist_live")
            if not isinstance(live_records, dict):
                continue
            for role, event in live_records.items():
                if not isinstance(event, dict) or event.get("execution_mode", "model") != "model":
                    continue
                task_id = event.get("task_id")
                if not isinstance(task_id, str) or task_id in existing_task_ids:
                    continue
                event_name = event.get("event")
                report_status = str(event.get("status") or "").lower()
                if event_name == "dispatched":
                    state = "running"
                    response_status = "awaiting response"
                elif event_name == "completed":
                    state = _display_status(report_status or "completed")
                    response_status = (
                        "response recorded" if state == "completed"
                        else "result unknown" if state == "result_unknown"
                        else "failed"
                    )
                else:
                    state = _display_status(report_status or "queued")
                    response_status = "awaiting response" if state in MODEL_LIVE_STATES else "not recorded"
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                input_tokens = usage.get("input_tokens")
                cache_read_tokens = usage.get("cache_read_tokens")
                cache_write_tokens = usage.get("cache_write_tokens")
                if type(input_tokens) is not int or input_tokens <= 0:
                    input_tokens = None
                if type(cache_read_tokens) is not int or cache_read_tokens < 0:
                    cache_read_tokens = None
                if type(cache_write_tokens) is not int or cache_write_tokens < 0:
                    cache_write_tokens = None
                cache_ratio = (
                    cache_read_tokens / input_tokens
                    if cache_read_tokens is not None and input_tokens is not None else None
                )
                cache_requested = event.get("cache_prompt") is True
                cache_status = (
                    "hit" if cache_read_tokens is not None and input_tokens is not None
                    and cache_read_tokens >= input_tokens
                    else "partial" if cache_read_tokens is not None and cache_read_tokens > 0
                    else "primed" if cache_write_tokens is not None and cache_write_tokens > 0
                    else "enabled · unreported" if cache_requested else "not requested"
                )
                observed_at = _iso_timestamp(event.get("observed_at"))
                started_at = _iso_timestamp(event.get("started_at")) or observed_at
                updated_at = _iso_timestamp(event.get("updated_at")) or observed_at
                base_url = event.get("base_url") if isinstance(event.get("base_url"), str) else None
                endpoint = self._model_endpoint(base_url)
                calls.append({
                    "root_key": "composer",
                    "task_id": task_id,
                    "attempt_id": None,
                    "state": state,
                    "role": event.get("role") if isinstance(event.get("role"), str) else role,
                    "stage_id": event.get("stage_id") if isinstance(event.get("stage_id"), str) else stage_id,
                    "model": event.get("model") if isinstance(event.get("model"), str) else "model not recorded",
                    "provider": endpoint["provider"],
                    "endpoint": endpoint["host"],
                    "provider_pool": event.get("provider_pool") if isinstance(event.get("provider_pool"), str) else None,
                    "route_id": event.get("route_id") if isinstance(event.get("route_id"), str) else None,
                    "cache": {
                        "requested": cache_requested,
                        "status": cache_status,
                        "read_tokens": cache_read_tokens,
                        "write_tokens": cache_write_tokens,
                        "read_ratio": cache_ratio,
                    },
                    "response_status": response_status,
                    "response_ref": event.get("response_ref") if isinstance(event.get("response_ref"), str) else None,
                    "artifact_ref": event.get("artifact_ref") if isinstance(event.get("artifact_ref"), str) else None,
                    "started_at": started_at,
                    "finished_at": updated_at if state not in MODEL_LIVE_STATES else None,
                    "updated_at": updated_at,
                    "elapsed_seconds": _safe_float(event.get("elapsed_seconds")),
                    "usage": {key: value for key, value in usage.items()
                              if key in {"model_calls", "input_tokens", "output_tokens",
                                         "cache_read_tokens", "cache_write_tokens"}
                              and type(value) is int and value >= 0},
                })
                existing_task_ids.add(task_id)

        def timestamp(value):
            if not isinstance(value, str) or not value:
                return 0.0
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0

        live_calls = [item for item in calls if item.get("state") in MODEL_LIVE_STATES]
        review_calls = [item for item in calls if item.get("state") == "awaiting_review"]
        recent_calls = [item for item in calls if item.get("state") not in MODEL_LIVE_STATES
                        and item.get("state") != "awaiting_review"]
        live_calls.sort(key=lambda item: (
            state_priority.get(item.get("state"), 3),
            -timestamp(item.get("updated_at") or item.get("started_at")),
        ))
        review_calls.sort(key=lambda item: -timestamp(item.get("updated_at") or item.get("started_at")))
        recent_calls.sort(key=lambda item: -timestamp(item.get("updated_at") or item.get("started_at")))
        history = review_calls + recent_calls
        return {
            # `items` remains the compact compatibility field, but it now
            # intentionally means live provider work only.
            "items": live_calls[:MAX_MODEL_CALLS],
            "live_items": live_calls[:MAX_MODEL_CALLS],
            "review_items": review_calls[:MAX_MODEL_HISTORY_CALLS],
            "recent_items": recent_calls[:MAX_MODEL_HISTORY_CALLS],
            "total": len(calls),
            "active": len(live_calls),
            "review_pending": len(review_calls),
            "displayed": min(len(live_calls), MAX_MODEL_CALLS),
            "truncated": len(live_calls) > MAX_MODEL_CALLS,
            "history_total": len(history),
            "history_displayed": min(len(history), MAX_MODEL_HISTORY_CALLS * 2),
            "history_truncated": len(history) > MAX_MODEL_HISTORY_CALLS * 2,
        }

    def _scoped_json_file(self, raw_path):
        """Read a small JSON file only when it remains inside a project root."""
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
            allowed = any(
                resolved == root.resolve() or resolved.is_relative_to(root.resolve())
                for root in self.roots.values()
            )
            if not allowed or not resolved.is_file() or resolved.stat().st_size > MAX_MODEL_CONTEXT_BYTES:
                return None
            value = _read_json(resolved)
        except (OSError, ValueError, RuntimeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _research_program_view(program):
        """Expose the decision surface without leaking prompt-sized payloads."""
        if not isinstance(program, dict):
            return None

        def outcome_view(outcome):
            if not isinstance(outcome, dict):
                return None
            return {
                "id": outcome.get("id"),
                "condition": _short(outcome.get("condition"), 520),
                "contribution": _short(outcome.get("contribution"), 420),
                "required_evidence": [
                    _short(item, 220) for item in (outcome.get("required_evidence") or [])[:8]
                    if isinstance(item, str)
                ],
            }

        def branch_view(branch):
            if not isinstance(branch, dict):
                return None
            return {
                "id": branch.get("id"),
                "title": _short(branch.get("title"), 240),
                "question": _short(branch.get("question"), 620),
                "hypothesis": _short(branch.get("hypothesis"), 620),
                "mechanism": _short(branch.get("mechanism"), 620),
                "plan": _short(branch.get("plan"), 620),
                "research_form": _short(branch.get("research_form"), 180),
                "evidence_mode": _short(branch.get("evidence_mode"), 180),
                "comparison_type": _short(branch.get("comparison_type"), 180),
                "paper_if": [
                    item for item in (outcome_view(value) for value in (branch.get("paper_if") or [])[:3])
                    if item is not None
                ],
                "kill_if": _short(branch.get("kill_if"), 620),
                "evidence_obligations": [
                    _short(item, 260) for item in (branch.get("evidence_obligations") or [])[:8]
                    if isinstance(item, str)
                ],
                "source_refs": [
                    _short(item, 180) for item in (branch.get("source_refs") or [])[:16]
                    if isinstance(item, str)
                ],
                "status": _display_status(branch.get("status")),
            }

        raw_branches = [item for item in (program.get("branches") or []) if isinstance(item, dict)]
        branches = [item for item in (branch_view(value) for value in raw_branches[:MAX_RESEARCH_BRANCHES])
                    if item is not None]
        selected_id = program.get("selected_id")
        selected = next((item for item in branches if item.get("id") == selected_id), None)
        return {
            "schema_version": program.get("schema_version"),
            "theme": _short(program.get("theme"), 240),
            "objective": _short(program.get("objective"), 620),
            "selection_mode": _short(program.get("selection_mode"), 80),
            "selected_id": selected_id,
            "selected_branch": selected,
            "selection_rationale": _short(program.get("selection_rationale"), 620),
            "selection_criteria": [
                _short(item, 320) for item in (program.get("selection_criteria") or [])[:8]
                if isinstance(item, str)
            ],
            "branches": branches,
            "branch_count": len(raw_branches),
            "retained_count": sum(item.get("status") == "retained" for item in branches),
        }

    @staticmethod
    def _argument_defense_view(defense):
        """Expose claim posture and repair signals as a compact review surface."""
        if not isinstance(defense, dict):
            return None

        claims = []
        for claim in (defense.get("claim_postures") or [])[:MAX_RESEARCH_CLAIMS]:
            if not isinstance(claim, dict):
                continue
            claims.append({
                "id": claim.get("id"),
                "claim": _short(claim.get("claim"), 520),
                "posture": _short(claim.get("posture"), 80),
                "evidence_ids": [
                    _short(item, 180) for item in (claim.get("evidence_ids") or [])[:MAX_RESEARCH_EVIDENCE_REFS]
                    if isinstance(item, str)
                ],
                "defense": _short(claim.get("defense"), 520),
                "caveat": _short(claim.get("caveat"), 520),
                "allowed_sections": [
                    _short(item, 80) for item in (claim.get("allowed_sections") or [])[:8]
                    if isinstance(item, str)
                ],
            })

        weak_points = []
        for item in (defense.get("weak_points") or [])[:MAX_RESEARCH_WEAK_POINTS]:
            if not isinstance(item, dict):
                continue
            weak_points.append({
                "id": item.get("id"),
                "weak_point": _short(item.get("weak_point"), 420),
                "defense_strategy": _short(item.get("defense_strategy"), 120),
                "argument": _short(item.get("argument"), 520),
                "remaining_uncertainty": _short(item.get("remaining_uncertainty"), 520),
                "reviewer_test": _short(item.get("reviewer_test"), 520),
            })

        policy = defense.get("policy") if isinstance(defense.get("policy"), dict) else {}
        return {
            "schema_version": defense.get("schema_version"),
            "research_question": _short(defense.get("research_question"), 900),
            "claim_postures": claims,
            "weak_points": weak_points,
            "claim_count": len(defense.get("claim_postures") or []),
            "weak_point_count": len(defense.get("weak_points") or []),
            "policy": {
                key: _short(policy.get(key), 420) for key in (
                    "results_rule", "discussion_rule", "limitation_rule", "missing_evidence_action"
                ) if policy.get(key) not in (None, "")
            },
        }

    def _research_view(self, live_value, stages):
        """Project scientific content separately from control-plane records."""
        live_value = live_value if isinstance(live_value, dict) else {}
        context = live_value.get("context") if isinstance(live_value.get("context"), dict) else {}
        topic_context = context.get("topic") if isinstance(context.get("topic"), dict) else {}
        topic = topic_context.get("topic") if isinstance(topic_context.get("topic"), dict) else {}
        if not topic and isinstance(topic_context, dict):
            topic = {key: topic_context[key] for key in (
                "title", "domain", "research_question", "question", "phenomenon", "mechanism",
                "comparison", "measurement", "data_regime", "disconfirmation_test", "resource_plan",
                "scope", "theory_target", "why_promising",
            ) if key in topic_context}

        def value(*keys, limit=1200):
            for key in keys:
                candidate = topic.get(key) if isinstance(topic, dict) else None
                if candidate in (None, "", [], {}):
                    candidate = topic_context.get(key)
                if candidate not in (None, "", [], {}):
                    return _short(candidate, limit) if not isinstance(candidate, (int, float, bool)) else candidate
            return None

        stage_map = {item.get("id"): item for item in stages if isinstance(item, dict)}
        deliverables = {
            "topic": "Accepted research question and source challenge",
            "survey": "Literature map, coverage, and gap assessment",
            "experiment": "Pinned executable comparison and independent recalculation",
            "interpretation": "Mechanism interpretation with alternatives",
            "argument": "Claim-to-evidence argument package",
            "paper": "Reviewed LaTeX source and rendered PDF",
        }
        gates = {
            "topic": "question admitted",
            "survey": "evidence suffices for experiment",
            "experiment": "results pass recalculation",
            "interpretation": "mechanism survives challenge",
            "argument": "every claim has support",
            "paper": "self-review and release gate",
        }
        stage_progress = []
        for stage in stages:
            stage_id = stage.get("id")
            result = context.get(stage_id) if isinstance(context.get(stage_id), dict) else None
            if result is None:
                result = {}
            stage_progress.append({
                "id": stage_id,
                "label": stage.get("label") or stage_id,
                "status": stage.get("status"),
                "current": stage.get("status") == "running",
                "attempts": stage.get("attempts", 0),
                "attempt_number": stage.get("attempt_number"),
                "deliverable": deliverables.get(stage_id, "Stage result"),
                "gate": gates.get(stage_id, "stage acceptance gate"),
                "last_result_status": result.get("status"),
                "output_path": result.get("output_path"),
                "error": _short(result.get("error"), 320),
            })

        survey_context = context.get("survey") if isinstance(context.get("survey"), dict) else {}
        coverage = survey_context.get("coverage") if isinstance(survey_context.get("coverage"), dict) else {}
        coverage_summary = {
            key: coverage[key] for key in (
                "unique_works", "abstracts", "verified_full_texts", "searches", "pagination_remaining", "saturated",
            ) if key in coverage and isinstance(coverage[key], (int, float, bool))
        }
        source_challenge = topic_context.get("source_challenge") if isinstance(topic_context.get("source_challenge"), dict) else {}
        feasibility = topic_context.get("feasibility_check") if isinstance(topic_context.get("feasibility_check"), dict) else {}
        survey_stage = stage_map.get("survey") or {}

        # New runs carry these objects in their stage context.  The path
        # fallback keeps the view useful for checkpoints written before the
        # full packet was embedded in Composer state.
        context_values = [value for value in context.values() if isinstance(value, dict)]
        program = topic_context.get("research_program") if isinstance(topic_context, dict) else None
        defense = None
        for item in context_values:
            if not isinstance(program, dict) and isinstance(item.get("research_program"), dict):
                program = item["research_program"]
            if isinstance(item.get("argument_defense"), dict):
                defense = item["argument_defense"]
            package = item.get("argument_package")
            if isinstance(package, dict) and isinstance(package.get("argument_defense"), dict):
                defense = package["argument_defense"]

        program_paths = []
        defense_paths = []
        for item in context_values:
            if isinstance(item.get("research_program_path"), str):
                program_paths.append(item["research_program_path"])
            for key in ("research_argument_defense_path", "argument_defense_path"):
                if isinstance(item.get(key), str):
                    defense_paths.append(item[key])
        if not isinstance(program, dict):
            for raw_path in program_paths:
                loaded = self._scoped_json_file(raw_path)
                if not isinstance(loaded, dict):
                    continue
                program = loaded.get("research_program") if isinstance(loaded.get("research_program"), dict) else loaded
                if isinstance(program, dict):
                    break
        if not isinstance(defense, dict):
            for raw_path in defense_paths:
                loaded = self._scoped_json_file(raw_path)
                if not isinstance(loaded, dict):
                    continue
                defense = loaded.get("argument_defense") if isinstance(loaded.get("argument_defense"), dict) else loaded
                if isinstance(defense, dict):
                    break

        return {
            "topic": {
                "title": value("title", "name"),
                "domain": value("domain"),
                "question": value("research_question", "question", limit=1800),
                "phenomenon": value("phenomenon"),
                "mechanism": value("mechanism", "theory_target"),
                "comparison": value("comparison"),
                "measurement": value("measurement"),
                "data_regime": value("data_regime"),
                "disconfirmation_test": value("disconfirmation_test"),
                "resource_plan": value("resource_plan"),
                "scope": value("scope"),
                "why_promising": value("why_promising"),
                "prior_work_ids": topic.get("prior_work_ids") if isinstance(topic.get("prior_work_ids"), list) else [],
                "search_queries": topic.get("search_queries") if isinstance(topic.get("search_queries"), list) else [],
            },
            "selection": {
                "status": topic_context.get("status"),
                "selected_id": topic_context.get("selected_id"),
                "maturity_score": topic_context.get("maturity_score"),
                "rationale": _short(topic_context.get("selection_rationale"), 1200),
                "source_decision": source_challenge.get("decision"),
                "prior_work_risk": source_challenge.get("prior_work_risk"),
                "feasibility": feasibility.get("status"),
            },
            "survey": {
                "current_status": survey_stage.get("status"),
                "current": bool(survey_context.get("survey_current")),
                "last_result_status": survey_context.get("status"),
                "gap_state": survey_context.get("gap_state"),
                "release_status": survey_context.get("release_status"),
                "coverage": coverage_summary,
                "blockers": [_short(item.get("reason") if isinstance(item, dict) else item, 320)
                             for item in (survey_context.get("blockers") or [])[:4]],
            },
            "experiment": {
                "status": (stage_map.get("experiment") or {}).get("status"),
                "comparison": value("comparison"),
                "measurement": value("measurement"),
                "data_regime": value("data_regime"),
                "disconfirmation_test": value("disconfirmation_test"),
                "resource_plan": value("resource_plan"),
                "scope": value("scope"),
            },
            "research_program": self._research_program_view(program),
            "argument_defense": self._argument_defense_view(defense),
            "stage_progress": stage_progress,
        }

    @staticmethod
    def _recent_work(db, organization):
        """Project assignment tasks into a bounded, activity-ordered workboard.

        The roster is a capability catalogue; it is not a work queue. The
        dashboard therefore derives this view from assignment task state and
        the latest assignment artifact instead of presenting every eligible
        role as if it had recently worked.
        """
        organization = organization if isinstance(organization, dict) else {}
        active_assignments = organization.get("active_assignments") or []
        active_task_ids = {
            item.get("task_id") for item in active_assignments
            if isinstance(item, dict) and isinstance(item.get("task_id"), str)
        }
        roster = organization.get("agents") or organization.get("role_pool") or []
        labels = {}
        for item in roster:
            if not isinstance(item, dict):
                continue
            role_id = item.get("id") or item.get("role_id")
            department = item.get("department")
            if not isinstance(role_id, str):
                continue
            role = role_id if "." in role_id else (
                f"{department}.{role_id}" if isinstance(department, str) else role_id
            )
            labels[role] = item.get("label") or item.get("agent") or item.get("role_id") or role

        latest_artifact = {}
        for item in db.get("artifacts") or []:
            logical_id = item.get("logical_id") if isinstance(item, dict) else None
            if not isinstance(logical_id, str):
                continue
            previous = latest_artifact.get(logical_id)
            if previous is None or (
                str(item.get("created_at") or ""), _safe_int(item.get("version"))
            ) >= (
                str(previous.get("created_at") or ""), _safe_int(previous.get("version"))
            ):
                latest_artifact[logical_id] = item

        action_labels = {
            "proposed": "admitted",
            "queued": "queued",
            "running": "working",
            "awaiting_review": "awaiting independent review",
            "completed": "completed",
            "failed": "failed · repair signal",
            "blocked": "blocked · repair signal",
            "paused": "paused",
            "cancelled": "cancelled",
            "stale": "stale",
        }
        work = []
        for task in db.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            if not isinstance(payload.get("assignment_id"), str):
                continue
            role = payload.get("assigned_role") or payload.get("role") or payload.get("agent")
            if not isinstance(role, str) or not role:
                continue
            state = str(task.get("state") or "unknown")
            logical_id = payload.get("assignment_logical_id")
            artifact = latest_artifact.get(logical_id) if isinstance(logical_id, str) else None
            artifact_version = _safe_int((artifact or {}).get("version"))
            if state in ACTIVE_STATES:
                response_status = "awaiting response"
                response_ref = None
            elif artifact is not None and artifact_version > 1:
                response_status = "response recorded"
                response_ref = artifact.get("file_ref")
            elif state in TERMINAL_STATES or state == "result_unknown":
                response_status = "task closed · artifact outside view"
                response_ref = None
            else:
                response_status = "not recorded"
                response_ref = None
            aliases = payload.get("internal_role_aliases")
            if not isinstance(aliases, list):
                aliases = payload.get("internal_roles")
            focus = payload.get("system_contract")
            if not isinstance(focus, str) or not focus:
                focus = next((item for item in aliases or [] if isinstance(item, str)), None)
            if not isinstance(focus, str) or not focus:
                focus = payload.get("execution_kind") or task.get("kind") or "assignment"
            work.append({
                "work_id": task.get("task_id"),
                "task_id": task.get("task_id"),
                "attempt_id": payload.get("attempt_id"),
                "attempt_number": payload.get("attempt_number"),
                "role": role,
                "label": labels.get(role, role),
                "department": payload.get("department") or (role.split(".", 1)[0] if "." in role else None),
                "stage_id": payload.get("stage_id"),
                "kind": task.get("kind") or payload.get("execution_kind"),
                "execution_kind": payload.get("execution_kind"),
                "focus": _short(focus, 180),
                "state": state,
                "status": _display_status(state),
                "action": action_labels.get(state, state.replace("_", " ")),
                "updated_at": task.get("updated_at"),
                "current": task.get("task_id") in active_task_ids,
                "response_status": response_status,
                "response_ref": response_ref,
                "artifact_ref": artifact.get("artifact_ref") if artifact_version > 1 and artifact else None,
                "reviewer": payload.get("reviewer_agent") or payload.get("verifier_agent"),
                "verifier": payload.get("verifier_agent"),
            })

        work.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        current = [item for item in work if item.get("current")]
        rest = [item for item in work if not item.get("current")]
        active_rest = [item for item in rest if item.get("state") in ACTIVE_STATES]
        inactive_rest = [item for item in rest if item.get("state") not in ACTIVE_STATES]
        ordered = current + active_rest + inactive_rest
        return {
            "items": ordered[:MAX_RECENT_WORK],
            "total": len(work),
            "truncated": len(work) > MAX_RECENT_WORK,
        }

    def payload(self):
        live = self._live_checkpoint()
        live_value = live["value"] if live else {}
        db = self._db_records()
        stages = [self._stage_summary(spec, live) for spec in self._stage_specs()]
        current_stage = None
        phase = live_value.get("phase") if isinstance(live_value, dict) else None
        if isinstance(phase, str) and phase:
            prefix = phase.split(":", 1)[0]
            if any(stage["id"] == prefix for stage in stages):
                current_stage = prefix
        if current_stage is None:
            running = next((stage for stage in stages if stage["status"] == "running"), None)
            current_stage = running["id"] if running else None
        completed = sum(stage["status"] == "completed" for stage in stages)
        activity = self._activity_from_department(live_value.get("department_activity"))
        activity.extend(self._activity_from_events(db["events"]))
        activity.sort(key=lambda item: item.get("ts") or "", reverse=True)
        checkpoint_items = []
        for record in self._checkpoint_files():
            try:
                size = record["path"].stat().st_size
            except OSError:
                size = 0
            checkpoint_items.append({
                "ref": _ref(record["root_key"], record["relative"]),
                "root_key": record["root_key"], "path": record["relative"].as_posix(),
                "name": record["path"].name, "kind": "checkpoint", "size": size,
                "updated_at": datetime.fromtimestamp(record["modified"], timezone.utc).isoformat(),
                "status": _display_status(record["value"].get("status")),
            })
        checkpoint_items.extend(item for item in db["artifacts"] if item["kind"] == "checkpoint")
        checkpoint_items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        usage = live_value.get("usage") if isinstance(live_value.get("usage"), dict) else {}
        deadline_at = live_value.get("deadline_at_epoch")
        workflow_policy = self.workflow.get("time_policy") if isinstance(self.workflow.get("time_policy"), dict) else {}
        deadline_seconds = live_value.get("deadline_seconds") or workflow_policy.get("hard_seconds")
        remaining = live_value.get("remaining_seconds")
        if remaining is None and isinstance(deadline_at, (int, float)):
            remaining = max(0, deadline_at - datetime.now(timezone.utc).timestamp())
        file_data = self._files()
        log_stream = self._log_stream(file_data)
        log_stream.extend(activity)
        log_stream.sort(key=lambda item: item.get("ts") or "", reverse=True)
        files = file_data["items"]
        artifacts = db["artifacts"]
        artifact_refs = {item.get("file_ref") for item in artifacts if item.get("file_ref")}
        files = [item for item in files if item.get("ref") not in artifact_refs]
        status = _display_status(live_value.get("status")) if live else "unknown"
        if status == "unknown" and db["counts"]["active_tasks"]:
            status = "running"
        objective = self.workflow.get("objective") if isinstance(self.workflow, dict) else None
        organization_raw = live_value.get("organization") if isinstance(live_value.get("organization"), dict) else None
        specialists = self._specialists(stages, db, organization_raw)
        execution = self._execution_view(stages, db, organization_raw)
        model_calls = self._model_calls(db, live_value)
        live_provider_calls = model_calls.get("live_items", [])
        execution.setdefault("provider", {})["live_model_calls"] = len(live_provider_calls)
        execution["provider"]["running_tasks"] = sum(
            item.get("state") in {"running", "started"} for item in live_provider_calls
        )
        execution["provider"]["queued_tasks"] = sum(
            item.get("state") == "queued" for item in live_provider_calls
        )
        total_provider_capacity = execution["provider"].get("durable_window_capacity")
        if type(total_provider_capacity) is not int:
            total_provider_capacity = execution["provider"].get("configured_capacity")
        execution["provider"]["within_capacity"] = (
            type(total_provider_capacity) is not int
            or len(live_provider_calls) <= total_provider_capacity
        )
        pool_limits = execution.get("provider", {}).get("pool_limits", {})
        execution.setdefault("provider", {})["pools"] = {
            name: {
                "max_concurrent": details["max_concurrent"],
                "running": sum(1 for item in model_calls.get("live_items", [])
                                if item.get("provider_pool") == name),
                "within_capacity": sum(1 for item in model_calls.get("live_items", [])
                                        if item.get("provider_pool") == name) <= details["max_concurrent"],
            }
            for name, details in pool_limits.items()
        }
        recent_work = self._recent_work(db, organization_raw)
        organization = self._organization_view(organization_raw)
        return {
            "schema_version": f"dashboard-snapshot-{APP_VERSION}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project": {
                "name": self.root.name, "path": str(self.root),
                "objective": _short(objective, 420),
                "workflow_path": str(self.workflow_path) if self.workflow_path else None,
                "roots": [{"key": key, "path": str(path)} for key, path in self.roots.items()],
            },
            "live": {
                "status": status, "phase": phase, "current_stage": current_stage,
                "source_ref": _ref(live["root_key"], live["relative"]) if live else None,
                "source_updated_at": datetime.fromtimestamp(live["modified"], timezone.utc).isoformat() if live else None,
                "elapsed_seconds": _safe_float(live_value.get("elapsed_seconds"), 0),
                "remaining_seconds": _safe_float(remaining), "deadline_seconds": _safe_float(deadline_seconds),
                "deadline_at_epoch": deadline_at, "last_phase": live_value.get("last_phase"),
                "stop_reason": live_value.get("stop_reason"),
                "next_actions": _bounded_notices(live_value.get("next_actions")),
                "blockers": _bounded_notices(live_value.get("blockers")),
                "usage": usage,
            },
            "pipeline": {
                "stages": stages, "completed": completed, "total": len(stages),
                "completion_ratio": completed / len(stages) if stages else 0,
            },
            "research": self._research_view(live_value, stages),
            "counts": db["counts"],
            "specialists": specialists,
            "specialists_active": sum(item.get("engaged") is True for item in specialists),
            "specialists_roster": len(specialists),
            "execution": execution,
            "model_calls": model_calls,
            "recent_work": recent_work["items"],
            "recent_work_meta": {
                "total": recent_work["total"],
                "displayed": len(recent_work["items"]),
                "truncated": recent_work["truncated"],
            },
            "activity": activity[:MAX_ACTIVITY],
            "logs": log_stream[:MAX_ACTIVITY],
            "artifacts": artifacts,
            "checkpoints": checkpoint_items[:MAX_ARTIFACTS],
            "files": files,
            "files_meta": {"total": file_data["total"], "truncated": file_data["truncated"],
                           "roots": file_data["roots"]},
            "structure": {"roots": file_data["roots"], "directories": file_data["directories"],
                          "skipped_directories": sorted(SKIP_DIRECTORIES)},
            "tasks": db["tasks"],
            "attempts": db["attempts"],
            "resources": {"usage": usage, "pools": db["pools"], "windows": db["windows"]},
            "integrity": {"databases": db["integrity"], "read_only": True},
            "runtime": {"host": platform.node(), "platform": platform.platform(),
                         "python": sys.version.split()[0], "processes": self._processes()},
            "organization": organization,
        }

    def resolve_file(self, file_ref):
        if not isinstance(file_ref, str) or "::" not in file_ref:
            raise ValueError("file reference must use root::relative/path syntax")
        root_key, relative_text = file_ref.split("::", 1)
        base = self.roots.get(root_key)
        if base is None or not relative_text or "\0" in relative_text:
            raise ValueError("unknown or malformed file reference")
        relative = Path(relative_text)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ValueError("file reference escapes its project root")
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
            raise FileNotFoundError("file is outside the configured project roots or does not exist")
        return candidate, root_key, candidate.relative_to(base.resolve())

    def file_payload(self, file_ref):
        path, root_key, relative = self.resolve_file(file_ref)
        stat = path.stat()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        is_text = path.suffix.lower() in TEXT_SUFFIXES or media_type.startswith("text/") or media_type == "application/json"
        object_preview = None
        # Content-addressed artifact objects intentionally have no extension.
        # Classify UTF-8 objects here so the inspector can show assignment and
        # result records instead of labelling every object as binary.
        if not is_text and path.parent.name == "sha256" and stat.st_size <= MAX_FILE_PREVIEW_BYTES:
            try:
                object_preview = path.read_bytes()
                if b"\x00" not in object_preview:
                    object_preview.decode("utf-8")
                    is_text = True
                    try:
                        json.loads(object_preview)
                        media_type = "application/json"
                    except (TypeError, ValueError, UnicodeDecodeError):
                        media_type = "text/plain"
            except (OSError, UnicodeError):
                object_preview = None
        content = ""
        truncated = False
        if is_text and stat.st_size <= MAX_FILE_PREVIEW_BYTES:
            content = (object_preview.decode("utf-8") if object_preview is not None
                       else path.read_text(encoding="utf-8", errors="replace"))
        elif is_text:
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_PREVIEW_BYTES).decode("utf-8", errors="replace")
            truncated = True
        else:
            content = f"Binary file · {media_type} · {_safe_int(stat.st_size)} bytes"
        return {
            "ref": _ref(root_key, relative), "root_key": root_key, "path": relative.as_posix(),
            "name": path.name, "media_type": media_type, "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "text": content, "is_text": is_text, "truncated": truncated,
        }


class DashboardService:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).expanduser().resolve()
        if not self.project_dir.is_dir():
            raise ValueError(f"dashboard project directory does not exist: {self.project_dir}")
        if self.project_dir == Path("/"):
            raise ValueError("dashboard refuses to manage the filesystem root")
        self._owned_processes = {}
        self._action_lock = Lock()

    def _resolve_project(self, project_ref=None):
        if project_ref in (None, "", "."):
            return self.project_dir
        if not isinstance(project_ref, str) or "\0" in project_ref:
            raise ValueError("project reference must be a relative path")
        relative = Path(project_ref)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("project reference must stay inside the dashboard workspace")
        candidate = (self.project_dir / relative).resolve()
        if not candidate.is_relative_to(self.project_dir) or not candidate.is_dir():
            raise FileNotFoundError("project is outside the dashboard workspace or does not exist")
        return candidate

    def _workflow(self, project_dir):
        snapshot = DashboardSnapshot(project_dir)
        if snapshot.workflow_path is None:
            raise ValueError("project has no Composer workflow.json")
        workflow = _read_json(snapshot.workflow_path)
        if not isinstance(workflow, dict):
            raise ValueError("project workflow.json is not a JSON object")
        return snapshot.workflow_path, workflow

    def snapshot(self, project_ref=None):
        return DashboardSnapshot(self._resolve_project(project_ref)).payload()

    @staticmethod
    def _project_stage_summary(workflow, progress):
        specs = [item for item in workflow.get("stages", [])
                 if isinstance(item, dict) and isinstance(item.get("id"), str)]
        stage_records = progress.get("stages") if isinstance(progress, dict) else {}
        if not isinstance(stage_records, dict):
            stage_records = {}
        order = {stage_id: index for index, stage_id in enumerate(STAGE_ORDER)}
        summaries = []
        for spec in sorted(specs, key=lambda item: (order.get(item["id"], len(order)), item["id"])):
            stage_id = spec["id"]
            raw = stage_records.get(stage_id)
            raw = raw if isinstance(raw, dict) else {}
            status = _display_status(raw.get("status"))
            if status == "unknown" and (raw.get("active_agents") or raw.get("attempt_id") or raw.get("attempt_number")):
                status = "running"
            summaries.append({
                "id": stage_id,
                "label": STAGE_LABELS.get(stage_id, stage_id.replace("_", " ").title()),
                "status": status,
                "current": status == "running",
                "attempts": len(raw.get("attempts")) if isinstance(raw.get("attempts"), list) else _safe_int(raw.get("attempt_count"), 0),
                "attempt_number": raw.get("attempt_number"),
            })
        return summaries

    def _project_record(self, candidate):
        workflow_path, workflow = self._workflow(candidate)
        ref = "." if candidate == self.project_dir else candidate.relative_to(self.project_dir).as_posix()
        project_id = workflow.get("project_id")
        project_root = Path(project_id).expanduser().resolve() if isinstance(project_id, str) else None
        progress_path = project_root / "output" / "progress.json" if project_root else None
        state_path = project_root / "state" / "control.sqlite" if project_root else None
        progress = _read_json(progress_path) if progress_path and progress_path.is_file() else {}
        progress = progress if isinstance(progress, dict) else {}
        running_processes = self._composer_processes(workflow_path)
        progress_status = _display_status(progress.get("status"))
        status = "running" if running_processes else ("draft" if not progress else progress_status)
        if status == "running" and not running_processes:
            status = "stale"

        stages = self._project_stage_summary(workflow, progress)
        phase = progress.get("phase")
        current_stage = None
        if isinstance(phase, str) and phase:
            phase_stage = phase.split(":", 1)[0]
            if any(item["id"] == phase_stage for item in stages):
                current_stage = phase_stage
        if current_stage is None:
            current_stage = next((item["id"] for item in stages if item["current"]), None)
        completed = sum(item["status"] == "completed" for item in stages)

        updated_path = progress_path if progress_path and progress_path.is_file() else workflow_path
        try:
            updated_at = datetime.fromtimestamp(updated_path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            updated_at = None
        deadline_at = progress.get("deadline_at_epoch")
        remaining = _safe_float(progress.get("remaining_seconds"))
        if remaining is None and isinstance(deadline_at, (int, float)):
            remaining = max(0, deadline_at - datetime.now(timezone.utc).timestamp())
        blockers = progress.get("blockers")
        blocker_count = len(blockers) if isinstance(blockers, list) else (1 if blockers else 0)
        source = "live process" if running_processes else ("checkpoint" if progress else "workflow")
        return {
            "ref": ref,
            "name": candidate.name,
            "path": str(candidate),
            "workflow_path": str(workflow_path),
            "workflow_id": workflow.get("id"),
            "objective": _short(workflow.get("objective"), 300),
            "status": status,
            "pid": running_processes[0]["pid"] if running_processes else None,
            "initialized": bool(state_path and state_path.is_file()),
            "source": source,
            "last_updated": updated_at,
            "current_stage": current_stage,
            "phase": phase,
            "completed_stages": completed,
            "total_stages": len(stages),
            "progress_ratio": completed / len(stages) if stages else 0,
            "elapsed_seconds": _safe_float(progress.get("elapsed_seconds"), 0),
            "remaining_seconds": remaining,
            "deadline_at_epoch": deadline_at,
            "run_id": progress.get("run_id"),
            "blocker_count": blocker_count,
            "stages": stages,
        }

    def _project_candidates(self):
        candidates = [self.project_dir]
        missions = self.project_dir / "missions"
        if missions.is_dir():
            candidates.extend(child for child in sorted(missions.iterdir())
                              if child.is_dir() and not child.name.startswith("."))
        return candidates

    def projects(self):
        candidates = self._project_candidates()
        projects = []
        for candidate in candidates[:MAX_PROJECTS]:
            try:
                projects.append(self._project_record(candidate))
            except (OSError, ValueError):
                continue
        return {"workspace": str(self.project_dir), "current": ".", "projects": projects,
                "bounded": len(candidates) > MAX_PROJECTS}

    def workspace(self):
        listing = self.projects()
        projects = listing["projects"]
        status_counts = {status: sum(item.get("status") == status for item in projects)
                         for status in ("running", "stale", "draft", "completed", "failed", "blocked", "cancelled")}
        active_runs = [item for item in projects if item.get("status") == "running"]
        active_stages = sum(1 for item in active_runs if item.get("current_stage"))
        repository = Path(__file__).resolve().parents[2]
        recent_projects = sorted(
            (item for item in projects if item.get("last_updated")),
            key=lambda item: item["last_updated"], reverse=True,
        )[:MAX_WORKSPACE_RECENT_PROJECTS]
        return {
            "schema_version": f"dashboard-workspace-{APP_VERSION}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": {
                "name": repository.name,
                "path": str(repository),
                "managed_root": str(self.project_dir),
                "current_project": listing.get("current"),
            },
            "summary": {
                "total_projects": len(projects),
                "running_projects": status_counts["running"],
                "stale_projects": status_counts["stale"],
                "draft_projects": status_counts["draft"],
                "completed_projects": status_counts["completed"],
                "failed_projects": status_counts["failed"],
                "blocked_projects": status_counts["blocked"],
                "active_stages": active_stages,
                "processes": sum(1 for item in projects if item.get("pid")),
            },
            "projects": projects,
            "active_runs": active_runs,
            "recent_projects": recent_projects,
            "bounded": listing.get("bounded", False),
            "controls": {"local_actions": ["create_project", "start_composer"]},
        }

    @staticmethod
    def _composer_processes(workflow_path):
        markers = {str(workflow_path)}
        try:
            markers.add(workflow_path.relative_to(Path.cwd().resolve()).as_posix())
        except ValueError:
            pass
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,command="], check=False,
                capture_output=True, text=True, timeout=0.7,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        processes = []
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(.*)", line)
            if not match:
                continue
            command = match.group(2)
            if "run-composer" not in command or not any(marker in command for marker in markers):
                continue
            processes.append({"pid": int(match.group(1)), "command": _short(command, 240)})
        return processes

    def _validate_composer_workflow(self, workflow_path, workflow, project_dir):
        from scisaurus.runtime.composer import validate_workflow
        validated = validate_workflow(workflow)
        project_id = Path(validated["project_id"]).expanduser().resolve()
        if not project_id.is_relative_to(project_dir):
            raise ValueError("workflow project_id must stay inside its managed project")
        if not workflow_path.resolve().is_relative_to(project_dir):
            raise ValueError("workflow must stay inside its managed project")
        return validated, project_id

    def start_composer(self, project_ref=None, *, resume=False):
        project_dir = self._resolve_project(project_ref)
        workflow_path, workflow = self._workflow(project_dir)
        workflow, project_id = self._validate_composer_workflow(workflow_path, workflow, project_dir)
        with self._action_lock:
            existing = self._composer_processes(workflow_path)
            if existing:
                return {"status": "already_running", "project": project_dir.name,
                        "workflow_path": str(workflow_path), "processes": existing}
            state_path = project_id / "state" / "control.sqlite"
            if resume and not state_path.is_file():
                raise ValueError("resume requires an initialized Composer project")
            if not resume and state_path.is_file():
                raise ValueError("project already has Composer state; choose Resume")
            repository = Path(__file__).resolve().parents[2]
            command = [sys.executable, "-m", "scisaurus.cli", "run-composer",
                       "--workflow", str(workflow_path)]
            if resume:
                command.append("--resume")
            process = subprocess.Popen(
                command, cwd=str(repository), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            self._owned_processes[str(workflow_path)] = process
            return {"status": "started", "project": project_dir.name,
                    "workflow_path": str(workflow_path), "pid": process.pid,
                    "resume": resume, "command": command}

    def create_project(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("create_project payload must be an object")
        slug = payload.get("slug") or payload.get("name")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,47}", slug):
            raise ValueError("project slug must match [a-z][a-z0-9-]{1,47}")
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip() or len(objective.strip()) > 8_000:
            raise ValueError("project objective must be 1 to 8000 characters")
        template_ref = payload.get("template", ".")
        template_dir = self._resolve_project(template_ref)
        template_path, template = self._workflow(template_dir)
        if not isinstance(template.get("stages"), list):
            raise ValueError("template workflow has no stage list")
        try:
            hard_seconds = int(payload.get("hard_seconds", template.get("time_policy", {}).get("hard_seconds", 0)))
        except (TypeError, ValueError):
            raise ValueError("hard_seconds must be an integer")
        if not MIN_PROJECT_HARD_SECONDS <= hard_seconds <= MAX_PROJECT_HARD_SECONDS:
            raise ValueError(f"hard_seconds must be between {MIN_PROJECT_HARD_SECONDS} and {MAX_PROJECT_HARD_SECONDS}")
        workflow = json.loads(json.dumps(template))
        target = self.project_dir / "missions" / slug
        if target.exists():
            raise FileExistsError(f"project already exists: {target}")
        workflow["id"] = f"mission-{slug}"[:64]
        workflow["revision"] = 1
        workflow["project_id"] = str((target / "composer").resolve())
        workflow["objective"] = objective.strip()
        policy = workflow.setdefault("time_policy", {})
        policy["hard_seconds"] = hard_seconds
        policy["target_seconds"] = min(int(policy.get("target_seconds", hard_seconds)), hard_seconds)
        policy["first_result_seconds"] = min(int(policy.get("first_result_seconds", policy["target_seconds"])), policy["target_seconds"])
        workflow["topic_history_path"] = str((target / "topic-history.json").resolve())
        for stage in workflow["stages"]:
            if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
                raise ValueError("template contains an invalid stage")
            stage["project_dir"] = str((target / "projects" / stage["id"]).resolve())
            stage["reuse_completed"] = False
            stage["reuse_output_path"] = None
        with self._action_lock:
            target.mkdir(parents=True, exist_ok=False)
            created_target = True
            temporary = None
            try:
                (target / "composer").mkdir()
                for stage in workflow["stages"]:
                    Path(stage["project_dir"]).mkdir(parents=True, exist_ok=False)
                from scisaurus.runtime.composer import validate_workflow
                workflow = validate_workflow(workflow)
                workflow_path = target / "workflow.json"
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target, prefix=".workflow-", suffix=".tmp", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(workflow, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temporary, workflow_path)
                created_target = False
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink(missing_ok=True)
                if created_target and target.exists():
                    shutil.rmtree(target)
        ref = target.relative_to(self.project_dir).as_posix()
        result = {"status": "created", "project": ref, "path": str(target),
                  "workflow_path": str(workflow_path), "workflow_id": workflow["id"]}
        if payload.get("start_now") is True:
            result["start"] = self.start_composer(ref, resume=False)
        return result

    def action(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("action payload must be an object")
        action = payload.get("action")
        if action == "create_project":
            return self.create_project(payload)
        if action == "start_composer":
            return self.start_composer(payload.get("project", "."), resume=payload.get("resume") is True)
        raise ValueError("unsupported dashboard action")

    def file_payload(self, file_ref, project_ref=None):
        return DashboardSnapshot(self._resolve_project(project_ref)).file_payload(file_ref)

    def raw_file(self, file_ref, project_ref=None):
        path, _, _ = DashboardSnapshot(self._resolve_project(project_ref)).resolve_file(file_ref)
        stat = path.stat()
        if stat.st_size > MAX_RAW_FILE_BYTES:
            raise ValueError(f"file exceeds the {MAX_RAW_FILE_BYTES} byte preview limit")
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.name


class DashboardHandler(BaseHTTPRequestHandler):
    server: "DashboardServer"

    def _json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _error(self, status, message):
        self._json_response({"error": message}, status)

    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/":
                return self._static("index.html")
            if parsed.path.startswith("/assets/"):
                return self._static(unquote(parsed.path.removeprefix("/assets/")))
            query = parse_qs(parsed.query)
            if parsed.path == "/api/snapshot":
                project_ref = query.get("project", [None])[0]
                return self._json_response(self.server.service.snapshot(project_ref))
            if parsed.path == "/api/workspace":
                return self._json_response(self.server.service.workspace())
            if parsed.path == "/api/projects":
                return self._json_response(self.server.service.projects())
            if parsed.path == "/api/file":
                ref = query.get("ref", [""])[0]
                project_ref = query.get("project", [None])[0]
                return self._json_response(self.server.service.file_payload(ref, project_ref))
            if parsed.path == "/api/raw":
                ref = query.get("ref", [""])[0]
                project_ref = query.get("project", [None])[0]
                body, media_type, name = self.server.service.raw_file(ref, project_ref)
                self.send_response(200)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(name)}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._error(404, "route not found")
        except (BrokenPipeError, ConnectionResetError):
            return
        except FileNotFoundError as exc:
            self._error(404, str(exc))
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._error(400, str(exc))

    def do_POST(self):  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path != "/api/actions":
            return self._error(404, "route not found")
        if self.client_address[0] not in {"127.0.0.1", "::1", "localhost"}:
            return self._error(403, "dashboard actions are limited to a local client")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "invalid Content-Length")
        if length <= 0 or length > MAX_ACTION_BYTES:
            return self._error(413, f"action body must be between 1 and {MAX_ACTION_BYTES} bytes")
        try:
            payload = json.loads(self.rfile.read(length))
            result = self.server.service.action(payload)
            return self._json_response(result, 201 if result.get("status") == "created" else 200)
        except (BrokenPipeError, ConnectionResetError):
            return
        except FileExistsError as exc:
            self._error(409, str(exc))
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self._error(400, str(exc))

    def _static(self, relative):
        base = Path(__file__).parent / "static"
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
            return self._error(404, "asset not found")
        body = candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type.startswith("text/") else ""))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, DashboardHandler)


def run_dashboard(project_dir, *, host="127.0.0.1", port=0, open_browser=False):
    """Serve the dashboard until interrupted and return the bound URL."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    service = DashboardService(project_dir)
    server = DashboardServer((host, port), service)
    url_host = "localhost" if host in {"127.0.0.1", "::1"} else host
    url = f"http://{url_host}:{server.server_port}/"
    print(f"Sci-saurus dashboard: {url}", flush=True)
    if open_browser:
        import webbrowser
        Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
