#!/usr/bin/env python3
"""Poll OSRS news and push to ntfy — new posts AND material edits to recent posts.

Two watchers, one state file:

1. **New posts** — official OSRS RSS feed. Any GUID not in state gets pushed.
2. **Post edits** — the OSRS Wiki mirrors every news post in its `Update:`
   namespace (ns 112) within minutes, and Jagex routinely *amends* live blogs
   (e.g. Summer Sweep-Up 2026 grew +13.9k chars five days after posting — a
   full "Changes" section the RSS feed never showed). We poll the wiki
   `recentchanges` API for ns-112 edits and push when an edit is:
     - **big enough**: |size delta| >= EDIT_MIN_DELTA bytes (filters the
       constant stream of wiki-editor formatting/image touch-ups), and
     - **to a recent post**: page title matches a title currently in the RSS
       feed (~20 posts), or — because Jagex's RSS titles/slugs drift from wiki
       page names — the page's own `{{Update|date=...}}` says the post is
       <= EDIT_MAX_POST_AGE_DAYS old (one extra API call, only for big edits
       that failed the cheap title match). Old-post wiki gardening never alerts.
   Known gap (accepted): several consecutive sub-threshold edits that sum past
   the threshold won't alert.

Runs as a cloud cron (GitHub Actions). State lives in seen.json, committed back
to the repo each run. v1 state was a bare list of feed GUIDs; v2 is a dict —
`load_state` migrates a v1 list transparently (migration run pushes any
qualifying edits already inside the lookback window — at most a handful).
First-ever run (no seen.json) seeds silently so the backlog isn't blasted out.

Zero third-party deps — stdlib only, so the Action needs no pip install.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEED_URL = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=1"
WIKI_API = "https://oldschool.runescape.wiki/api.php"
UPDATE_NAMESPACE = 112
EDIT_MIN_DELTA = 500       # bytes; |newlen - oldlen| below this = wiki gardening, ignore
EDIT_LOOKBACK_HOURS = 48   # recentchanges window; revid dedupe makes overlap safe
EDIT_MAX_POST_AGE_DAYS = 60  # fallback recency check via the page's own date param
SEEN_PATH = Path(__file__).resolve().parent / "seen.json"
NTFY_URL = "https://ntfy.sh/"
UA = "osrs-news-watch/2.0 (personal; +https://github.com/Mango-Punch/osrs-news-watch)"
MAX_SEEN = 200       # feed only carries ~20; keep a buffer, drop the ancient tail
MAX_EDIT_REVIDS = 500


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
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


def norm_title(title: str) -> str:
    """Match feed titles to wiki page titles despite punctuation drift."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def fetch_recent_edits() -> list[dict]:
    """ns-112 edits in the lookback window, oldest first."""
    since = datetime.now(timezone.utc) - timedelta(hours=EDIT_LOOKBACK_HOURS)
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcnamespace": UPDATE_NAMESPACE,
        "rctype": "edit",
        "rcend": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rcprop": "title|ids|timestamp|sizes",
        "rclimit": 100,
        "format": "json",
    }
    raw = http_get(f"{WIKI_API}?{urllib.parse.urlencode(params)}")
    changes = json.loads(raw).get("query", {}).get("recentchanges", [])
    return list(reversed(changes))


def fetch_page_meta(title: str) -> dict:
    """date + official url from the page's {{Update|...}} template params."""
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": title,
        "format": "json",
        "formatversion": 2,
    }
    raw = http_get(f"{WIKI_API}?{urllib.parse.urlencode(params)}")
    page = json.loads(raw)["query"]["pages"][0]
    wikitext = page["revisions"][0]["slots"]["main"]["content"]
    out: dict = {}
    m = re.search(r"\|\s*date\s*=\s*([^|}\n]+)", wikitext)
    if m:
        try:
            out["date"] = datetime.strptime(m.group(1).strip(), "%d %B %Y").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    m = re.search(r"\|\s*url\s*=\s*([^|}\s]+)", wikitext)
    if m:
        out["url"] = m.group(1).strip()
    return out


def load_state() -> dict | None:
    """None => no state file yet (first run). Migrates v1 bare-list state."""
    if not SEEN_PATH.exists():
        return None
    try:
        data = json.loads(SEEN_PATH.read_text())
    except Exception:
        return None
    if isinstance(data, list):  # v1
        return {"feed_guids": data, "edit_revids": []}
    if isinstance(data, dict) and "feed_guids" in data:
        data.setdefault("edit_revids", [])
        return data
    return None


def save_state(state: dict) -> None:
    out = {
        "feed_guids": state["feed_guids"][-MAX_SEEN:],
        "edit_revids": state["edit_revids"][-MAX_EDIT_REVIDS:],
    }
    SEEN_PATH.write_text(json.dumps(out, indent=2) + "\n")


def push(topic: str, payload: dict) -> bool:
    payload = {"topic": topic, **payload}
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
        print(f"  push failed: {e}", file=sys.stderr)
        return False


def new_post_payload(item: dict) -> dict:
    payload = {
        "title": (item["title"] or "New OSRS post")[:200],
        "message": "\n".join(
            p for p in (item["category"], item["date"], item["desc"][:300]) if p
        ) or "Tap to open",
        "tags": ["newspaper"],
    }
    if item["link"]:
        payload["click"] = item["link"]
    return payload


def edit_payload(edit: dict, feed_item: dict | None) -> dict:
    title = edit["title"].removeprefix("Update:")
    delta = edit.get("newlen", 0) - edit.get("oldlen", 0)
    return {
        "title": f"Updated: {title}"[:200],
        "message": f"blog post edited ({delta:+,} chars) · {edit.get('timestamp', '')}",
        "tags": ["pencil"],
        "click": (feed_item or {}).get("link")
        or "https://oldschool.runescape.wiki/w/"
        + urllib.parse.quote(edit["title"].replace(" ", "_")),
    }


def check_new_posts(topic: str, items: list[dict], state: dict) -> None:
    seen_set = set(state["feed_guids"])
    # reversed() => oldest-first, so a burst of posts pings in chronological order
    new_items = [it for it in reversed(items) if it["guid"] not in seen_set]
    if not new_items:
        print("no new posts")
        return
    for it in new_items:
        if push(topic, new_post_payload(it)):
            state["feed_guids"].append(it["guid"])  # only mark what actually sent
            print(f"pushed new: {it['title']}")
        else:
            print(f"not pushed (retry next run): {it['title']}")


def check_post_edits(topic: str, items: list[dict], state: dict) -> None:
    try:
        edits = fetch_recent_edits()
    except Exception as e:  # noqa: BLE001 — transient wiki/network hiccup
        print(f"wiki recentchanges fetch failed (transient): {e}", file=sys.stderr)
        return

    feed_by_norm = {norm_title(it["title"]): it for it in items if it["title"]}
    pushed_revids = set(state["edit_revids"])
    recency_cache: dict[str, dict | None] = {}  # wiki title -> page meta if recent, else None

    def is_recent_post(e: dict) -> bool:
        """Cheap title match against the feed, else the page's own date param."""
        if norm_title(e["title"].removeprefix("Update:")) in feed_by_norm:
            return True
        if e["title"] not in recency_cache:
            try:
                meta = fetch_page_meta(e["title"])
            except Exception as exc:  # noqa: BLE001 — fail closed, retry next run
                print(f"  page meta fetch failed for {e['title']}: {exc}", file=sys.stderr)
                return False
            fresh = "date" in meta and (
                datetime.now(timezone.utc) - meta["date"]
            ) <= timedelta(days=EDIT_MAX_POST_AGE_DAYS)
            recency_cache[e["title"]] = meta if fresh else None
        return recency_cache[e["title"]] is not None

    def qualifies(e: dict) -> bool:
        return (
            bool(e.get("revid"))
            and e["revid"] not in pushed_revids
            and abs(e.get("newlen", 0) - e.get("oldlen", 0)) >= EDIT_MIN_DELTA
            and is_recent_post(e)
        )

    # Biggest qualifying edit per page this run, so an edit burst pings once
    best: dict[str, dict] = {}
    for e in edits:
        if not qualifies(e):
            continue
        key = norm_title(e["title"].removeprefix("Update:"))
        cur = best.get(key)
        if cur is None or abs(e["newlen"] - e["oldlen"]) > abs(cur["newlen"] - cur["oldlen"]):
            best[key] = e

    if not best:
        print("no material post edits")
        return

    for key, e in best.items():
        # Mark every qualifying revid of the page's burst, not just the one
        # pushed, so the next run doesn't re-alert the rest of the burst
        burst = [
            c["revid"] for c in edits
            if qualifies(c) and norm_title(c["title"].removeprefix("Update:")) == key
        ]
        feed_item = feed_by_norm.get(key)
        if feed_item is None:
            meta = recency_cache.get(e["title"]) or {}
            feed_item = {"link": meta["url"]} if meta.get("url") else None
        if push(topic, edit_payload(e, feed_item)):
            state["edit_revids"].extend(burst)
            print(f"pushed edit: {e['title']} ({e['newlen'] - e['oldlen']:+,} chars)")
        else:
            print(f"edit not pushed (retry next run): {e['title']}")


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set — refusing to run", file=sys.stderr)
        return 1

    try:
        items = parse_items(http_get(FEED_URL))
    except Exception as e:  # noqa: BLE001 — transient feed/network hiccup, don't fail the run
        print(f"feed fetch/parse failed (transient): {e}", file=sys.stderr)
        return 0

    state = load_state()
    if state is None:
        save_state({"feed_guids": [it["guid"] for it in items], "edit_revids": []})
        print(f"first run — seeded {len(items)} existing posts, pushed nothing")
        return 0

    check_new_posts(topic, items, state)
    check_post_edits(topic, items, state)
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
