import json
import sys
import traceback

import actions
import mac_tools

NAME = actions.NAME
SESSIONS = "; ".join(f"{k}: {v}" for k, v in actions.SESSIONS.items())

TOOLS = [
    {
        "name": "server_overview",
        "description": "Server health: uptime/load, memory, disks, every container's status, failed systemd units.",
        "inputSchema": {"type": "object", "properties": {}},
        "run": lambda a: actions.overview(),
    },
    {
        "name": "list_windows",
        "description": "Every tmux window on the server (target, name, running command, active, attached). " + SESSIONS,
        "inputSchema": {"type": "object", "properties": {}},
        "run": lambda a: actions.list_windows(),
    },
    {
        "name": "read_window",
        "description": f"What is on screen in one tmux window right now, plus scrollback. Use this to see what {NAME} is working on, "
                       "follow a build, or read what a Claude session in that window is doing.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "session:window, e.g. work:5"},
            "lines": {"type": "integer", "description": "scrollback lines, default 80, max 400"},
        }, "required": ["target"]},
        "run": lambda a: actions.read_window(a["target"], a.get("lines", 80)),
    },
    {
        "name": "claude_activity",
        "description": "Recent prompts and replies from the newest Claude Code session on the host or inside a container. "
                       "Good for 'what was I doing at work yesterday'.",
        "inputSchema": {"type": "object", "properties": {
            "where": {"type": "string", "description": "'host' or a container name such as work-box"},
            "messages": {"type": "integer", "description": "how many recent messages, default 12"},
        }, "required": ["where"]},
        "run": lambda a: actions.claude_activity(a["where"], a.get("messages", 12)),
    },
    {
        "name": "read_command",
        "description": "Run one read-only command on the server, or inside a container if `container` is set. "
                       "Allowed: cat head tail ls grep wc df du free uptime ps find stat date sensors lsblk journalctl top, "
                       "docker ps/logs/inspect/stats/images, systemctl status/list-units/is-active/show, git log/status/diff/show/branch. "
                       "No pipes or redirects (arguments are passed literally). Anything else must go through propose_action.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "container": {"type": "string", "description": "optional container to docker exec into"},
        }, "required": ["command"]},
        "run": lambda a: actions.read_command(a["command"], a.get("container")),
    },
    {
        "name": "propose_action",
        "description": f"Propose a change. It is NOT run: Max asks {NAME}, and runs it only after a yes. "
                       "Kinds: type_into_window (type text into a tmux window, e.g. give an instruction to the Claude running there), "
                       "restart_container, run_command (any shell command on the host, or in `container`; sudo works after the SSH key is approved). "
                       f"Propose at most one action per reply, then ask {NAME}.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["type_into_window", "restart_container", "run_command"]},
            "summary": {"type": "string", "description": "one short spoken sentence of what will happen"},
            "target": {"type": "string", "description": "type_into_window: session:window"},
            "text": {"type": "string", "description": "type_into_window: text to type"},
            "press_enter": {"type": "boolean", "description": "type_into_window: press Enter after, default true"},
            "command": {"type": "string", "description": "run_command: the shell command"},
            "container": {"type": "string", "description": "restart_container target, or run_command inside this container"},
        }, "required": ["kind", "summary"]},
        "run": lambda a: actions.propose(**{k: v for k, v in a.items() if k in
                                           ("kind", "summary", "target", "text", "press_enter", "command", "container")}),
    },
]
WHEN = {"type": "string", "description": "local time, ISO format like 2026-10-14T15:30"}
TOOLS += [
    {
        "name": "list_reminders",
        "description": "Apple Reminders (synced to the iPhone). Open ones by default.",
        "inputSchema": {"type": "object", "properties": {
            "list_name": {"type": "string"}, "include_completed": {"type": "boolean"}}},
        "run": lambda a: mac_tools.list_reminders(a.get("list_name"), a.get("include_completed", False)),
    },
    {
        "name": "create_reminder",
        "description": "Create an Apple Reminder. With `due` it also alerts at that time on the phone and Mac.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"}, "due": WHEN, "notes": {"type": "string"},
            "list_name": {"type": "string", "description": "default list if omitted"}}, "required": ["title"]},
        "run": lambda a: mac_tools.create_reminder(a["title"], a.get("due"), a.get("notes"), a.get("list_name")),
    },
    {
        "name": "complete_reminder",
        "description": "Mark an open reminder done, matched by part of its title.",
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        "run": lambda a: mac_tools.complete_reminder(a["title"]),
    },
    {
        "name": "list_events",
        "description": "Calendar events from all calendars. Defaults to today.",
        "inputSchema": {"type": "object", "properties": {
            "start": WHEN, "days": {"type": "integer", "description": "how many days from start, default 1"}}},
        "run": lambda a: mac_tools.list_events(a.get("start"), a.get("days", 1)),
    },
    {
        "name": "create_event",
        "description": "Add a calendar event. Give `end` or `minutes` (default 60). Alert 10 minutes before unless alert_minutes says otherwise.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"}, "start": WHEN, "end": WHEN, "minutes": {"type": "integer"},
            "calendar": {"type": "string"}, "location": {"type": "string"}, "notes": {"type": "string"},
            "alert_minutes": {"type": "integer"}}, "required": ["title", "start"]},
        "run": lambda a: mac_tools.create_event(a["title"], a["start"], a.get("end"), a.get("minutes", 60),
                                                a.get("calendar"), a.get("location"), a.get("notes"),
                                                a.get("alert_minutes", 10)),
    },
    {
        "name": "set_alarm",
        "description": "Alarm or timer. Max rings and speaks the label on the Mac at that time, and a reminder alerts the phone as backup.",
        "inputSchema": {"type": "object", "properties": {"at": WHEN, "label": {"type": "string"}}, "required": ["at"]},
        "run": lambda a: mac_tools.set_alarm(a["at"], a.get("label", "Alarm")),
    },
    {
        "name": "list_alarms",
        "description": "Alarms Max has set that haven't rung yet.",
        "inputSchema": {"type": "object", "properties": {}},
        "run": lambda a: mac_tools.list_alarms(),
    },
    {
        "name": "cancel_alarm",
        "description": "Cancel alarms whose label contains this text.",
        "inputSchema": {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]},
        "run": lambda a: mac_tools.cancel_alarm(a["label"]),
    },
]
BY_NAME = {t["name"]: t for t in TOOLS}


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method, id_ = req.get("method"), req.get("id")
    if id_ is None:
        return
    if method == "initialize":
        reply(id_, {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "max", "version": "0.1"}})
    elif method == "tools/list":
        reply(id_, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
    elif method == "tools/call":
        p = req.get("params", {})
        tool = BY_NAME.get(p.get("name"))
        if not tool:
            reply(id_, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
            return
        try:
            text, err = tool["run"](p.get("arguments") or {}), False
        except actions.Refused as e:
            text, err = f"Refused: {e}", True
        except Exception as e:
            text, err = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}", True
        reply(id_, {"content": [{"type": "text", "text": text or "(empty)"}], "isError": err})
    elif method == "ping":
        reply(id_, {})
    else:
        reply(id_, error={"code": -32601, "message": f"no method {method}"})


for line in sys.stdin:
    line = line.strip()
    if line:
        try:
            handle(json.loads(line))
        except ValueError:
            pass
