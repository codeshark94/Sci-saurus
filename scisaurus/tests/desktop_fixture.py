"""Provider-free worker for exercising the desktop process lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scisaurus.core.events import ControlStore


class DesktopFixtureRunner:
    def __init__(self, workflow, *, resume=False, on_progress=None, **_):
        self.root = Path(workflow["project_id"])
        self.control = ControlStore(self.root)
        self.resume = resume
        self.progress = on_progress

    def close(self):
        self.control.close()

    def run(self):
        output = self.root / "output"
        output.mkdir(exist_ok=True)
        path = output / "progress.json"
        prior = json.loads(path.read_text()) if path.exists() else {}
        deadline = prior.get("deadline_at_epoch", time.time() + 3600)
        value = {"status": "running", "phase": "survey:running", "run_id": "desktop-fixture",
                 "deadline_at_epoch": deadline, "remaining_seconds": deadline - time.time(),
                 "stages": {}, "resume_count": prior.get("resume_count", 0) + int(self.resume)}
        path.write_text(json.dumps(value))
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"], start_new_session=True)
        (output / "fixture-worker.pid").write_text(str(worker.pid))
        (output / "fixture-child.pid").write_text(str(os.getpid()))
        if self.progress:
            self.progress(value)
        try:
            time.sleep(3600)
        finally:
            worker.terminate()
            worker.wait(timeout=5)
