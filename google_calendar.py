"""Google Calendar bridge. Calendar content only goes to Google, never to the Epitech API.

`epiplan gcal login` is the explicit opt-in: it saves a token with calendar.events and calendar.readonly
scopes. Each copied Epitech activity gets a deterministic event id derived from its marker, so a repeated
sync updates the same event and no local state file is needed.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TZ = ZoneInfo("Europe/Paris")
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
MARK_KEY = "epiplan_marker"  # private extended property: which epiplan copy this event is
EPITECH_PREFIX = "epiplan://epitech/"


class CalendarError(Exception):
    pass


def config_dir():
    return Path(os.environ.get("EPIPLAN_CONFIG_DIR", "~/.config/epiplan")).expanduser()


def client_secret_path():
    return Path(os.environ.get("EPIPLAN_GCAL_CLIENT", str(config_dir() / "google_client_secret.json"))).expanduser()


def token_path():
    return Path(os.environ.get("EPIPLAN_GCAL_TOKEN", str(config_dir() / "google_token.json"))).expanduser()


def config_path():
    return config_dir() / "google_calendar.json"


def load_config():
    p = config_path()
    if not p.exists():
        return {"enabled": False, "calendar_id": "primary", "auto_sync": True}
    try:
        value = json.loads(p.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise ValueError("Invalid Google Calendar config")
        return {"calendar_id": "primary", "auto_sync": True, **value}
    except (OSError, ValueError) as e:
        raise CalendarError("Cannot read Google Calendar configuration. Run epiplan gcal login again.") from e


def enabled():
    return load_config().get("enabled", False) and token_path().exists()


def _private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False)
    os.chmod(path, 0o600)


def _credentials():
    if not token_path().exists():
        raise CalendarError("Not logged in to Google Calendar: run epiplan gcal login first.")
    creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        _private_json(token_path(), json.loads(creds.to_json()))
    return creds


def _service():
    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


def status():
    cfg = load_config()
    return {"enabled": enabled(), "calendar_id": cfg["calendar_id"], "auto_sync": cfg["auto_sync"],
            "logged_in": token_path().exists()}


def connect(calendar_id="primary", auto_sync=True):
    """Interactive OAuth login in the browser. Run from the terminal, not from the MCP server."""
    secret = client_secret_path()
    if not secret.exists():
        raise CalendarError(f"Missing {secret}: create a 'Desktop app' OAuth client in Google Cloud Console "
                            "(Calendar API enabled) and save its JSON there.")
    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    _private_json(token_path(), json.loads(creds.to_json()))
    _private_json(config_path(), {"enabled": True, "calendar_id": calendar_id, "auto_sync": auto_sync})
    return status()


def calendars():
    items = _service().calendarList().list().execute().get("items", [])
    return [{"id": c["id"], "title": c.get("summary", c["id"]),
             "writable": c.get("accessRole") in ("owner", "writer")} for c in items]


def parse(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def bounds(start, end):
    try:
        s, e = parse(start), parse(end)
    except (ValueError, TypeError, AttributeError) as exc:
        raise CalendarError("Invalid dates. Use ISO dates in Europe/Paris or include an offset.") from exc
    if s >= e:
        raise CalendarError("The end must be after the start.")
    return s, e


def _rfc3339(value):
    """Google needs full RFC 3339 datetimes with seconds: tools give e.g. 2026-11-02T09:00+01:00."""
    return parse(value).isoformat(timespec="seconds")


def _local_iso(item):
    if "date" in item:  # all-day event: a bare date
        return datetime.fromisoformat(item["date"]).replace(tzinfo=TZ).isoformat(timespec="minutes")
    return parse(item["dateTime"]).astimezone(TZ).isoformat(timespec="minutes")


def events(start, end, calendar_id=None):
    s, e = bounds(start, end)
    if (e - s).days > 3660:
        raise CalendarError("Calendar queries are limited to ten years.")
    svc, out, page = _service(), [], None
    cal = calendar_id or load_config()["calendar_id"]
    while True:
        resp = svc.events().list(calendarId=cal, timeMin=s.isoformat(),
                                 timeMax=e.isoformat(), singleEvents=True, orderBy="startTime",
                                 pageToken=page).execute()
        for item in resp.get("items", []):
            if item.get("status") == "cancelled":
                continue
            out.append({
                "id": item["id"], "title": item.get("summary", ""), "source": "google_calendar",
                "start": _local_iso(item["start"]), "end": _local_iso(item["end"]),
                "busy": item.get("transparency") != "transparent",
                "location": item.get("location", ""),
                "marker": (item.get("extendedProperties") or {}).get("private", {}).get(MARK_KEY),
            })
        page = resp.get("nextPageToken")
        if not page:
            return out


def marker(event_id, login):
    key = f"{login}\0{int(event_id)}"
    return EPITECH_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def external_marker(title, start, end, calendar_id):
    key = json.dumps([title.strip(), parse(start).isoformat(), parse(end).isoformat(), calendar_id], ensure_ascii=False)
    return "epiplan://external/" + hashlib.sha256(key.encode()).hexdigest()


def _event_id(marker_value):
    # Google ids: lowercase a-v and 0-9, 5 to 1024 chars. Hex digits are a subset of that.
    return "ep" + hashlib.sha256(marker_value.encode()).hexdigest()[:48]


def conflicts(start, end, *, exclude_marker=None):
    s, e = bounds(start, end)
    return [ev for ev in events(start, end)
            if ev.get("busy", True) and ev.get("marker") != exclude_marker
            and parse(ev["start"]) < e and parse(ev["end"]) > s]


def reminders(reminder_minutes):
    """Minutes before the start, up to 5 values. None keeps the event's reminders, [] clears them."""
    if reminder_minutes is None:
        return None
    if (not isinstance(reminder_minutes, list) or len(reminder_minutes) > 5
            or any(isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= 40320 for m in reminder_minutes)):
        raise CalendarError("Reminders: up to 5 whole numbers of minutes before the start, between 0 and 40320.")
    return sorted(set(reminder_minutes))


def _reminders_body(alarms):
    if alarms is None:
        return None
    return {"useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in alarms]}


def upsert(*, title, start, end, marker, calendar_id=None, location="", notes="", all_day=False, alarms=None):
    s, e = bounds(start, end)
    if all_day:
        when = {"start": {"date": s.date().isoformat()}, "end": {"date": e.date().isoformat()}}
    else:
        when = {"start": {"dateTime": _rfc3339(start), "timeZone": "Europe/Paris"},
                "end": {"dateTime": _rfc3339(end), "timeZone": "Europe/Paris"}}
    body = {"id": _event_id(marker), "summary": title, "location": location, "description": notes,
            "extendedProperties": {"private": {MARK_KEY: marker}}, **when}
    if alarms is not None:
        body["reminders"] = _reminders_body(alarms)
    cal = calendar_id or load_config()["calendar_id"]
    svc = _service()
    try:
        result = svc.events().update(calendarId=cal, eventId=body["id"], body=body).execute()
    except HttpError as err:
        if err.resp.status != 404:
            raise CalendarError(f"Google Calendar refused the event: {err.reason}") from err
        try:
            result = svc.events().insert(calendarId=cal, body=body).execute()
        except HttpError as err2:
            if err2.resp.status == 409:
                raise CalendarError("This event was deleted by hand in Google Calendar and is not re-created.") from err2
            raise CalendarError(f"Google Calendar refused the event: {err2.reason}") from err2
    return {"id": result["id"], "start": _local_iso(result["start"]), "end": _local_iso(result["end"])}


def remove(marker, calendar_id=None):
    """Remove only the epiplan copy tracked by this marker, never a personal event."""
    svc = _service()
    try:
        svc.events().delete(calendarId=calendar_id or load_config()["calendar_id"],
                            eventId=_event_id(marker)).execute()
    except HttpError as err:
        if err.resp.status in (404, 410):
            return {"removed": False, "note": "No tracked calendar copy."}
        raise CalendarError(f"Google Calendar refused the removal: {err.reason}") from err
    return {"removed": True}
