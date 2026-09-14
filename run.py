"""Launches the FastAPI server, Celery worker, Celery beat, and the
LiveKit agent worker together for local development, each in its own
console window — a crash or manual close of one never touches the
others. Unlike a Procfile-style process manager (which by default kills
every process in the group the moment any single one exits), these are
fully independent OS processes here.

ngrok is deliberately not included — it needs its own visible terminal
to show the tunnel URL, so keep running it separately.

Always resolves python.exe/celery.exe from this project's own .venv,
regardless of which Python interpreter is used to launch this script
(this machine has more than one Python on PATH — system, MSYS2/Git Bash,
and this project's venv — so relying on sys.executable/PATH previously
picked whichever one happened to run this file, not necessarily the one
with celery/fastapi/etc. actually installed).

Run with (or without) the project's own interpreter — it no longer
matters which python launches this file:
    .venv\\Scripts\\python.exe run.py

Windows only (local dev tool).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_SCRIPTS = ROOT / ".venv" / "Scripts"
PYTHON = str(VENV_SCRIPTS / "python.exe")
CELERY = str(VENV_SCRIPTS / "celery.exe")

PROCESSES = [
    ("FastAPI server", [PYTHON, "-m", "app.main"]),
    ("Celery worker", [CELERY, "-A", "app.workers.celery_app", "worker", "--loglevel=info", "--pool=solo"]),
    ("Celery beat", [CELERY, "-A", "app.workers.celery_app", "beat", "--loglevel=info"]),
    ("LiveKit agent", [PYTHON, "-m", "app.interviews.workers.livekit_agent", "start"]),
]

if __name__ == "__main__":
    if not Path(PYTHON).exists():
        sys.exit(
            f"Could not find {PYTHON}. Run this from the project root, and make "
            f"sure the virtualenv was created at .venv (python -m venv .venv)."
        )

    for name, cmd in PROCESSES:
        print(f"Starting {name} in its own window...")
        subprocess.Popen(cmd, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)

    print("\nAll started, each in its own window. Closing this window does not stop them.")
    print("Remember to run 'ngrok http 8000' separately if you need a public URL.")
    print("To stop a service, close its own window or use Task Manager.")
