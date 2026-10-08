"""Build a standalone console backend for the host architecture."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

desktop = Path(__file__).resolve().parent
repository = desktop.parent
shutil.copyfile(repository / "scisaurus/dashboard/static/favicon.svg", desktop / "ui/favicon.svg")
python = desktop / ".venv-build/bin/python"
if not python.exists():
    subprocess.run([sys.executable, "-m", "venv", str(desktop / ".venv-build")], check=True)
subprocess.run([str(python), "-m", "pip", "install", "-r", str(desktop / "requirements-build.txt")], check=True)
rustc = os.environ.get("RUSTC", str(Path.home() / ".cargo/bin/rustc"))
triple = subprocess.check_output([rustc, "--print", "host-tuple"], text=True).strip()
binaries = desktop / "src-tauri/binaries"
subprocess.run([
    str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
    "--name", f"sciwhale-backend-{triple}", "--distpath", str(binaries),
    "--workpath", str(desktop / ".build/backend"), "--specpath", str(desktop / ".build"),
    "--paths", str(repository),
    "--add-data", f"{repository / 'scisaurus/dashboard/static'}:scisaurus/dashboard/static",
    "--collect-submodules", "scisaurus.runtime", "--exclude-module", "scisaurus.tests",
    str(desktop / "backend_entry.py"),
], cwd=repository, check=True)
