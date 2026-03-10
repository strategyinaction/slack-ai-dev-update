"""
SIA – AI Development Update Pipeline.
Ingests AI coding news from multiple sources, filters via OpenAI LLM,
and dispatches a formatted digest to Slack.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")
MAX_AGGREGATED_ITEMS = 100
REQUEST_TIMEOUT = 15
USER_AGENT = "SIA-AI-Dev-Update/1.0"

LLM_SYSTEM_PROMPT = (
    "You are a technical filter. Evaluate the provided updates. "
    "Discard general AI news, consumer wrappers, and standard LLM model releases. "
    "Isolate the top 5 critical updates strictly related to AI-integrated IDEs "
    "(Cursor, Antigravity), or LLM coding plugins (Claude Code). "
    "Output a maximum of 5 items. For each, write a single-sentence highly technical "
    "summary of the architectural change or feature. Do not use filler words. "
    "Output strictly as a JSON array of objects: "
    '[{"title": "...", "url": "...", "summary": "..."}].'
)


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

def load_sources(path: str = SOURCES_PATH) -> dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 2. Data Ingestion
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None) -> requests.Response | None:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        logger.warning("HTTP request failed for %s: %s", url, exc)
        return None


def _cutoff_timestamp() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=28)


def _parse_feed_entries(url: str, source_label: str) -> list[dict]:
    resp = _http_get(url)
    if resp is None:
        return []
    feed = feedparser.parse(resp.text)
    cutoff = _cutoff_timestamp()
    items: list[dict] = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        entry_dt = datetime(*published[:6], tzinfo=timezone.utc)
        if entry_dt < cutoff:
            continue
        items.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "source": source_label,
            "content_snippet": (entry.get("summary") or "")[:300],
        })
    return items


def ingest_github_releases(repos: list[str]) -> list[dict]:
    results: list[dict] = []
    for repo in repos:
        url = f"https://github.com/{repo}/releases.atom"
        results.extend(_parse_feed_entries(url, f"GitHub:{repo}"))
    return results


def ingest_rss_feeds(urls: list[str]) -> list[dict]:
    results: list[dict] = []
    for url in urls:
        results.extend(_parse_feed_entries(url, "RSS"))
    return results


def ingest_reddit_searches(urls: list[str]) -> list[dict]:
    results: list[dict] = []
    for url in urls:
        results.extend(_parse_feed_entries(url, "Reddit"))
    return results


def ingest_hacker_news(queries: list[str]) -> list[dict]:
    cutoff_ts = int(_cutoff_timestamp().timestamp())
    results: list[dict] = []
    for query in queries:
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>{cutoff_ts}",
            "hitsPerPage": 10,
        }
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("HN query '%s' failed: %s", query, exc)
            continue
        for hit in data.get("hits", []):
            results.append({
                "title": hit.get("title", ""),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                "source": "HackerNews",
                "content_snippet": (hit.get("story_text") or hit.get("title", ""))[:300],
            })
    return results


INGEST_ROUTER: dict[str, Any] = {
    "github_releases": ingest_github_releases,
    "rss_feeds": ingest_rss_feeds,
    "reddit_searches": ingest_reddit_searches,
    "hn_queries": ingest_hacker_news,
}


def aggregate(sources: dict[str, Any]) -> list[dict]:
    all_items: list[dict] = []
    for key, value in sources.items():
        handler = INGEST_ROUTER.get(key)
        if handler:
            logger.info("Ingesting source: %s (%d targets)", key, len(value))
            all_items.extend(handler(value))

    seen_urls: set[str] = set()
    deduplicated: list[dict] = []
    for item in all_items:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduplicated.append(item)

    logger.info("Aggregated %d unique items (capped at %d)", len(deduplicated), MAX_AGGREGATED_ITEMS)
    return deduplicated[:MAX_AGGREGATED_ITEMS]


# ---------------------------------------------------------------------------
# 3. LLM Processing
# ---------------------------------------------------------------------------

def filter_with_llm(items: list[dict], api_key: str) -> list[dict]:
    if not items:
        logger.info("No items to filter, skipping LLM call")
        return []

    client = OpenAI(api_key=api_key)
    payload = json.dumps(items, indent=2)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return []

    try:
        text = response.choices[0].message.content.strip()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
        return []
    except (json.JSONDecodeError, AttributeError, IndexError) as exc:
        logger.error("Failed to parse LLM response: %s — raw: %s", exc, text[:500])
        return []


# ---------------------------------------------------------------------------
# 4. Distribution (Slack)
# ---------------------------------------------------------------------------

def build_slack_blocks(filtered_items: list[dict]) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🤖 SIA – AI Dev Update • {today}", "emoji": True},
        },
        {"type": "divider"},
    ]

    if not filtered_items:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No critical AI coding updates in the last 24 hours._"},
        })
        return blocks

    for item in filtered_items:
        title = item.get("title", "Untitled")
        summary = item.get("summary", "No summary available.")
        url = item.get("url", "")
        link = f"<{url}|Source Link>" if url else ""
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}*\n{summary}\n{link}",
            },
        })
        blocks.append({"type": "divider"})

    return blocks


def send_to_slack(webhook_url: str, blocks: list[dict]) -> None:
    payload = {"blocks": blocks}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        logger.info("Slack message sent successfully")
    except requests.RequestException as exc:
        logger.error("Slack delivery failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# 5. Orchestrator
# ---------------------------------------------------------------------------

def run() -> None:
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not openai_api_key:
        logger.error("OPENAI_API_KEY is not set")
        sys.exit(1)
    if not slack_webhook_url:
        logger.error("SLACK_WEBHOOK_URL is not set")
        sys.exit(1)

    sources = load_sources()
    raw_items = aggregate(sources)
    filtered = filter_with_llm(raw_items, openai_api_key)
    blocks = build_slack_blocks(filtered)
    send_to_slack(slack_webhook_url, blocks)


if __name__ == "__main__":
    run()
