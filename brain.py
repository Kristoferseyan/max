import datetime
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from actions import CONFIG, NAME, ROOT, SESSIONS, STATE

MODEL = os.environ.get("MAX_MODEL", "sonnet")
DAY_STARTS_HOUR = 4
LOG = STATE / "log.jsonl"
SESSION = STATE / "session.json"
JOURNAL = ROOT / "journal"
JOURNAL.mkdir(exist_ok=True)

JOURNAL_PROMPT = """End of day. This reply is written to a file, not spoken, so markdown is fine.
Write your journal for {day}, for your future self who starts fresh tomorrow and only gets this note.
Cover, per area that came up ({areas}):
what {name} worked on, what you found, actions you took and how they turned out, decisions they made,
and open loops to follow up tomorrow. Facts only, no chit-chat, under 400 words. No tools needed."""


def render_prompt():
    files = CONFIG.get("background_files", [])
    background = (f"Background on {NAME}'s setup is in {' and '.join(files)}. Read them only when the question needs it."
                  if files else "")
    sessions = "; ".join(f"{k} ({v})" for k, v in SESSIONS.items()) or "none configured"
    text = (ROOT / "prompt.md").read_text()
    for key, val in {"name": NAME, "server": CONFIG.get("server_desc", "server"), "sessions": sessions,
                     "background": background}.items():
        text = text.replace("{" + key + "}", val)
    return text


def mcp_config():
    own = ROOT / "mcp.json"
    if own.exists():
        return own
    out = STATE / "mcp.json"
    out.write_text(json.dumps({"mcpServers": {"max": {"command": sys.executable,
                                                         "args": [str(ROOT / "tools_server.py")]}}}))
    return out


def today():
    return (datetime.datetime.now() - datetime.timedelta(hours=DAY_STARTS_HOUR)).date().isoformat()


class Brain:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.s = self._load()

    def _load(self):
        try:
            return json.loads(SESSION.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self):
        SESSION.write_text(json.dumps(self.s))

    def _prompt_file(self):
        out = STATE / "prompt-today.md"
        text = render_prompt()
        notes = sorted(JOURNAL.glob("*.md"))
        if notes:
            text += f"\n\nYour journal from your last day ({notes[-1].stem}). Use it for continuity; don't recite it:\n\n"
            text += notes[-1].read_text()
        out.write_text(text)
        return out

    def _start(self):
        cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", MODEL, "--setting-sources", "project",
               "--strict-mcp-config", "--mcp-config", str(mcp_config()),
               "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob", "mcp__max"]
        if self.s.get("id") and self.s.get("turns"):
            cmd += ["--resume", self.s["id"], "--append-system-prompt-file", str(self._prompt_file())]
        else:
            self.s = {"id": str(uuid.uuid4()), "day": today(), "turns": 0, "prompt": str(self._prompt_file())}
            self._save()
            cmd += ["--session-id", self.s["id"], "--append-system-prompt-file", self.s["prompt"]]
        err = open(STATE / "claude.err", "a")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                     text=True, bufsize=1)

    def warm(self):
        with self.lock:
            self._rollover()
            if not self._alive():
                self._start()

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _turn(self, text, on_text=None, on_tool=None):
        if not self._alive():
            self._start()
        proc = self.proc
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            self.proc = None
            return []
        said = []
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block["text"].strip():
                        said.append(block["text"])
                        if on_text:
                            on_text(block["text"])
                    elif block.get("type") == "tool_use" and on_tool:
                        on_tool(block.get("name", ""), block.get("input", {}))
            elif ev.get("type") == "result":
                if ev.get("is_error"):
                    self._log({"error": ev.get("result")})
                break
        else:
            self._log({"error": f"claude exited with {proc.poll()}"})
            if self.proc is proc:
                self.proc = None
        self.s["turns"] = self.s.get("turns", 0) + 1
        self._save()
        return said

    def ask(self, text, on_text, on_tool):
        with self.lock:
            self._rollover()
            self._log({"you": text, "session": self.s.get("id")})
            t0 = time.time()
            said = self._turn(f"[{datetime.datetime.now():%a %b %d %Y, %I:%M %p}] {text}", on_text, on_tool)
            if not said:
                on_text("Sorry, Claude didn't answer. Check the log.")
            self._log({"max": " ".join(said), "claude_s": round(time.time() - t0, 1)})

    def maybe_rollover(self):
        if self.lock.acquire(blocking=False):
            try:
                self._rollover()
            finally:
                self.lock.release()

    def _rollover(self):
        if self.s.get("id") and self.s.get("day") != today():
            self._end_session()

    def _end_session(self):
        if self.s.get("turns"):
            day = self.s["day"]
            areas = ", ".join(CONFIG.get("journal_areas", ["work", "the server", "personal projects"]))
            notes = self._turn(JOURNAL_PROMPT.format(day=day, name=NAME, areas=areas))
            if notes:
                path = JOURNAL / f"{day}.md"
                head = "" if path.exists() else f"# Max journal, {day}\n\n"
                with open(path, "a") as f:
                    f.write(head + f"## Session ended {time.strftime('%Y-%m-%d %H:%M')}\n\n" + "\n\n".join(notes).strip() + "\n\n")
                self._log({"journal": day})
        self.close()
        self.s = {}
        SESSION.unlink(missing_ok=True)

    def reset(self):
        with self.lock:
            self._end_session()

    def stop(self):
        self.close()

    def close(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def _log(self, entry):
        entry["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
