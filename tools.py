"""Agent-facing tools for my.epitech.eu.

Plain functions returning small JSON-able dicts. No tokens, no raw API payloads, no other
students' names. Every write goes through `_guarded_write`, which enforces the guardrails
(dry run by default, kill switch, action budget, module allowlist, audit log).

Env:
  EPIPLAN_ENABLE_WRITES=1        writes are refused unless set (kill switch)
  EPIPLAN_MAX_WRITES=5           max real writes per server process
  EPIPLAN_MODULE_ALLOWLIST=A,B   unit codes `register_module` may touch (empty = none)
  EPIPLAN_AUDIT=<path>           JSONL audit log (default ~/.config/epiplan/audit.jsonl)
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

import google_calendar as gcal
from client import ApiError, AuthError, Client
from google_calendar import CalendarError

TZ = ZoneInfo("Europe/Paris")
MAX_WINDOW_DAYS = 44  # API limit is 45
AUDIT = Path(os.environ.get("EPIPLAN_AUDIT", "~/.config/epiplan/audit.jsonl")).expanduser()

_client = None
_writes_done = 0


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


# ---------- helpers ----------

def _dt(s):
    """ISO string from the API (UTC) or from the agent ('2026-10-27' or with offset)."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def _api(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _local(s):
    return _dt(s).astimezone(TZ).isoformat(timespec="minutes") if s else None


def _closes_at(ev):
    h = ev.get("registrationDeadlineHours")
    return _dt(ev["startDate"]) - timedelta(hours=h or 0)


def _error(e):
    if isinstance(e, ApiError):
        try:
            msg = json.loads(e.body).get("message", e.body)
        except Exception:
            msg = e.body
        return {"ok": False, "error": f"HTTP {e.status}: {msg}"}
    return {"ok": False, "error": str(e)}


def _fetch_events(start, end):
    """Raw enriched events over any range (split in <= 44-day windows), calendars excluded."""
    c, out, cur = client(), {}, _dt(start)
    end = _dt(end)
    while cur < end:
        nxt = min(cur + timedelta(days=MAX_WINDOW_DAYS), end)
        for ev in c.call("GET", "/events", params={
            "startDate": _api(cur), "endDate": _api(nxt),
            "cursus": c.profile["cursus"], "semester": c.profile["semester"],
        }):
            if not ev.get("isCalendarEvent") and ev.get("activityInstanceId"):
                out[ev["id"]] = ev
        cur = nxt
    return list(out.values())


def _my_event_ids(start, end):
    c = client()
    ids, cur, end = set(), _dt(start), _dt(end)
    while cur < end:
        nxt = min(cur + timedelta(days=MAX_WINDOW_DAYS), end)
        for ev in c.call("GET", "/events", params={
            "startDate": _api(cur), "endDate": _api(nxt), "registered": "me",
            "cursus": c.profile["cursus"], "semester": c.profile["semester"],
        }):
            ids.add(ev["id"])
        cur = nxt
    return ids


def _registered_modules():
    return {m["id"] for m in _modules() if m["isRegistered"]}


def _modules(school_year=None):
    c = client()
    return c.call("GET", "/units/instances",
                  params={"schoolYear": school_year or c.profile["cursusSchoolYear"], "limit": 100})["data"]


def _slim_event(ev, mine, now=None):
    now = now or datetime.now(timezone.utc)
    closes = _closes_at(ev)
    cap = ev.get("maxCapacity")
    full = bool(cap) and ev["registrationsCount"] >= cap
    return {
        "id": ev["id"],
        "title": ev.get("fullTitle") or ev.get("activityName") or ev.get("title"),
        "module": {"unit_instance_id": ev["unitInstanceId"], "code": ev.get("unitCode"), "name": ev.get("unitName")},
        "start": _local(ev["startDate"]),
        "end": _local(ev["endDate"]),
        "registered": ev["id"] in mine,
        "registration_closes_at": _local(closes.isoformat()),
        "registration_open": closes > now and _dt(ev["endDate"]) > now and not full,
        "seats": {"registered": ev["registrationsCount"], "max": cap, "full": full},
        "uses_slots": bool(ev.get("activitySlotsEnabled")),
        "slots_count": ev.get("slotsCount", 0),  # total created by staff, taken or not
        "is_exam": bool(ev.get("isExam")),
    }


def _find_event(event_id):
    """Enriched event by id (GET /events/{id} lacks the enriched fields). Widens the window once."""
    raw = client().call("GET", f"/events/{event_id}")
    start = _dt(raw["startDate"])
    for before, after in ((1, 2), (7, 8)):
        for ev in _fetch_events(_api(start - timedelta(days=before)), _api(start + timedelta(days=after))):
            if ev["id"] == event_id:
                return ev
    return None


def _slim_slot(s, me):
    reg = s.get("registration")
    return {"id": s["id"], "start": _local(s["startDate"]), "end": _local(s["endDate"]),
            "taken": bool(reg), "mine": bool(reg and (reg.get("student") or {}).get("login") == me)}


# ---------- read tools ----------

def get_profile() -> dict:
    """Who am I: login, name, cursus, semester, school year."""
    p = client().profile
    return {k: p.get(k) for k in ("login", "firstname", "lastname", "cursus", "semester", "tekYear",
                                  "promotion", "cursusSchoolYear")}


def list_modules(only_unregistered: bool = False, school_year: int | None = None) -> list[dict]:
    """Modules (units) of the school year with my registration state and registration deadline."""
    now = datetime.now(timezone.utc)
    out = []
    for m in _modules(school_year):
        if only_unregistered and m["isRegistered"]:
            continue
        out.append({
            "unit_instance_id": m["id"], "unit_code": m["unitCode"], "name": m["name"],
            "registered": m["isRegistered"],
            "start": _local(m["startDate"]), "end": _local(m["endDate"]),
            "registration_closes_at": _local(m["endRegistrationDate"]),
            "registration_open": _dt(m["endRegistrationDate"]) > now,
        })
    return out


def list_events(start: str, end: str, registered_only: bool = False, only_open: bool = False) -> list[dict]:
    """Planning activities between two dates (ISO; a bare date means Paris midnight; any range).
    `registered_only`: only mine. `only_open`: registration still possible (not full, not closed)."""
    mine = _my_event_ids(start, end)
    now = datetime.now(timezone.utc)
    out = [_slim_event(ev, mine, now) for ev in _fetch_events(start, end)]
    if registered_only:
        out = [e for e in out if e["registered"]]
    if only_open:
        out = [e for e in out if e["registration_open"] and not e["registered"]]
    return sorted(out, key=lambda e: e["start"])


def my_schedule(start: str, end: str) -> list[dict]:
    """Events I am registered to between two dates."""
    return list_events(start, end, registered_only=True)


def get_event(event_id: int) -> dict:
    """One event with my registration state, my module state and its slots (no names)."""
    c = client()
    ev = _find_event(event_id)
    if ev is None:
        # Not in my cursus/semester planning listing: still show what the API exposes about the event.
        raw = c.call("GET", f"/events/{event_id}")
        slots = [_slim_slot(s, c.login) for s in c.call("GET", f"/events/{event_id}/slots")]
        return {"id": event_id, "enriched": False, "start": _local(raw["startDate"]), "end": _local(raw["endDate"]),
                "registered": bool(c.call("GET", f"/events/{event_id}/registrations/me")),
                "slots": slots, "slots_free": sum(1 for s in slots if not s["taken"]),
                "free_slot_times": sorted({s["start"] for s in slots if not s["taken"]}),
                "note": "Event is not in my cursus/semester listing; module and seat info unavailable."}
    mine = {event_id} if c.call("GET", f"/events/{event_id}/registrations/me") else set()
    out = _slim_event(ev, mine)
    out["module"]["registered"] = ev["unitInstanceId"] in _registered_modules()
    out["description"] = (ev.get("activityDescription") or "")[:500]
    if ev.get("activitySlotsEnabled"):
        slots = c.call("GET", f"/events/{event_id}/slots")
        out["slots"] = [_slim_slot(s, c.login) for s in slots]
        out["registered"] = out["registered"] or any(s["mine"] for s in out["slots"])
        out["slots_free"] = sum(1 for s in out["slots"] if not s["taken"])
        out["free_slot_times"] = sorted({s["start"] for s in out["slots"] if not s["taken"]})
    return out


# ---------- write tools ----------

def _audit(entry):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"at": datetime.now(TZ).isoformat(timespec="seconds"), **entry}, ensure_ascii=False) + "\n")


def _guarded_write(action, target, reason, dry_run, plan, do, cost=1):
    """Shared tail of every write: dry run, kill switch, budget, audit, API error mapping.
    `cost` counts against EPIPLAN_MAX_WRITES: Google Calendar writes pass cost=0, only Epitech writes are budgeted."""
    global _writes_done
    if not reason.strip():
        return {"ok": False, "error": "A non-empty reason is required."}
    if dry_run:
        _audit({"action": action, "target": target, "reason": reason, "dry_run": True})
        return {"ok": True, "dry_run": True, "would": plan}
    if os.environ.get("EPIPLAN_ENABLE_WRITES") != "1":
        return {"ok": False, "error": "Writes are disabled (set EPIPLAN_ENABLE_WRITES=1 on the server)."}
    if _writes_done + cost > int(os.environ.get("EPIPLAN_MAX_WRITES", "5")):
        return {"ok": False, "error": "Write budget for this run exhausted."}
    try:
        result = do()
    except (ApiError, AuthError, CalendarError, HttpError) as e:
        _audit({"action": action, "target": target, "reason": reason, "ok": False, "error": str(e)[:200]})
        return _error(e)
    _writes_done += cost
    _audit({"action": action, "target": target, "reason": reason, "ok": True})
    return {"ok": True, "done": plan}


def register_event(event_id: int, reason: str, slot_id: int | None = None, allow_overlap: bool = False,
                   dry_run: bool = True) -> dict:
    """Register to an event (or to one slot of it). `reason` is mandatory (audit log).
    Checks module registration, window, capacity, overlaps. dry_run=True by default: nothing is written."""
    try:
        ev = _find_event(event_id)
        if ev is None:
            return {"ok": False, "error": f"Event {event_id} not found in my planning"}
        c, now = client(), datetime.now(timezone.utc)
        if ev["unitInstanceId"] not in _registered_modules():
            return {"ok": False, "error": f"Not registered to module {ev.get('unitCode')} (register_module first)."}
        if ev.get("isExam"):
            return {"ok": False, "error": "Exam: refused, register by hand."}
        if _dt(ev["endDate"]) <= now or _closes_at(ev) <= now:
            return {"ok": False, "error": f"Registration closed since {_local(_closes_at(ev).isoformat())}."}
        cap = ev.get("maxCapacity")
        if cap and ev["registrationsCount"] >= cap:
            return {"ok": False, "error": "Event is full."}
        uses_slots = bool(ev.get("activitySlotsEnabled"))
        if uses_slots and slot_id is None:
            return {"ok": False, "error": "This activity uses slots: call get_event and pass slot_id."}
        if not uses_slots and slot_id is not None:
            return {"ok": False, "error": "This event has no slots."}
        if c.call("GET", f"/events/{event_id}/registrations/me"):
            return {"ok": False, "error": "Already registered."}
        if uses_slots:
            slot = next((s for s in c.call("GET", f"/events/{event_id}/slots") if s["id"] == slot_id), None)
            if slot is None or slot.get("registration"):
                return {"ok": False, "error": "Slot does not exist or is taken."}
            start, end = _dt(slot["startDate"]), _dt(slot["endDate"])
        else:
            start, end = _dt(ev["startDate"]), _dt(ev["endDate"])
        if not allow_overlap:
            mine = _my_event_ids(_api(start - timedelta(days=1)), _api(end + timedelta(days=1)))
            clash = [e for e in _fetch_events(_api(start - timedelta(days=1)), _api(end + timedelta(days=1)))
                     if e["id"] in mine and e["id"] != event_id
                     and _dt(e["startDate"]) < end and _dt(e["endDate"]) > start]
            if clash:
                return {"ok": False, "error": "Overlaps with my events.",
                        "conflicts": [{"id": e["id"], "title": e.get("fullTitle"), "start": _local(e["startDate"])}
                                      for e in clash]}
        path = f"/events/{event_id}/slots/{slot_id}/registrations" if uses_slots else f"/events/{event_id}/registrations"
        plan = {"register": ev.get("fullTitle"), "event_id": event_id, "slot_id": slot_id, "start": _local(start.isoformat())}
        return _guarded_write("register_event", event_id, reason, dry_run, plan, lambda: c.call("POST", path))
    except (ApiError, AuthError) as e:
        return _error(e)


def unregister_event(event_id: int, reason: str, dry_run: bool = True) -> dict:
    """Cancel my registration to an event (or its slot). Only inside the unregistration window."""
    try:
        ev = _find_event(event_id)
        if ev is None:
            return {"ok": False, "error": f"Event {event_id} not found in my planning"}
        c, now = client(), datetime.now(timezone.utc)
        if _closes_at(ev) <= now:
            return {"ok": False, "error": "Unregistration window closed."}
        path = f"/events/{event_id}/registrations"
        if ev.get("activitySlotsEnabled"):
            mine = next((s for s in c.call("GET", f"/events/{event_id}/slots") if _slim_slot(s, c.login)["mine"]), None)
            if mine is None:
                return {"ok": False, "error": "I have no slot on this event."}
            path = f"/events/{event_id}/slots/{mine['id']}/registrations"
        elif not c.call("GET", f"/events/{event_id}/registrations/me"):
            return {"ok": False, "error": "Not registered."}
        plan = {"unregister": ev.get("fullTitle"), "event_id": event_id}
        return _guarded_write("unregister_event", event_id, reason, dry_run, plan, lambda: c.call("DELETE", path))
    except (ApiError, AuthError) as e:
        return _error(e)


def register_module(unit_instance_id: int, reason: str, dry_run: bool = True) -> dict:
    """Register to a module. Only unit codes in EPIPLAN_MODULE_ALLOWLIST are allowed (not tested live)."""
    try:
        m = next((m for m in _modules() if m["id"] == unit_instance_id), None)
        if m is None:
            return {"ok": False, "error": "Unknown module."}
        allowed = {x.strip() for x in os.environ.get("EPIPLAN_MODULE_ALLOWLIST", "").split(",") if x.strip()}
        if m["unitCode"] not in allowed:
            return {"ok": False, "error": f"{m['unitCode']} is not in the module allowlist."}
        if m["isRegistered"]:
            return {"ok": False, "error": "Already registered."}
        if _dt(m["endRegistrationDate"]) <= datetime.now(timezone.utc):
            return {"ok": False, "error": "Module registration closed."}
        plan = {"register_module": m["unitCode"], "unit_instance_id": unit_instance_id}
        return _guarded_write("register_module", unit_instance_id, reason, dry_run, plan,
                              lambda: client().call("POST", f"/units/instances/{unit_instance_id}/register"))
    except (ApiError, AuthError) as e:
        return _error(e)


# ---------- Google Calendar (add-on) ----------

_FAILURES = (ApiError, AuthError, CalendarError, HttpError, ValueError)


def _epitech_bookings(start, end):
    """My bookings in a range, with the booked slot's time rather than the parent activity's window."""
    c, result, seen = client(), [], set()
    cur, stop = gcal.bounds(start, end)
    while cur < stop:
        nxt = min(cur + timedelta(days=MAX_WINDOW_DAYS), stop)
        raw = c.call("GET", "/events", params={"startDate": _api(cur), "endDate": _api(nxt),
                     "registered": "me", "cursus": c.profile["cursus"], "semester": c.profile["semester"]})
        for ev in raw:
            if ev["id"] in seen:
                continue
            seen.add(ev["id"])
            slots = [None]
            if ev.get("activitySlotsEnabled"):
                slots = [s for s in c.call("GET", f"/events/{ev['id']}/slots") if _slim_slot(s, c.login)["mine"]]
                if not slots:
                    raise CalendarError("A registered Epitech activity has no readable booked slot. Cannot verify availability.")
            for slot in slots:
                s, e = (slot or ev)["startDate"], (slot or ev)["endDate"]
                if _dt(s) >= stop or _dt(e) <= _dt(start):
                    continue
                result.append({"id": ev["id"], "title": ev.get("fullTitle") or ev.get("title") or ev.get("activityName") or "Epitech",
                               "start": _local(s), "end": _local(e), "source": "epitech", "busy": True,
                               "marker": gcal.marker(ev["id"], c.login)})
        cur = nxt
    return sorted(result, key=lambda e: _dt(e["start"]))


def _epitech_conflicts(start, end, exclude_event_id=None):
    s, e = gcal.bounds(start, end)
    return [ev for ev in _epitech_bookings(start, end) if ev["id"] != exclude_event_id
            and _dt(ev["start"]) < e and _dt(ev["end"]) > s]


def _both_conflicts(start, end, exclude_event_id=None, exclude_marker=None):
    """Conflicts with registered Epitech slots and, when connected, Google Calendar events.
    A Google copy of an Epitech activity is not a conflict with that same activity."""
    school = _epitech_conflicts(start, end, exclude_event_id)
    mirrors = {e["marker"] for e in school}
    google = gcal.conflicts(start, end, exclude_marker=exclude_marker) if gcal.enabled() else []
    return school + [e for e in google if e.get("marker") not in mirrors]


def _chosen_slot(ev, event_id, slot_id):
    """The slot to copy: the one I hold, or the one given. None for activities without slots."""
    if not ev.get("activitySlotsEnabled"):
        if slot_id is not None:
            raise CalendarError("This activity has no slots.")
        return None
    c = client()
    slots = c.call("GET", f"/events/{event_id}/slots")
    mine = [s for s in slots if _slim_slot(s, c.login)["mine"]]
    if slot_id is None and len(mine) == 1:
        slot = mine[0]
    elif slot_id is not None:
        slot = next((s for s in slots if s["id"] == slot_id), None)
    else:
        slot = None
    if slot is None:
        raise CalendarError("Choose an existing activity slot before copying it to Google Calendar.")
    if _slim_slot(slot, c.login)["taken"] and not _slim_slot(slot, c.login)["mine"]:
        raise CalendarError("This slot is booked by someone else.")
    return slot


def _gcal_event_plan(ev, slot=None):
    chosen = slot or ev
    rooms = chosen.get("rooms") or ev.get("rooms") or []
    location = ", ".join(dict.fromkeys(r["name"].strip() for r in rooms
                                     if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"].strip()))
    return {"location": location, "title": ev.get("fullTitle") or ev.get("activityName") or ev.get("title") or "Epitech",
            "start": _local(chosen["startDate"]), "end": _local(chosen["endDate"]),
            "marker": gcal.marker(ev["id"], client().login),
            "notes": "Activité Epitech sélectionnée dans epiplan. https://my.epitech.eu"}


def google_calendar_status() -> dict:
    """Google Calendar connection state: logged in, default calendar, auto-sync. Never opens a login window."""
    try:
        return gcal.status()
    except _FAILURES as exc:
        return _error(exc)


def list_google_calendars() -> list[dict]:
    """Google calendars on this account, their names and whether they are writable."""
    try:
        return gcal.calendars()
    except _FAILURES as exc:
        return [_error(exc)]


def google_schedule(start: str, end: str) -> dict:
    """Registered Epitech slots AND Google Calendar events; epiplan copies are deduplicated.
    Only reports the Google side as checked when Google Calendar is connected."""
    try:
        school = _epitech_bookings(start, end)
        mirrors = {e["marker"] for e in school}
        sources, personal = ["epitech"], []
        if gcal.enabled():
            sources.append("google_calendar")
            personal = [e for e in gcal.events(start, end) if e.get("marker") not in mirrors]
        return {"events": sorted(school + personal, key=lambda e: _dt(e["start"])), "sources_checked": sources}
    except _FAILURES as exc:
        return _error(exc)


def check_availability(start: str, end: str) -> dict:
    """Check a proposed time against registered Epitech slots and, when connected, Google Calendar."""
    try:
        conflicts = _both_conflicts(start, end)
    except _FAILURES as exc:
        return _error(exc)
    sources = ["epitech"] + (["google_calendar"] if gcal.enabled() else [])
    return {"available": not conflicts, "conflicts": conflicts, "sources_checked": sources}


def sync_google_calendar_event(event_id: int, reason: str, slot_id: int | None = None,
                               calendar_id: str | None = None, allow_overlap: bool = False,
                               dry_run: bool = True, reminder_minutes: list[int] | None = None) -> dict:
    """Copy a registered Epitech activity (its booked slot if slot-based) into Google Calendar, without registering.
    Idempotent per school event: repeating it updates the same copy. Checks conflicts with Epitech and Google.
    reminder_minutes: reminders in minutes before the start (e.g. [60, 10]); [] clears them, omitted keeps them."""
    try:
        alarms = gcal.reminders(reminder_minutes)
        if not gcal.enabled():
            raise CalendarError("Connect Google Calendar first: run epiplan gcal login in a terminal.")
        ev = _find_event(event_id)
        if ev is None:
            raise CalendarError("Epitech activity not found in your planning.")
        plan = _gcal_event_plan(ev, _chosen_slot(ev, event_id, slot_id))
        target = calendar_id or gcal.load_config()["calendar_id"]
        cal = next((c for c in gcal.calendars() if c["id"] == target), None)
        if cal is None or not cal["writable"]:
            raise CalendarError("Choose an available writable Google calendar.")
        conflicts = _both_conflicts(plan["start"], plan["end"], event_id, plan["marker"])
        if conflicts and not allow_overlap:
            return {"ok": False, "error": "Calendar copy overlaps an existing activity.", "conflicts": conflicts}
        plan = {**plan, "calendar": cal["title"], "reminder_minutes": alarms}
        return _guarded_write("sync_google_calendar_event", plan["marker"], reason, dry_run, plan,
                              lambda: gcal.upsert(title=plan["title"], start=plan["start"], end=plan["end"],
                                                  marker=plan["marker"], calendar_id=target,
                                                  location=plan["location"], notes=plan["notes"], alarms=alarms),
                              cost=0)
    except _FAILURES as exc:
        return _error(exc)


def create_google_external_event(title: str, start: str, end: str, reason: str,
                                 calendar_id: str | None = None, location: str = "", notes: str = "",
                                 all_day: bool = False, allow_overlap: bool = False, dry_run: bool = True,
                                 reminder_minutes: list[int] | None = None) -> dict:
    """Add a personal, non-Epitech event to Google Calendar after checking Epitech and Google schedules.
    Never writes to Epitech. Repeating the same title, time and calendar updates the same event.
    reminder_minutes: reminders in minutes before the start (e.g. [30]); [] clears them."""
    try:
        alarms = gcal.reminders(reminder_minutes)
        s, e = gcal.bounds(start, end)
        if not title.strip():
            raise CalendarError("An event title is required.")
        if all_day and (s.timetz().replace(tzinfo=None) != datetime.min.time()
                        or e.timetz().replace(tzinfo=None) != datetime.min.time()):
            raise CalendarError("All-day events must start and end at midnight; the end date is exclusive.")
        if not gcal.enabled():
            raise CalendarError("Connect Google Calendar first: run epiplan gcal login in a terminal.")
        target = calendar_id or gcal.load_config()["calendar_id"]
        tag = gcal.external_marker(title, start, end, target)
        conflicts = _both_conflicts(start, end, exclude_marker=tag)
        if conflicts and not allow_overlap:
            return {"ok": False, "error": "Personal event overlaps an existing activity.", "conflicts": conflicts}
        cal = next((c for c in gcal.calendars() if c["id"] == target), None)
        if cal is None or not cal["writable"]:
            raise CalendarError("Choose an available writable Google calendar.")
        plan = {"title": title.strip(), "start": s.isoformat(), "end": e.isoformat(),
                "calendar": cal["title"], "all_day": all_day, "reminder_minutes": alarms}
        return _guarded_write("create_google_external_event", tag, reason, dry_run, plan,
                              lambda: gcal.upsert(title=title.strip(), start=start, end=end, marker=tag,
                                                  calendar_id=target, location=location, notes=notes,
                                                  all_day=all_day, alarms=alarms),
                              cost=0)
    except _FAILURES as exc:
        return _error(exc)


READ_TOOLS = [get_profile, list_modules, list_events, my_schedule, get_event,
              google_calendar_status, list_google_calendars, google_schedule, check_availability]
WRITE_TOOLS = [register_event, unregister_event, register_module,
               sync_google_calendar_event, create_google_external_event]
