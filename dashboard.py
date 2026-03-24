"""
BetterWiser Briefing Agent — Web Dashboard

A browser-based interface for triggering and monitoring briefing runs.
Non-technical users can use this instead of the command line.

Usage:
    python dashboard.py
    Open: http://localhost:5000

Requires: flask (pip install flask)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

load_dotenv()

app = Flask(__name__)
RUNS_DIR = Path("runs")
RUNS_DIR.mkdir(exist_ok=True)

# Track running processes: run_id -> Popen
_processes: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()

# Allowlisted track values
_VALID_TRACKS = {"A", "B", "C"}
# run_id must be YYYY-MM_run_YYYYMMDDTHHMMSS (only safe filesystem chars)
_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}_run_\d{8}T\d{6}(?:_\w+)?$")


def _safe_run_id(run_id: str) -> str:
    """Validate run_id against allowlist pattern. Abort 400 if invalid."""
    if not _RUN_ID_RE.match(run_id):
        abort(400, description=f"Invalid run_id: {run_id!r}")
    return run_id


def _safe_track(track: str) -> str:
    """Validate track is one of A/B/C. Abort 400 if invalid."""
    if track not in _VALID_TRACKS:
        abort(400, description=f"Invalid track: {track!r}")
    return track


def _safe_run_dir(run_id: str) -> Path:
    """Resolve run directory and confirm it stays inside RUNS_DIR."""
    run_dir = (RUNS_DIR / run_id).resolve()
    if not str(run_dir).startswith(str(RUNS_DIR.resolve())):
        abort(400, description="Invalid run path")
    return run_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_run_status(run_dir: Path) -> dict:
    """Read a single run directory and return its status dict."""
    run_id = run_dir.name
    delivery_dir = run_dir / "delivery"
    receipts_file = run_dir / "delivery_receipts.json"

    parts = run_id.split("_run_")
    month = parts[0] if len(parts) >= 2 else run_id
    ts_str = parts[1] if len(parts) >= 2 else ""
    started_at = ""
    if ts_str and len(ts_str) >= 15:
        try:
            started_at = datetime.strptime(ts_str[:15], "%Y%m%dT%H%M%S").strftime(
                "%d %b %Y %H:%M"
            )
        except ValueError:
            started_at = ts_str

    with _lock:
        is_running = run_id in _processes and _processes[run_id].poll() is None

    receipts = []
    if receipts_file.exists():
        try:
            receipts = json.loads(receipts_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    tracks = []
    if delivery_dir.exists():
        for track in ["A", "B", "C"]:
            html_file = delivery_dir / f"track_{track}.html"
            if html_file.exists():
                held = delivered = False
                dry_run = True
                for r in receipts:
                    if r.get("track") == track:
                        held = r.get("held_for_review", False)
                        delivered = r.get("delivered", False)
                        dry_run = r.get("dry_run", True)
                        break
                if held:
                    badge = "held"
                elif delivered:
                    badge = "sent"
                else:
                    badge = "saved"
                tracks.append({"track": track, "badge": badge})

    return {
        "run_id": run_id,
        "month": month,
        "started_at": started_at,
        "running": is_running,
        "tracks": tracks,
        "has_log": (run_dir / "run.log").exists(),
    }


def _get_all_runs() -> list[dict]:
    runs = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True):
        if run_dir.is_dir() and run_dir.name != ".gitkeep":
            try:
                runs.append(_get_run_status(run_dir))
            except Exception:
                pass
    return runs


def _launch_agent(run_id: str, cli_args: list[str]) -> None:
    """Run the agent as a subprocess. Called from a background thread."""
    cmd = [sys.executable, "-m", "src.orchestrator"] + cli_args
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).parent),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with _lock:
            _processes[run_id] = proc
        proc.wait()
    except Exception as exc:
        flag = RUNS_DIR / run_id / "dashboard_error.txt"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(str(exc), encoding="utf-8")
    finally:
        with _lock:
            _processes.pop(run_id, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    runs = _get_all_runs()
    has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_azure = all(
        os.getenv(v)
        for v in ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_USER_EMAIL"]
    )
    any_running = any(r["running"] for r in runs)
    return render_template(
        "dashboard.html",
        runs=runs,
        has_api_key=has_api_key,
        has_azure=has_azure,
        any_running=any_running,
        default_month=datetime.now().strftime("%Y-%m"),
    )


@app.route("/run/start", methods=["POST"])
def start_run():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return redirect(url_for("index") + "?error=no_api_key")

    month = request.form.get("month") or datetime.now().strftime("%Y-%m")
    # Validate month format YYYY-MM with valid month range
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        return redirect(url_for("index") + "?error=invalid_month")
    # Validate tracks: only allow A, B, C
    raw_tracks = request.form.getlist("tracks") or ["A", "B", "C"]
    tracks = [t for t in raw_tracks if t in _VALID_TRACKS]
    if not tracks:
        tracks = ["A", "B", "C"]
    mode = request.form.get("mode", "dry_run")

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_id = f"{month}_run_{ts}"

    cli_args = ["--month", month]
    for t in tracks:
        cli_args += ["--track", t]
    if mode == "send":
        cli_args.append("--send")
    else:
        cli_args.append("--dry-run")

    thread = threading.Thread(target=_launch_agent, args=(run_id, cli_args), daemon=True)
    thread.start()
    time.sleep(0.8)  # let the process create its log file

    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/run/<run_id>")
def run_detail(run_id: str):
    _safe_run_id(run_id)
    run_dir = _safe_run_dir(run_id)
    with _lock:
        is_running = run_id in _processes and _processes[run_id].poll() is None

    receipts = []
    receipts_file = run_dir / "delivery_receipts.json"
    if receipts_file.exists():
        try:
            receipts = json.loads(receipts_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    tracks_available = []
    delivery_dir = run_dir / "delivery"
    if delivery_dir.exists():
        for t in ["A", "B", "C"]:
            if (delivery_dir / f"track_{t}.html").exists():
                tracks_available.append(t)

    track_names = {"A": "Vendor & Customer", "B": "Global AI Policy", "C": "Thought Leadership"}
    error = (run_dir / "dashboard_error.txt").read_text() if (run_dir / "dashboard_error.txt").exists() else None

    return render_template(
        "run_detail.html",
        run_id=run_id,
        is_running=is_running,
        tracks_available=tracks_available,
        track_names=track_names,
        receipts=receipts,
        has_log=(run_dir / "run.log").exists(),
        error=error,
    )


@app.route("/run/<run_id>/logs")
def stream_logs(run_id: str):
    """Server-Sent Events endpoint — streams run.log in real time."""
    _safe_run_id(run_id)
    run_dir = _safe_run_dir(run_id)
    log_file = run_dir / "run.log"

    def generate():
        # Wait up to 6 seconds for log file to appear
        for _ in range(12):
            if log_file.exists():
                break
            time.sleep(0.5)
            yield "data: Waiting for run to start...\n\n"

        if not log_file.exists():
            yield f"data: ERROR — log file not found for {run_id}\n\n"
            yield "event: done\ndata: done\n\n"
            return

        with open(log_file, encoding="utf-8", errors="replace") as fh:
            while True:
                line = fh.readline()
                if line:
                    line = line.rstrip()
                    if not line:
                        continue
                    # Parse structured JSON log if possible
                    try:
                        entry = json.loads(line)
                        level = entry.get("level", "INFO")
                        msg = entry.get("message", line)
                        extra = {k: v for k, v in entry.items() if k not in ("level", "message", "timestamp", "logger")}
                        extra_str = "  " + "  ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
                        yield f"data: [{level}] {msg}{extra_str}\n\n"
                    except (json.JSONDecodeError, AttributeError):
                        yield f"data: {line}\n\n"
                else:
                    with _lock:
                        still_running = run_id in _processes and _processes[run_id].poll() is None
                    if not still_running:
                        yield "data: ── Run finished ──\n\n"
                        yield "event: done\ndata: done\n\n"
                        return
                    time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/run/<run_id>/briefing/<track>")
def view_briefing(run_id: str, track: str):
    _safe_run_id(run_id)
    _safe_track(track)
    run_dir = _safe_run_dir(run_id)
    html_file = run_dir / "delivery" / f"track_{track}.html"
    if not html_file.exists():
        return f"<p>Briefing track_{track}.html not found in {run_id}.</p>", 404
    # Final path check: confirm file is inside the expected delivery dir
    resolved = html_file.resolve()
    expected_prefix = str((run_dir / "delivery").resolve())
    if not str(resolved).startswith(expected_prefix):
        abort(400)
    return send_file(str(resolved), mimetype="text/html")


@app.route("/api/run/<run_id>/status")
def api_status(run_id: str):
    _safe_run_id(run_id)
    with _lock:
        is_running = run_id in _processes and _processes[run_id].poll() is None
    run_dir = _safe_run_dir(run_id)
    tracks_done = []
    delivery_dir = run_dir / "delivery"
    if delivery_dir.exists():
        tracks_done = [t for t in ["A", "B", "C"] if (delivery_dir / f"track_{t}.html").exists()]
    return jsonify({"running": is_running, "tracks_done": tracks_done})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Bind to localhost ONLY — the dashboard has no authentication and exposes
    # run controls and briefing content. Never expose to 0.0.0.0 in production.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    print()
    print("  BetterWiser Briefing Agent Dashboard")
    print("  ─────────────────────────────────────")
    print(f"  Open in your browser: http://{host}:{port}")
    print("  ⚠  Accessible on this machine only (localhost binding).")
    print()
    app.run(host=host, port=port, debug=False, threaded=True)
