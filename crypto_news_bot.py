"""
Telegram crypto news bot.

Polls a news source, dedupes against previously-sent items, posts new ones.
Designed to run as a cron job (stateless between runs) OR as a loop.

Setup:
    pip install requests feedparser
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="-1001234567890"
    export CRYPTOPANIC_TOKEN="..."        # only if SOURCE=cryptopanic
    export CRYPTOPANIC_PLAN="developer"   # verify this in your CP dashboard
    export SOURCE="rss"                   # "rss" or "cryptopanic"

    python crypto_news_bot.py             # one pass
    python crypto_news_bot.py --loop      # poll forever
"""

import html
import json
import os
import pathlib
import sys
import time

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SOURCE = os.environ.get("SOURCE", "rss")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))

# Verify each of these actually returns XML before trusting it.
# Feed URLs move. Pull them from each site's footer.
RSS_FEEDS = [
    # "https://example-crypto-site.com/rss",
]

STATE_PATH = pathlib.Path(os.environ.get("STATE_PATH", "seen.json"))
MAX_SEEN = 500          # ring buffer, keeps state file from growing forever
MAX_PER_RUN = 5         # don't dump 40 posts into the channel at once
TG_LIMIT = 4096         # Telegram hard cap on message length


# ---------- state ----------

def load_seen() -> list:
    if not STATE_PATH.exists():
        return []
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(seen: list) -> None:
    STATE_PATH.write_text(json.dumps(seen[-MAX_SEEN:]))


# ---------- sources ----------
# Each returns: [{"id": str, "title": str, "url": str, "source": str}, ...]

def fetch_cryptopanic() -> list:
    plan = os.environ.get("CRYPTOPANIC_PLAN", "developer")
    resp = requests.get(
        f"https://cryptopanic.com/api/{plan}/v2/posts/",
        params={
            "auth_token": os.environ["CRYPTOPANIC_TOKEN"],
            "public": "true",
            "kind": "news",
        },
        timeout=20,
    )
    resp.raise_for_status()
    items = []
    for post in resp.json().get("results", []):
        items.append({
            "id": str(post.get("id")),
            "title": post.get("title", "").strip(),
            # v2 sometimes nests the outbound link; fall back to the CP permalink
            "url": post.get("original_url") or post.get("url", ""),
            "source": (post.get("source") or {}).get("title", "CryptoPanic"),
        })
    return items


def fetch_rss() -> list:
    import feedparser

    items = []
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"feed failed {feed_url}: {exc}", file=sys.stderr)
            continue
        site = parsed.feed.get("title", feed_url)
        for entry in parsed.entries[:20]:
            link = entry.get("link", "")
            if not link:
                continue
            items.append({
                "id": entry.get("id") or link,
                "title": entry.get("title", "").strip(),
                "url": link,
                "source": site,
            })
    return items


# ---------- telegram ----------

def send(text: str) -> None:
    """HTML parse_mode, not MarkdownV2. MarkdownV2 requires escaping 18
    different characters and crypto headlines are full of them."""
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text[:TG_LIMIT],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"send failed {resp.status_code}: {resp.text}", file=sys.stderr)


def format_item(item: dict) -> str:
    title = html.escape(item["title"])
    source = html.escape(item["source"])
    url = html.escape(item["url"], quote=True)
    return f'<b>{title}</b>\n<a href="{url}">{source}</a>'


# ---------- main ----------

def run_once() -> None:
    seen = load_seen()
    seen_set = set(seen)

    items = fetch_cryptopanic() if SOURCE == "cryptopanic" else fetch_rss()
    fresh = [i for i in items if i["id"] not in seen_set and i["title"]]

    # oldest first so the channel reads chronologically
    for item in reversed(fresh[:MAX_PER_RUN]):
        send(format_item(item))
        seen.append(item["id"])
        time.sleep(3)   # stay well under Telegram's per-chat send rate

    save_seen(seen)
    print(f"{len(items)} fetched, {len(fresh)} new, {min(len(fresh), MAX_PER_RUN)} sent")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as exc:
                print(f"run failed: {exc}", file=sys.stderr)
            time.sleep(POLL_SECONDS)
    else:
        run_once()
