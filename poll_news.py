#!/usr/bin/env python3
"""Poll the official OSRS news feed and push new posts to ntfy.

Runs as a cloud cron (GitHub Actions). State lives in seen.json, committed back to
the repo each run — that's how it remembers what it has already pushed. The first
run (no seen.json yet) seeds silently so the existing backlog isn't blasted out.

Zero third-party deps — stdlib only, so the Action needs no pip install.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_URL = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=1"
SEEN_PATH = Path(__file__).resolve().parent / "seen.json"
NTFY_URL = "https://ntfy.sh/"
UA = "osrs-news-watch/1.0 (personal; +https://github.com/Mango-Punch/osrs-news-watch)"
MAX_SEEN = 200  # feed only carries ~20; keep a buffer, drop the ancient tail


def fetch_feed() -> bytes:
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse_items(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    out: list[dict] = []
    for it in root.findall("./channel/item"):
        def txt(tag: str) -> str:
            el = it.find(tag)
            return (el.text or "").strip() if el is not None else ""

        guid = txt("guid") or txt("link")
        if not guid:
            continue
        out.append({
            "guid": guid,
            "title": txt("title"),
            "link": txt("link"),
            "date": txt("pubDate"),
            "category": txt("category"),
            "desc": txt("description"),
        })
    return out


def load_seen() -> list[str] | None:
    """None => no state file yet (first run). [] or [...] => prior state."""
    if not SEEN_PATH.exists():
        return None
    try:
        data = json.loads(SEEN_PATH.read_text())
        return data if isinstance(data, list) else None
    except Exception:
        return None


def save_seen(guids: list[str]) -> None:
    SEEN_PATH.write_text(json.dumps(guids[-MAX_SEEN:], indent=2) + "\n")


def push(topic: str, item: dict) -> bool:
    payload = {
        "topic": topic,
        "title": (item["title"] or "New OSRS post")[:200],
        "message": "\n".join(p for p in (item["category"], item["date"], item["desc"][:300]) if p) or "Tap to open",
        "tags": ["newspaper"],
    }
    if item["link"]:
        payload["click"] = item["link"]
    req = urllib.request.Request(
        NTFY_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"  push failed for {item['guid']}: {e}", file=sys.stderr)
        return False


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set — refusing to run", file=sys.stderr)
        return 1

    try:
        items = parse_items(fetch_feed())
    except Exception as e:  # noqa: BLE001 — transient feed/network hiccup, don't fail the run
        print(f"feed fetch/parse failed (transient): {e}", file=sys.stderr)
        return 0

    current = [it["guid"] for it in items]
    seen = load_seen()

    if seen is None:
        save_seen(current)
        print(f"first run — seeded {len(current)} existing posts, pushed nothing")
        return 0

    seen_set = set(seen)
    # reversed() => oldest-first, so a burst of posts pings in chronological order
    new_items = [it for it in reversed(items) if it["guid"] not in seen_set]
    if not new_items:
        print("no new posts")
        return 0

    pushed: list[str] = []
    for it in new_items:
        if push(topic, it):
            pushed.append(it["guid"])
            print(f"pushed: {it['title']}")
        else:
            print(f"not pushed (retry next run): {it['title']}")

    if pushed:
        save_seen(seen + pushed)  # only mark what actually sent
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
