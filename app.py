"""
Claude Code Desktop Companion - Backend API
Reads local Claude Code CLI data and serves it to the web UI.
"""
import asyncio
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Claude Code Desktop")

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
SETTINGS_LOCAL_FILE = CLAUDE_DIR / "settings.local.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"

# ── helpers ──────────────────────────────────────────────────────────

def _scan_jsonl_files() -> dict[str, Path]:
    """Scan all project dirs and return {sessionId: filepath} mapping."""
    mapping = {}
    if PROJECTS_DIR.exists():
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                mapping[f.stem] = f
    return mapping


def _parse_first_prompt(filepath: Path) -> str:
    """Extract first user prompt from a JSONL session file."""
    try:
        for line_no, line in enumerate(open(filepath, encoding="utf-8", errors="replace")):
            if line_no > 100:
                break
            obj = json.loads(line)
            if obj.get("type") == "user" and obj.get("message", {}).get("role") == "user":
                content = obj["message"].get("content", "")
                if isinstance(content, str):
                    return content[:200]
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            return c.get("text", "")[:200]
    except Exception:
        pass
    return ""


def _list_all_sessions() -> list[dict]:
    """Scan all project dirs — driven by actual JSONL files on disk, not stale index."""
    entries = []
    seen = set()
    all_files = _scan_jsonl_files()

    for sid, fpath in sorted(all_files.items(),
                              key=lambda x: x[1].stat().st_mtime, reverse=True):
        stat = fpath.stat()
        first_prompt = _parse_first_prompt(fpath)
        entries.append({
            "sessionId": sid,
            "firstPrompt": first_prompt,
            "summary": first_prompt[:80] if first_prompt else sid[:8],
            "messageCount": 0,
            "created": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "projectPath": str(fpath.parent),
            "projectKey": fpath.parent.name,
            "fileSize": stat.st_size,
        })

    return entries


def _read_conversation(session_id: str, project: str | None = None,
                       limit: int = 500) -> list[dict]:
    # 1) try given project
    if project:
        fpath = PROJECTS_DIR / project / f"{session_id}.jsonl"
        if fpath.exists():
            return _parse_jsonl(fpath, limit)

    # 2) search all projects
    all_files = _scan_jsonl_files()
    fpath = all_files.get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")

    return _parse_jsonl(fpath, limit)


def _parse_jsonl(fpath: Path, limit: int) -> list[dict]:
    messages = []
    with open(fpath, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if len(messages) >= limit:
                break
            try:
                obj = json.loads(line)
                messages.append(obj)
            except json.JSONDecodeError:
                continue
    return messages


def _active_session() -> dict | None:
    if not SESSIONS_DIR.exists():
        return None
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(data)
        except Exception:
            pass
    # return most recently updated active session
    sessions.sort(key=lambda s: s.get("updatedAt", 0), reverse=True)
    return sessions[0] if sessions else None


def _read_settings() -> dict:
    settings = {}
    for f in (SETTINGS_FILE, SETTINGS_LOCAL_FILE):
        if f.exists():
            try:
                settings.update(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return settings


def _write_settings_env(updates: dict) -> dict:
    """Write model settings to settings.local.json env block."""
    current = {}
    if SETTINGS_LOCAL_FILE.exists():
        try:
            current = json.loads(SETTINGS_LOCAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if "env" not in current:
        current["env"] = {}
    current["env"].update(updates)

    SETTINGS_LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_LOCAL_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


# ── API routes ────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions(search: str = Query(default=""), project: str | None = None):
    entries = _list_all_sessions()
    if search:
        q = search.lower()
        entries = [e for e in entries
                   if q in (e.get("firstPrompt", "") + e.get("summary", "")).lower()]
    # active
    active = _active_session()
    active_id = active.get("sessionId") if active else None
    return {
        "sessions": entries,
        "activeSessionId": active_id,
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, project: str | None = None, limit: int = 500):
    messages = _read_conversation(session_id, project, limit)
    return {"sessionId": session_id, "messages": messages, "count": len(messages)}


@app.get("/api/active")
def get_active():
    active = _active_session()
    if not active:
        return {"active": None}
    # also return recent messages
    sid = active.get("sessionId")
    messages = []
    if sid:
        try:
            messages = _read_conversation(sid, limit=100)
        except HTTPException:
            pass
    return {"active": active, "messages": messages}


@app.get("/api/settings")
def get_settings():
    settings = _read_settings()
    env = settings.get("env", {})
    return {
        "model": env.get("ANTHROPIC_MODEL", "unknown"),
        "opusModel": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        "sonnetModel": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "haikuModel": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
        "baseUrl": env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "maxTokens": env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", ""),
        "allEnv": env,
    }


class ModelUpdate(BaseModel):
    model: str
    key: str = "ANTHROPIC_MODEL"


@app.post("/api/settings/model")
def set_model(update: ModelUpdate):
    _write_settings_env({update.key: update.model})
    return {"ok": True, "model": update.model}


@app.get("/api/history")
def get_history(limit: int = 50):
    items = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in reversed(lines[-limit * 2:]):
            try:
                obj = json.loads(line)
                items.append(obj)
            except Exception:
                continue
            if len(items) >= limit:
                break
    return {"history": items}


# health / poll endpoint for frontend to check if active session changed
@app.get("/api/poll")
def poll():
    active = _active_session()
    # also get file sizes for each session to detect changes
    session_sizes = {}
    all_files = _scan_jsonl_files()
    for sid, fpath in all_files.items():
        session_sizes[sid] = fpath.stat().st_size
    return {
        "activeSessionId": active.get("sessionId") if active else None,
        "activePid": active.get("pid") if active else None,
        "activeStatus": active.get("status") if active else None,
        "sessionSizes": session_sizes,
        "timestamp": int(time.time() * 1000),
    }


# track background send jobs
_send_jobs: dict[str, dict] = {}


class SendMessage(BaseModel):
    message: str


@app.post("/api/sessions/{session_id}/send")
def send_message(session_id: str, body: SendMessage):
    """Send a message to a session by spawning claude CLI."""
    # find the session file
    all_files = _scan_jsonl_files()
    fpath = all_files.get(session_id)
    if not fpath:
        raise HTTPException(status_code=404, detail="Session not found")

    # resolve working directory from project key
    proj_key = fpath.parent.name
    # C--Users-PETER → C:\Users\PETER
    cwd = proj_key.replace("--", ":\\", 1).replace("-", "\\")
    if not Path(cwd).exists():
        cwd = str(Path.home())

    # read env from settings
    settings = _read_settings()
    env = os.environ.copy()
    env.update(settings.get("env", {}))

    # ensure npm global bin is in PATH (where claude CLI lives)
    npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
    if npm_bin not in env.get("PATH", ""):
        env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")

    # on Windows, npm installs .cmd wrappers; use claude.cmd
    claude_exe = str(Path(npm_bin) / "claude.cmd")
    if not Path(claude_exe).exists():
        claude_exe = str(Path(npm_bin) / "claude")
    if not Path(claude_exe).exists():
        claude_exe = "claude"  # fallback to PATH search

    job_id = f"{session_id[:8]}-{int(time.time())}"
    _send_jobs[job_id] = {"status": "running", "sessionId": session_id, "started": time.time()}

    def _run():
        try:
            proc = subprocess.run(
                [claude_exe, "-p", body.message, "--resume", session_id],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            _send_jobs[job_id]["status"] = "done"
            _send_jobs[job_id]["exitCode"] = proc.returncode
            _send_jobs[job_id]["stderr"] = proc.stderr[:500] if proc.stderr else ""
        except subprocess.TimeoutExpired:
            _send_jobs[job_id]["status"] = "timeout"
        except FileNotFoundError:
            _send_jobs[job_id]["status"] = "error"
            _send_jobs[job_id]["error"] = "claude CLI not found in PATH"
        except Exception as e:
            _send_jobs[job_id]["status"] = "error"
            _send_jobs[job_id]["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "jobId": job_id, "message": "Message sent, processing in background"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _send_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Managed Claude Code process (PTY-based) ──────────────────────────

class ManagedClaude:
    """Spawns Claude Code via Windows PTY — full terminal interaction mirrored to Web UI."""

    def __init__(self):
        self.pty: object = None  # winpty.PtyProcess
        self.clients: list[WebSocket] = []
        self.output_queue: queue.Queue = queue.Queue()
        self.running = False
        self.session_id: str | None = None
        self._lock = threading.Lock()
        self._read_thread = None

    def _get_claude_exe(self) -> str:
        npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
        for name in ("claude.cmd", "claude"):
            p = str(Path(npm_bin) / name)
            if Path(p).exists():
                return p
        return "claude"

    def _get_env(self) -> dict:
        env = os.environ.copy()
        settings = _read_settings()
        env.update(settings.get("env", {}))
        npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
        if npm_bin not in env.get("PATH", ""):
            env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
        return env

    def start(self, session_id: str | None = None, cwd: str | None = None):
        with self._lock:
            if self.running:
                self.stop()
            try:
                from winpty import PtyProcess
            except ImportError:
                # fallback to subprocess
                self._start_subprocess(session_id, cwd)
                return

            exe = self._get_claude_exe()
            env = self._get_env()
            work_dir = cwd or str(Path.home())

            args = [exe, "--dangerously-skip-permissions"]
            if session_id:
                args = [exe, "--resume", session_id]

            self.pty = PtyProcess.spawn(
                args,
                cwd=work_dir,
                env=env,
                dimensions=(120, 40),
            )
            self.running = True
            self.session_id = session_id
            self._read_thread = threading.Thread(target=self._pty_read_loop, daemon=True)
            self._read_thread.start()
            threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def _start_subprocess(self, session_id: str | None, cwd: str | None):
        """Fallback: plain subprocess (no PTY — some prompts may not show)."""
        exe = self._get_claude_exe()
        env = self._get_env()
        work_dir = cwd or str(Path.home())
        args = [exe, "--dangerously-skip-permissions"]
        if session_id:
            args = [exe, "--resume", session_id]

        self.proc = subprocess.Popen(
            args, cwd=work_dir, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.running = True
        self.session_id = session_id
        threading.Thread(target=self._read_stream, args=(self.proc.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=self._read_stream, args=(self.proc.stderr, "stderr"), daemon=True).start()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def _pty_read_loop(self):
        try:
            while self.running and self.pty and self.pty.isalive():
                try:
                    data = self.pty.read(4096)
                    if data:
                        for line in data.splitlines():
                            if line.strip():
                                self.output_queue.put({"type": "stdout", "text": line, "ts": time.time()})
                except Exception:
                    break
        except Exception:
            pass

    proc: subprocess.Popen | None = None  # fallback ref

    def _read_stream(self, stream, name: str):
        try:
            for line in iter(stream.readline, ""):
                if not self.running:
                    break
                line = line.rstrip("\n")
                if line:
                    self.output_queue.put({"type": name, "text": line, "ts": time.time()})
        except (ValueError, OSError):
            pass

    def _broadcast_loop(self):
        while self.running:
            try:
                msg = self.output_queue.get(timeout=0.3)
                text = msg.get("text", "")
                # detect ANY interactive prompt
                prompt_type = None
                lower = text.lower()
                if any(kw in lower for kw in ("permission", "approve", "allow", "deny", "tool call", "proceed", "continue?")):
                    prompt_type = "permission"
                elif re.search(r'(\(y/n\)|\[y/n\]|y/n\?|yes/no)', lower):
                    prompt_type = "confirm"
                elif re.search(r'(select|choose|pick|option|choice).*[:\?]', lower) or re.search(r'^\s*[\d]+[\.\)]\s', text):
                    prompt_type = "choice"
                elif "?" in text and len(text) < 300:
                    prompt_type = "question"
                msg["promptType"] = prompt_type

                disconnected = []
                for ws in self.clients:
                    try:
                        asyncio.run_coroutine_threadsafe(ws.send_json(msg), asyncio.get_event_loop())
                    except Exception:
                        disconnected.append(ws)
                for ws in disconnected:
                    self.clients.remove(ws)
            except queue.Empty:
                pass
            except Exception:
                break

    def send_input(self, text: str):
        """Send text to process stdin."""
        try:
            if self.pty and self.running:
                self.pty.write(text + "\r\n")
            elif hasattr(self, 'proc') and self.proc and self.proc.stdin and self.running:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

    def stop(self):
        with self._lock:
            self.running = False
            try:
                if self.pty:
                    self.pty.close()
                    self.pty = None
            except Exception:
                pass
            try:
                if hasattr(self, 'proc') and self.proc:
                    self.proc.stdin.close()
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                    self.proc = None
            except Exception:
                pass
            self.clients.clear()

    @property
    def is_running(self):
        if self.pty:
            return self.running and self.pty.isalive()
        if hasattr(self, 'proc') and self.proc:
            return self.running and self.proc.poll() is None
        return False


managed = ManagedClaude()


# ── WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    managed.clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("action") == "start":
                managed.start(session_id=msg.get("sessionId"), cwd=msg.get("cwd"))
                await ws.send_json({"type": "status", "text": "Claude Code started", "running": True})
            elif msg.get("action") == "stop":
                managed.stop()
                await ws.send_json({"type": "status", "text": "Claude Code stopped", "running": False})
            elif msg.get("action") == "input":
                managed.send_input(msg.get("text", ""))
            elif msg.get("action") == "approve":
                # send "y" or approval response to stdin
                managed.send_input(msg.get("response", "y"))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in managed.clients:
            managed.clients.remove(ws)


@app.get("/api/managed/status")
def managed_status():
    return {"running": managed.is_running, "sessionId": managed.session_id, "clientCount": len(managed.clients)}


class ManagedAction(BaseModel):
    action: str  # start, stop, input, approve
    sessionId: str | None = None
    cwd: str | None = None
    text: str = ""
    response: str = "y"


@app.post("/api/managed/action")
def managed_action(body: ManagedAction):
    if body.action == "start":
        managed.start(session_id=body.sessionId, cwd=body.cwd)
    elif body.action == "stop":
        managed.stop()
    elif body.action == "input":
        managed.send_input(body.text)
    elif body.action == "approve":
        managed.send_input(body.response)
    return {"ok": True, "running": managed.is_running}


# ── static & spa fallback ─────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def index():
    return FileResponse(str(static_dir / "index.html"))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9020, log_level="info")
