"""Producer (מפיק) attribution for GoOut events.

GoOut's public event page embeds `ProducersName` in its __NEXT_DATA__ blob — no
login needed. We cache it per goOutEventId in `goout_producers` (never touching
`parties`) and rank producers by tickets/commission from `goout_sales_log`,
so we can see who we sell the most for and negotiate better deals.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_UA = {"User-Agent": "Mozilla/5.0"}


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def extract_producer(html: str) -> str | None:
    m = _NEXT.search(html)
    if not m:
        return None
    try:
        name = _find_key(json.loads(m.group(1)), "ProducersName")
    except ValueError:
        return None
    return name.strip() if isinstance(name, str) and name.strip() else None


def fetch_producer(url: str) -> tuple[str | None, str | None]:
    """Returns (producer, error)."""
    resp = requests.get(url.split("?")[0], headers=_UA, timeout=20)
    if resp.status_code != 200:
        return None, f"http {resp.status_code}"
    producer = extract_producer(resp.text)
    return producer, (None if producer else "no ProducersName")


def ensure_producers(db, refetch_all: bool = False, delay: float = 0.4, max_events: int | None = None) -> dict:
    """Fill `goout_producers` for parties that don't have a producer yet."""
    done = set()
    if not refetch_all:
        done = {d["_id"] for d in db.goout_producers.find({"producer": {"$ne": None}}, {"_id": 1})}
    ok = failed = 0
    parties = db.parties.find(
        {"goOutEventId": {"$exists": True}},
        {"goOutEventId": 1, "goOutUrl": 1, "originalUrl": 1, "name": 1},
    )
    for p in parties:
        eid = str(p["goOutEventId"])
        if eid in done:
            continue
        url = p.get("goOutUrl") or p.get("originalUrl") or ""
        if "go-out.co/event/" not in url:
            continue
        if max_events is not None and ok + failed >= max_events:
            break
        try:
            producer, err = fetch_producer(url)
        except Exception as exc:
            producer, err = None, repr(exc)
        db.goout_producers.update_one(
            {"_id": eid},
            {"$set": {"producer": producer, "error": err, "party_name": p.get("name"), "checked_at": time.time()}},
            upsert=True,
        )
        if producer:
            ok += 1
        else:
            failed += 1
        time.sleep(delay)
    logger.info(f"ensure_producers: {ok} resolved, {failed} failed")
    return {"resolved": ok, "failed": failed}


def get_producer_report(db, limit: int = 15) -> dict:
    """Rank producers by tickets sold through us (goout_sales_log, all history)."""
    producers = {d["_id"]: d.get("producer") for d in db.goout_producers.find()}
    agg = collections.defaultdict(lambda: {"tickets": 0, "revenue": 0.0, "events": set()})
    unknown = 0
    for row in db.goout_sales_log.find():
        tickets = row.get("delta_confirmed") or 0
        eid = str(row.get("go_out_id"))
        name = producers.get(eid)
        if not name:
            unknown += tickets
            continue
        a = agg[name]
        a["tickets"] += tickets
        a["revenue"] += row.get("revenue_earned") or 0
        a["events"].add(eid)
    rows = [
        {"producer": n, "tickets": a["tickets"], "events": len(a["events"]), "revenue": a["revenue"]}
        for n, a in agg.items()
    ]
    rows.sort(key=lambda r: (-r["tickets"], -r["revenue"]))
    return {"rows": rows[:limit], "unknown_tickets": unknown}
