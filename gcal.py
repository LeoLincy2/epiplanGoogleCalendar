#!/usr/bin/env python3
"""Copy my registered my.epitech.eu activities into Google Calendar (terminal side of the add-on).

    gcal.py login                          # once: Google consent in the browser
    gcal.py status
    gcal.py sync [--days 30] [--calendar primary] [--no-prune]

Only activities you are registered to are sent. For slot-based activities the booked slot's time is used,
not the whole activity's window. Each copy has a stable event id, so running sync again updates it instead
of duplicating it. Activities you unregister from are removed on the next sync (only epiplan copies are deleted).

Needs ~/.config/epiplan/google_client_secret.json: an OAuth client of type "Desktop app", with the
Google Calendar API enabled in your Google Cloud project.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import google_calendar as gcal
from google_calendar import CalendarError

TZ = ZoneInfo("Europe/Paris")
NOTE = "Activité Epitech sélectionnée dans epiplan. https://my.epitech.eu"


def cmd_login(a):
    gcal.connect(a.calendar, auto_sync=True)
    print("Google Calendar login saved.")


def cmd_status(a):
    print(json.dumps(gcal.status(), ensure_ascii=False, indent=2))


def cmd_sync(a):
    import tools  # imported here: the MCP tools need the Epitech client, login and sync don't

    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=a.days)
    bookings = tools._epitech_bookings(today.isoformat(), end.isoformat())
    keep = set()
    for b in bookings:
        keep.add(b["marker"])
        gcal.upsert(title=b["title"], start=b["start"], end=b["end"], marker=b["marker"],
                    calendar_id=a.calendar, notes=NOTE)
    removed = 0
    if not a.no_prune:
        for ev in gcal.events(today.isoformat(), end.isoformat(), calendar_id=a.calendar):
            marker = ev.get("marker")
            if marker and marker.startswith(gcal.EPITECH_PREFIX) and marker not in keep:
                removed += gcal.remove(marker, calendar_id=a.calendar)["removed"]
    print(f"{len(bookings)} registered activities synced, {removed} removed.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login")
    s.add_argument("--calendar", default="primary", help="Google calendar id used by default")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("sync")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--calendar", default="primary", help="Google calendar id (default: your primary calendar)")
    s.add_argument("--no-prune", action="store_true", help="keep events you are no longer registered to")
    s.set_defaults(fn=cmd_sync)
    a = p.parse_args()
    try:
        a.fn(a)
    except CalendarError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
