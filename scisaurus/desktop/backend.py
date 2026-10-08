"""Bundled HTTP backend; scientific workers use the selected repository runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Thread

from scisaurus.dashboard.server import DashboardUnixServer, DashboardService


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--parent-pipe", action="store_true")
    args = parser.parse_args(argv)
    repository = Path(args.repository).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    python = repository / ".venv/bin/python"
    if not (repository / "scisaurus/cli.py").is_file() or not python.is_file():
        raise ValueError("Select a Sci-whale repository with its .venv runtime installed.")
    if not workspace.is_dir() or not workspace.is_relative_to(repository):
        raise ValueError("The research workspace must be a directory inside the repository.")
    service = DashboardService(workspace, repository=repository, runtime_python=python)
    transport = tempfile.TemporaryDirectory(prefix="scisaurus-")
    socket_path = str(Path(transport.name) / "backend.sock")
    server = DashboardUnixServer(socket_path, service)
    print(json.dumps({"url": "sciwhale://localhost/", "socket_path": socket_path,
                      "repository": str(repository), "workspace": str(workspace)}), flush=True)

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    if args.parent_pipe:
        def watch_parent():
            while os.read(sys.stdin.fileno(), 4096):
                pass
            os.kill(os.getpid(), signal.SIGTERM)
        Thread(target=watch_parent, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        transport.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
