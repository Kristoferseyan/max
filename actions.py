import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PENDING = STATE / "pending.json"
STATE.mkdir(exist_ok=True)

_cfg = ROOT / "config.json"
CONFIG = json.loads((_cfg if _cfg.exists() else ROOT / "config.example.json").read_text())
NAME = CONFIG.get("name") or "the user"
SESSIONS = CONFIG.get("sessions", {})

HOST = os.environ.get("MAX_HOST") or CONFIG.get("host", "myserver")
CONTROL_PATH = os.path.expanduser("~/.ssh/cm-max-%r@%h-%p")
SSH_OPTS = ["-o", f"ControlPath={CONTROL_PATH}", "-o", "ControlMaster=auto",
            "-o", "ControlPersist=1h", "-o", "ConnectTimeout=20"]

BLOCKED_SESSIONS = set(CONFIG.get("blocked_sessions", []))
BLOCKED_CONTAINERS = set(CONFIG.get("blocked_containers", []))

READ_COMMANDS = {
    "cat", "head", "tail", "ls", "grep", "egrep", "wc", "df", "du", "free", "uptime",
    "ps", "find", "stat", "date", "whoami", "hostname", "sensors", "lsblk", "env",
    "printenv", "which", "file", "nproc", "top", "journalctl", "pwd", "realpath",
}
DOCKER_READ = {"ps", "logs", "inspect", "stats", "images", "top", "port", "version", "info"}
SYSTEMCTL_READ = {"status", "list-units", "list-timers", "is-active", "is-enabled", "show", "cat"}
GIT_READ = {"log", "status", "diff", "show", "branch", "remote", "rev-parse", "describe", "shortlog", "blame"}
FIND_BAD = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}

DESTRUCTIVE = re.compile(
    r"\b(rm|rmdir|dd|mkfs|shred|truncate|kill|pkill|killall|reboot|shutdown|poweroff|"
    r"prune|down|drop|delete|destroy|wipe|reset|format)\b|docker\s+(rm|rmi|stop|kill)|--force|-f\b"
)


class Refused(Exception):
    pass


def ssh(remote_cmd, timeout=40, fresh=False):
    opts = ["-o", "ControlPath=none", "-o", "ConnectTimeout=30"] if fresh else SSH_OPTS
    p = subprocess.run(["ssh", *opts, HOST, remote_cmd], capture_output=True, text=True, timeout=timeout)
    out = (p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")).strip()
    return p.returncode, out


def master_alive():
    p = subprocess.run(["ssh", *SSH_OPTS, "-O", "check", HOST], capture_output=True, text=True)
    return p.returncode == 0


def open_master():
    p = subprocess.run(["ssh", *SSH_OPTS, "-fN", HOST], capture_output=True, text=True, timeout=90)
    return p.returncode == 0


def clip(text, limit=6000):
    text = text.strip()
    if len(text) <= limit:
        return text
    return "[...trimmed...]\n" + text[-limit:]


def check_window(target):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+:\d+(\.\d+)?", target or ""):
        raise Refused(f"bad window target {target!r}, use session:window like work:5")
    if target.split(":")[0] in BLOCKED_SESSIONS:
        raise Refused("That tmux session is off limits for Max.")


def check_container(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
        raise Refused(f"bad container name {name!r}")
    if name in BLOCKED_CONTAINERS:
        raise Refused("That container is off limits for Max.")


def check_read_command(tokens):
    if not tokens:
        raise Refused("empty command")
    cmd = tokens[0]
    if cmd == "docker":
        if len(tokens) < 2 or tokens[1] not in DOCKER_READ:
            raise Refused("only read-only docker subcommands are allowed here")
        if tokens[1] == "stats" and "--no-stream" not in tokens:
            tokens.insert(2, "--no-stream")
    elif cmd == "systemctl":
        sub = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if sub not in SYSTEMCTL_READ:
            raise Refused("only read-only systemctl subcommands are allowed here")
    elif cmd == "git":
        rest = tokens[1:]
        while rest and rest[0] == "-C" and len(rest) > 1:
            rest = rest[2:]
        if not rest or rest[0] not in GIT_READ or any(t.startswith("--output") for t in rest):
            raise Refused("only read-only git subcommands are allowed here")
    elif cmd == "find":
        if FIND_BAD & set(tokens):
            raise Refused("find with -exec/-delete is not read-only")
    elif cmd == "top":
        if "-b" not in tokens:
            tokens[1:1] = ["-b", "-n", "1"]
    elif cmd not in READ_COMMANDS:
        raise Refused(f"{cmd} is not on the read-only list; propose it as an action instead")
    return tokens


def read_command(command, container=None):
    tokens = check_read_command(shlex.split(command))
    if container:
        check_container(container)
        tokens = ["docker", "exec", container, *tokens]
    return clip(ssh(shlex.join(tokens))[1])


def list_windows():
    fmt = "#S:#I\t#W\t#{pane_current_command}\t#{?window_active,active,}\t#{session_attached}"
    rc, out = ssh(f"tmux list-windows -a -F {shlex.quote(fmt)}")
    lines = [l for l in out.splitlines() if l.split(":")[0] not in BLOCKED_SESSIONS]
    return "target\tname\tcommand\tactive\tattached\n" + "\n".join(lines)


def read_window(target, lines=80):
    check_window(target)
    lines = max(10, min(int(lines or 80), 400))
    rc, out = ssh(f"tmux capture-pane -p -J -t {shlex.quote(target)} -S -{lines}")
    return clip("\n".join(l.rstrip() for l in out.splitlines()).strip())


def overview():
    cmd = ("echo '== uptime'; uptime; echo '== memory'; free -h; echo '== disk'; df -h -x tmpfs -x overlay -x squashfs; "
           "echo '== containers'; docker ps -a --format '{{.Names}}\t{{.Status}}'; "
           "echo '== failed units'; systemctl --failed --no-legend; systemctl --user --failed --no-legend")
    return clip(ssh(cmd)[1])


def claude_activity(where="host", messages=12):
    if where != "host":
        check_container(where)
    find = "ls -t $HOME/.claude*/projects/*/*.jsonl 2>/dev/null | head -1"
    if where == "host":
        rc, newest = ssh(find)
        prefix = ""
    else:
        rc, newest = ssh(f"docker exec {shlex.quote(where)} sh -c {shlex.quote(find)}")
        prefix = f"docker exec {shlex.quote(where)} "
    newest = newest.strip().splitlines()[0] if newest.strip() else ""
    if not newest.endswith(".jsonl"):
        return f"No Claude sessions found in {where}."
    rc, raw = ssh(f"{prefix}tail -n 600 {shlex.quote(newest)}", timeout=60)
    picked = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        role = e.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = content if isinstance(content, str) else ""
        text = text.strip()
        if text and not text.startswith("<"):
            picked.append(f"[{e.get('timestamp', '')[:16]}] {role}: {text[:600]}")
    head = f"Session file: {newest}\ncwd: {e.get('cwd', '?') if picked else '?'}\n"
    return clip(head + "\n".join(picked[-int(messages):]))


def propose(kind, summary, target=None, text=None, press_enter=True, command=None, container=None):
    action = {"kind": kind, "summary": summary, "created": time.time()}
    if kind == "type_into_window":
        check_window(target)
        if not text:
            raise Refused("text is required")
        action.update(target=target, text=text, press_enter=bool(press_enter))
    elif kind == "restart_container":
        check_container(container)
        action.update(container=container)
    elif kind == "run_command":
        if not command:
            raise Refused("command is required")
        if container:
            check_container(container)
        action.update(command=command, container=container)
    else:
        raise Refused(f"unknown action kind {kind!r}")
    action["destructive"] = needs_watch(action)
    PENDING.write_text(json.dumps(action, indent=1))
    extra = f" This one is destructive, so {NAME} also has to approve the SSH key." if action["destructive"] else ""
    return (f"Proposed, not run. Ask {NAME} out loud to confirm in one short sentence and stop there. "
            "Max runs it only after a yes." + extra)


def needs_watch(action):
    if action["kind"] == "run_command":
        return bool(DESTRUCTIVE.search(action["command"])) or "sudo" in action["command"]
    if action["kind"] == "type_into_window":
        return bool(DESTRUCTIVE.search(action["text"]))
    return False


def load_pending():
    try:
        a = json.loads(PENDING.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - a.get("created", 0) > 600:
        clear_pending()
        return None
    return a


def clear_pending():
    PENDING.unlink(missing_ok=True)


def execute(action):
    fresh = action.get("destructive", False)
    kind = action["kind"]
    if kind == "type_into_window":
        t = shlex.quote(action["target"])
        cmd = f"tmux send-keys -t {t} -l {shlex.quote(action['text'])}"
        if action["press_enter"]:
            cmd += f" && sleep 0.3 && tmux send-keys -t {t} Enter"
        rc, out = ssh(cmd, fresh=fresh)
    elif kind == "restart_container":
        rc, out = ssh(f"docker restart {shlex.quote(action['container'])}", timeout=120, fresh=fresh)
    else:
        cmd = action["command"]
        if action.get("container"):
            cmd = f"docker exec {shlex.quote(action['container'])} sh -lc {shlex.quote(cmd)}"
        rc, out = ssh(cmd, timeout=300, fresh=fresh)
    return rc, clip(out or "(no output)", 3000)
