import datetime
import json
import threading
import time

import EventKit
from Foundation import NSDate, NSDateComponents

from actions import NAME, STATE, Refused

ALARMS = STATE / "alarms.json"
_store = None


def store():
    global _store
    if _store is None:
        _store = EventKit.EKEventStore.alloc().init()
    return _store


def parse_when(text):
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise Refused(f"time must be ISO local time like 2026-10-14T15:30, got {text!r}")


def nsdate(dt):
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def pydate(nsd):
    return datetime.datetime.fromtimestamp(nsd.timeIntervalSince1970()) if nsd else None


def components(dt):
    c = NSDateComponents.alloc().init()
    c.setYear_(dt.year)
    c.setMonth_(dt.month)
    c.setDay_(dt.day)
    c.setHour_(dt.hour)
    c.setMinute_(dt.minute)
    return c


def say_time(dt):
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def find_calendar(name, kind):
    cals = store().calendarsForEntityType_(kind)
    if not name:
        return store().defaultCalendarForNewReminders() if kind == EventKit.EKEntityTypeReminder \
            else store().defaultCalendarForNewEvents()
    for c in cals:
        if c.title().lower() == name.lower():
            return c
    raise Refused(f"no list named {name!r}. Available: " + ", ".join(c.title() for c in cals))


def fetch_reminders(predicate):
    done, out = threading.Event(), []

    def cb(items):
        out.extend(items or [])
        done.set()
    store().fetchRemindersMatchingPredicate_completion_(predicate, cb)
    done.wait(15)
    return out


def list_reminders(list_name=None, include_completed=False):
    cals = [find_calendar(list_name, EventKit.EKEntityTypeReminder)] if list_name else None
    if include_completed:
        pred = store().predicateForRemindersInCalendars_(cals)
    else:
        pred = store().predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, cals)
    items = fetch_reminders(pred)
    rows = []
    for r in items:
        due = r.dueDateComponents()
        due_s = ""
        if due and due.year() < 10000:
            hour = due.hour() if due.hour() < 24 else 9
            minute = due.minute() if due.minute() < 60 else 0
            due_s = " due " + say_time(datetime.datetime(due.year(), due.month(), due.day(), hour, minute))
        done = " (done)" if r.isCompleted() else ""
        rows.append(f"- {r.title()}{due_s} [{r.calendar().title()}]{done}")
    lists = ", ".join(c.title() for c in store().calendarsForEntityType_(EventKit.EKEntityTypeReminder))
    return f"Lists: {lists}\n" + ("\n".join(rows[:60]) if rows else "No reminders.")


def create_reminder(title, due=None, notes=None, list_name=None):
    r = EventKit.EKReminder.reminderWithEventStore_(store())
    r.setTitle_(title)
    r.setCalendar_(find_calendar(list_name, EventKit.EKEntityTypeReminder))
    if notes:
        r.setNotes_(notes)
    when = ""
    if due:
        dt = parse_when(due)
        r.setDueDateComponents_(components(dt))
        r.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(nsdate(dt)))
        when = " for " + say_time(dt)
    ok, err = store().saveReminder_commit_error_(r, True, None)
    if not ok:
        raise Refused(f"Reminders refused to save: {err}")
    return f"Created reminder '{title}'{when} in {r.calendar().title()}."


def complete_reminder(title):
    pred = store().predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, None)
    matches = [r for r in fetch_reminders(pred) if title.lower() in r.title().lower()]
    if not matches:
        return f"No open reminder matches {title!r}."
    if len(matches) > 1:
        return "More than one matches: " + "; ".join(r.title() for r in matches[:8]) + f". Ask {NAME} which."
    r = matches[0]
    r.setCompleted_(True)
    store().saveReminder_commit_error_(r, True, None)
    return f"Marked '{r.title()}' done."


def list_events(start=None, days=1):
    begin = parse_when(start) if start else datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = begin + datetime.timedelta(days=max(1, min(int(days or 1), 31)))
    pred = store().predicateForEventsWithStartDate_endDate_calendars_(nsdate(begin), nsdate(end), None)
    events = sorted(store().eventsMatchingPredicate_(pred) or [], key=lambda e: e.startDate().timeIntervalSince1970())
    rows = []
    for e in events:
        s = pydate(e.startDate())
        when = s.strftime("%a %b %d") + (" all day" if e.isAllDay() else s.strftime(" %I:%M %p"))
        loc = f" at {e.location()}" if e.location() else ""
        rows.append(f"- {when}: {e.title()}{loc} [{e.calendar().title()}]")
    span = f"{say_time(begin)} to {say_time(end)}"
    return f"Events {span}:\n" + ("\n".join(rows) if rows else "Nothing scheduled.")


def create_event(title, start, end=None, minutes=60, calendar=None, location=None, notes=None, alert_minutes=10):
    s = parse_when(start)
    e_dt = parse_when(end) if end else s + datetime.timedelta(minutes=int(minutes or 60))
    ev = EventKit.EKEvent.eventWithEventStore_(store())
    ev.setTitle_(title)
    ev.setStartDate_(nsdate(s))
    ev.setEndDate_(nsdate(e_dt))
    ev.setCalendar_(find_calendar(calendar, EventKit.EKEntityTypeEvent))
    if location:
        ev.setLocation_(location)
    if notes:
        ev.setNotes_(notes)
    if alert_minutes is not None:
        ev.addAlarm_(EventKit.EKAlarm.alarmWithRelativeOffset_(-60 * int(alert_minutes)))
    ok, err = store().saveEvent_span_commit_error_(ev, EventKit.EKSpanThisEvent, True, None)
    if not ok:
        raise Refused(f"Calendar refused to save: {err}")
    return f"Added '{title}' on {say_time(s)} to {ev.calendar().title()}."


def set_alarm(at, label="Alarm"):
    dt = parse_when(at)
    if dt < datetime.datetime.now():
        raise Refused("that time has already passed")
    alarms = load_alarms()
    alarms.append({"at": dt.timestamp(), "label": label})
    ALARMS.write_text(json.dumps(alarms))
    create_reminder(f"⏰ {label}", due=at)
    return f"Alarm set for {say_time(dt)}. Max rings on the Mac and the reminder alerts the phone."


def load_alarms():
    try:
        return json.loads(ALARMS.read_text())
    except (OSError, ValueError):
        return []


def due_alarms():
    alarms = load_alarms()
    now = time.time()
    due = [a for a in alarms if a["at"] <= now]
    if due:
        ALARMS.write_text(json.dumps([a for a in alarms if a["at"] > now]))
    return [a for a in due if now - a["at"] < 3600]


def list_alarms():
    alarms = sorted(load_alarms(), key=lambda a: a["at"])
    if not alarms:
        return "No alarms set."
    return "\n".join(f"- {say_time(datetime.datetime.fromtimestamp(a['at']))}: {a['label']}" for a in alarms)


def cancel_alarm(label):
    alarms = load_alarms()
    keep = [a for a in alarms if label.lower() not in a["label"].lower()]
    ALARMS.write_text(json.dumps(keep))
    gone = len(alarms) - len(keep)
    if gone:
        complete_reminder(f"⏰ {label}")
    return f"Cancelled {gone} alarm(s)." if gone else f"No alarm matches {label!r}."
