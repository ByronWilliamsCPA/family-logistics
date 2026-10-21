#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "websockets>=13",
# ]
# ///
"""M0 check 3: inventory Home Assistant school, calendar, zone and routing inputs.

Uses a SHORT-LIVED Home Assistant long-lived access token that the owner creates
for this run and deletes immediately afterwards (Profile -> Security). Family
Logistics never stores a Home Assistant credential; this token exists only for
discovery. The token is read from ``HA_TOKEN`` or an interactive prompt.

Read-only: uses ``GET /api/states``, ``GET /api/services``, the websocket
registry list commands, and the ``calendar.get_events`` action (which returns
data and changes nothing).

Reports:
* the integration (platform and config-entry domain/title) behind each
  calendar entity, including ``calendar.family_calendar``,
  ``calendar.school_district`` and ``calendar.school_year``;
* candidate bell-schedule representations: ``schedule.*`` helpers, calendars,
  template sensors and other entities matching ``--pattern`` (default
  ``school|bell``), with their attribute KEYS only;
* the shape of ``calendar.get_events`` output (keys, event counts; summaries
  only for school calendars with ``--show-school-summaries``);
* zones: names, radius, whether coordinates exist (coordinates are never
  printed unless ``--show-coordinates``);
* whether the ``waze_travel_time`` integration and action are present.

Exit codes:
    0  inventory complete and every expected calendar entity was found
    1  inventory complete but an expected entity or Waze is missing
    2  usage, authentication or connectivity error

Run with: ``uv run --script tools/m0/check3_ha_calendar_inventory.py --help``
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
from typing import Any, Final

import httpx
import websockets

EXPECTED_CALENDARS: Final = (
    "calendar.family_calendar",
    "calendar.school_district",
    "calendar.school_year",
)
EXIT_OK: Final = 0
EXIT_MISSING: Final = 1
EXIT_ERROR: Final = 2

JsonObj = dict[str, Any]


async def _ws_call(ws_url: str, token: str, commands: list[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            msg = "websocket authentication failed"
            raise PermissionError(msg)
        for msg_id, command in enumerate(commands, start=1):
            await ws.send(json.dumps({"id": msg_id, "type": command}))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == msg_id:
                    results[command] = (
                        reply.get("result") if reply.get("success") else None
                    )
                    break
    return results


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Home Assistant input inventory (M0 check 3). Read-only."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("HA_URL"),
        required=os.environ.get("HA_URL") is None,
        help="Home Assistant base URL (required unless $HA_URL is set)",
    )
    parser.add_argument(
        "--pattern", default="school|bell", help="regex for schedule entities"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="calendar.get_events window"
    )
    parser.add_argument("--show-school-summaries", action="store_true")
    parser.add_argument("--show-coordinates", action="store_true")
    parser.add_argument("--json", action="store_true", help="also print a JSON summary")
    return parser.parse_args(argv)


def _get_events(client: httpx.Client, entity_id: str, days: int) -> JsonObj | None:
    response = client.post(
        "/api/services/calendar/get_events?return_response",
        json={"entity_id": entity_id, "duration": {"days": days}},
    )
    if response.status_code != httpx.codes.OK:
        return None
    body: JsonObj = response.json()
    return body.get("service_response", {}).get(entity_id)


def main(argv: list[str] | None = None) -> int:
    """Run the inventory.

    Args:
        argv: Optional argument list.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    token = os.environ.get("HA_TOKEN") or getpass.getpass(
        "Short-lived HA token (hidden): "
    )
    if not token:
        print("error: no token supplied", file=sys.stderr)
        return EXIT_ERROR
    base = args.url.rstrip("/")
    ws_url = re.sub(r"^http", "ws", base) + "/api/websocket"
    pattern = re.compile(args.pattern, re.IGNORECASE)
    summary: JsonObj = {}
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        ) as client:
            states_resp = client.get("/api/states")
            if states_resp.status_code == httpx.codes.UNAUTHORIZED:
                print("error: token rejected (401)", file=sys.stderr)
                return EXIT_ERROR
            states_resp.raise_for_status()
            states: list[JsonObj] = states_resp.json()
            services: list[JsonObj] = client.get("/api/services").json()
            registry = asyncio.run(
                _ws_call(
                    ws_url, token, ["config/entity_registry/list", "config_entries/get"]
                )
            )
            entity_reg = {
                e["entity_id"]: e
                for e in registry.get("config/entity_registry/list") or []
            }
            entries = {
                e["entry_id"]: e for e in registry.get("config_entries/get") or []
            }

            def source(entity_id: str) -> str:
                reg = entity_reg.get(entity_id)
                if reg is None:
                    return "not in entity registry (YAML or template)"
                entry = entries.get(reg.get("config_entry_id") or "")
                entry_text = (
                    f"{entry['domain']} '{entry.get('title', '')}'" if entry else "-"
                )
                return f"platform={reg.get('platform')} entry={entry_text}"

            print("== Calendars")
            calendars = [s for s in states if s["entity_id"].startswith("calendar.")]
            summary["calendars"] = {}
            for state in calendars:
                entity_id = state["entity_id"]
                events = _get_events(client, entity_id, args.days)
                count = len(events.get("events", [])) if events else None
                keys = (
                    sorted(events["events"][0].keys())
                    if events and events.get("events")
                    else []
                )
                print(
                    f" {entity_id}: {source(entity_id)}; events_{args.days}d={count}; keys={keys}"
                )
                is_school = "school" in entity_id
                if args.show_school_summaries and is_school and events:
                    for event in events.get("events", [])[:15]:
                        print(f"     {event.get('start')} {event.get('summary')}")
                summary["calendars"][entity_id] = {
                    "source": source(entity_id),
                    "events": count,
                }
            missing = [c for c in EXPECTED_CALENDARS if c not in summary["calendars"]]

            print("\n== Bell-schedule candidates (attribute keys only)")
            candidates = [
                s
                for s in states
                if s["entity_id"].split(".")[0]
                in {
                    "schedule",
                    "sensor",
                    "binary_sensor",
                    "input_datetime",
                    "calendar",
                    "template",
                }
                and (
                    pattern.search(s["entity_id"])
                    or pattern.search(
                        str(s.get("attributes", {}).get("friendly_name", ""))
                    )
                )
            ]
            summary["bell_schedule_candidates"] = []
            for state in candidates:
                attrs = sorted(state.get("attributes", {}).keys())
                print(
                    f" {state['entity_id']}: {source(state['entity_id'])}; attributes={attrs}"
                )
                summary["bell_schedule_candidates"].append(state["entity_id"])
            if not candidates:
                print(" none matched; rerun with --pattern naming the schools")

            print("\n== Zones")
            summary["zones"] = []
            for state in (s for s in states if s["entity_id"].startswith("zone.")):
                attrs = state.get("attributes", {})
                has_coords = "latitude" in attrs and "longitude" in attrs
                coords = (
                    f" lat={attrs['latitude']} lon={attrs['longitude']}"
                    if args.show_coordinates and has_coords
                    else ""
                )
                print(
                    f" {state['entity_id']} '{attrs.get('friendly_name', '')}' "
                    f"radius={attrs.get('radius')} coordinates={has_coords}{coords}"
                )
                summary["zones"].append(state["entity_id"])

            print("\n== Waze travel time")
            waze_service = any(s.get("domain") == "waze_travel_time" for s in services)
            waze_entities = [
                s["entity_id"]
                for s in states
                if entity_reg.get(s["entity_id"], {}).get("platform")
                == "waze_travel_time"
            ]
            print(f" action available={waze_service}; sensors={waze_entities or '-'}")
            summary["waze"] = {"action": waze_service, "sensors": waze_entities}
    except (
        httpx.HTTPError,
        OSError,
        PermissionError,
        websockets.WebSocketException,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(summary, indent=2))
    print("\nReminder: delete the short-lived token in HA now (Profile -> Security).")
    if missing or not summary["waze"]["action"]:
        print(
            f"RESULT: ATTENTION missing={missing or '-'} waze={summary['waze']['action']}"
        )
        return EXIT_MISSING
    print("RESULT: PASS")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
