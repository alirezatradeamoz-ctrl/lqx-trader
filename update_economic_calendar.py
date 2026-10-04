#!/usr/bin/env python3
"""Fetch, normalize and filter Finance Calendar events for the LQX Trader site.

No third-party Python packages are required.
The source is public and does not require an API key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.financecalendar.com/wp-json/fc/v1/calendar"
SOURCE_NAME = "Finance Calendar"
SOURCE_ATTRIBUTION_URL = "https://www.financecalendar.com"
TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "UTC")
OUTPUT = Path("data/economic-calendar.json")

# Gold is not supplied as XAU by the source. Keep this list deliberately narrow:
# only USD macro releases that commonly have a direct rates/real-yield/USD channel
# into gold are eligible for the Gold view.
GOLD_EVENT_PATTERNS = [
    r"\bfomc\b",
    r"federal reserve",
    r"fed (?:rate|interest|funds)",
    r"interest rate decision",
    r"federal funds",
    r"consumer price index|\bcpi\b",
    r"personal consumption expenditures|\bpce\b",
    r"core pce",
    r"non.?farm payrolls|employment situation",
    r"unemployment rate",
    r"average hourly earnings",
    r"retail sales",
    r"gross domestic product|\bgdp\b",
    r"ism (?:manufacturing|services|non.?manufacturing|pmi)",
    r"initial jobless claims|jobless claims",
]
GOLD_RE = [re.compile(p, re.I) for p in GOLD_EVENT_PATTERNS]

CURRENCY_ALIASES = {
    "USD": "USD", "US": "USD", "USA": "USD", "UNITED STATES": "USD",
    "EUR": "EUR", "EU": "EUR", "EURO AREA": "EUR", "EUROZONE": "EUR",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def pick(obj: dict, *keys):
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def normalize_currency(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    # Handle arrays or values like "USD, United States".
    for token in re.split(r"[,/|;]+", text):
        token = token.strip()
        if token in CURRENCY_ALIASES:
            return CURRENCY_ALIASES[token]
    for code in ("USD", "EUR"):
        if re.search(rf"\b{code}\b", text):
            return code
    return None


def infer_currency(event: dict) -> str | None:
    for key in ("currency", "ccy", "currencies", "country", "countries", "region"):
        cur = normalize_currency(pick(event, key))
        if cur:
            return cur
    text = " ".join(str(pick(event, k) or "") for k in ("name", "title", "event", "description"))
    # Only infer EUR/USD from explicit geographic/currency words; do not guess from
    # generic macro titles such as "GDP".
    if re.search(r"\b(eurozone|euro area|european central bank|ecb|eur)\b", text, re.I):
        return "EUR"
    if re.search(r"\b(us|u\.s\.|united states|federal reserve|fed|usd)\b", text, re.I):
        return "USD"
    return None


def parse_dt(event: dict) -> datetime | None:
    raw = pick(event, "time_utc", "datetime", "scheduledAt", "scheduled_at", "date_time", "timestamp")
    if not raw:
        d = pick(event, "date")
        t = pick(event, "time")
        if d:
            raw = f"{d}T{t or '00:00:00'}"
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "na", "-", "pending", "not yet published"}:
        return None
    return text


def source_events(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("events", "data", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = value.get("events") or value.get("items")
            if isinstance(nested, list):
                return nested
    return []


def event_impact(event: dict) -> str | None:
    value = str(pick(event, "impact", "importance", "impact_level") or "").lower()
    if value in {"high", "3", "red"}:
        return "high"
    if value in {"medium", "med", "mid", "2", "orange"}:
        return "medium"
    return None


def gold_relevant(event_name: str, currency: str, impact: str) -> bool:
    if currency != "USD" or impact not in {"high", "medium"}:
        return False
    return any(rx.search(event_name) for rx in GOLD_RE)


def stable_id(event: dict, dt: datetime, currency: str | None, name: str) -> str:
    raw = pick(event, "id", "event_id", "series_id", "url")
    if raw:
        base = f"source:{raw}"
    else:
        base = "|".join([
            dt.isoformat(),
            currency or "",
            re.sub(r"\s+", " ", name).strip().lower(),
        ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


def fetch_json(url: str) -> object:
    req = Request(url, headers={"User-Agent": "LQX-Trader-Economic-Calendar/1.0"})
    with urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    now = utc_now()
    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    ws, we = week_bounds(local_now.date())
    # Fetch the current week plus tomorrow so the Tomorrow tab still works on Sunday.
    fetch_to = we + timedelta(days=1)
    query = urlencode({"from": ws.isoformat(), "to": fetch_to.isoformat(), "limit": 500})
    url = f"{SOURCE_URL}?{query}"

    try:
        payload = fetch_json(url)
    except Exception as exc:
        print(f"Economic calendar source unavailable: {exc}", file=sys.stderr)
        print("Keeping the existing JSON file unchanged.", file=sys.stderr)
        return 0

    events = []
    seen = set()
    for raw in source_events(payload):
        if not isinstance(raw, dict):
            continue
        dt = parse_dt(raw)
        if not dt:
            continue
        local_dt = dt.astimezone(ZoneInfo(TIMEZONE))
        # Only keep current week + tomorrow.
        if local_dt.date() < ws or local_dt.date() > fetch_to:
            continue
        impact = event_impact(raw)
        if impact not in {"high", "medium"}:
            continue
        currency = infer_currency(raw)
        if currency not in {"USD", "EUR"}:
            continue
        name = clean_value(pick(raw, "name", "title", "event", "eventName", "report_name"))
        if not name:
            continue
        eid = stable_id(raw, dt, currency, name)
        if eid in seen:
            continue
        seen.add(eid)
        events.append({
            "id": eid,
            "date": local_dt.date().isoformat(),
            "time": local_dt.strftime("%H:%M"),
            "datetimeUtc": dt.isoformat().replace("+00:00", "Z"),
            "currency": currency,
            "market": "Gold" if gold_relevant(name, currency, impact) else currency,
            "goldRelevant": gold_relevant(name, currency, impact),
            "event": name,
            "impact": impact,
            "previous": clean_value(pick(raw, "previous", "prior", "previous_value")),
            "forecast": clean_value(pick(raw, "forecast", "consensus", "consensus_forecast")),
            "actual": clean_value(pick(raw, "actual", "result", "released")),
            "sourceUrl": clean_value(pick(raw, "url", "link")),
        })

    events.sort(key=lambda x: (x["datetimeUtc"], 0 if x["impact"] == "high" else 1, x["event"]))
    output = {
        "schemaVersion": 1,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "timezone": TIMEZONE,
        "weekStart": ws.isoformat(),
        "weekEnd": we.isoformat(),
        "fetchEnd": fetch_to.isoformat(),
        "source": SOURCE_NAME,
        "sourceUrl": SOURCE_ATTRIBUTION_URL,
        "events": events,
        "goldFilter": {
            "mode": "narrow-usd-macro",
            "description": "USD macro releases with a direct rates, real-yield or Federal Reserve channel to gold.",
            "patterns": GOLD_EVENT_PATTERNS,
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUTPUT)
    print(f"Wrote {len(events)} events to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
