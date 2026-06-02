# osrs-news-watch

Tiny cloud cron that watches the **official Old School RuneScape news feed** and
**pushes a phone notification (via [ntfy](https://ntfy.sh)) whenever Jagex posts
anything new**. Built so I hear about OSRS update catalysts in the first hours —
without my laptop being on and without opening anything.

Part of the **OSRS Trader** project (the proactive half of its catalyst loop).
It is deliberately *dumb*: it pings on **every** new post and makes no judgement
about relevance — I scan the title myself and dig in if it matters.

## How it works

1. GitHub Actions runs [`poll_news.py`](poll_news.py) on a timer (`*/15` — see caveat).
2. The script reads the [OSRS news RSS feed](https://secure.runescape.com/m=news/latest_news.rss?oldschool=1).
3. It compares against [`seen.json`](seen.json) — the list of posts already pushed.
4. Anything new → one ntfy push (**title · category · date**, tap to open the post).
5. It commits the updated `seen.json` back, so the same post never pings twice.

The very first run seeds `seen.json` with the current backlog and pushes nothing.

No third-party Python deps (stdlib only). No local files from the OSRS Trader repo
are used — that's why it can live entirely in the cloud.

## Setup (already done, recorded here for future me)

- **Push channel:** the ntfy topic is stored as the repo **Actions secret `NTFY_TOPIC`**
  (Settings → Secrets and variables → Actions). It is *never* in the code, so this
  repo can stay public. Subscribe to the same topic in the ntfy app to receive pings.
- **Change the topic / channel:** update the `NTFY_TOPIC` secret; no code change.
- **Repo is public** so GitHub Actions minutes are free and unlimited (a private repo
  caps at 2,000 min/month, which a 15-min cadence would overrun).

## Operating

- **Pause:** Actions tab → this workflow → ⋯ → *Disable workflow*. Re-enable to resume.
- **Test now:** Actions tab → *Run workflow* (manual `workflow_dispatch`). Or push a
  test ping straight to the channel: `curl -d "test" ntfy.sh/<your-topic>`.
- **Change cadence:** edit the `cron:` in [`.github/workflows/poll.yml`](.github/workflows/poll.yml).

## Caveats

- **Timing is best-effort.** GitHub deprioritises scheduled workflows; they often run
  10–30 min late and can skip a slot under load. "Every 15 min" ≈ "every 15–40 min".
  For tighter timing a dedicated cron host (Cloudflare Workers, Val.town) is more precise.
- **60-day inactivity rule.** GitHub disables scheduled workflows on a repo with no
  activity for 60 days. The marker commits + OSRS's regular posting cadence keep this
  one active; if OSRS ever goes quiet for ~2 months, re-enable it from the Actions tab.
- **Public feed only.** Everything pushed is already-public OSRS news, so an
  unauthenticated ntfy topic is fine. If this is ever extended to include private data
  (e.g. my live positions), move to an authenticated/self-hosted channel first.
