#!/usr/bin/env python3
import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import feedparser
import requests
import yaml
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ISSUES_DIR = ROOT / "issues"
SOURCES_PATH = ROOT / "sources.yaml"
README_PATH = ROOT / "README.md"

KEYWORDS = [
    "fpv",
    "first person view",
    "freestyle",
    "racing",
    "cinewhoop",
    "whoop",
    "micro whoop",
    "quad",
    "quadcopter",
    "goggles",
    "vtx",
    "betaflight",
    "expresslrs",
    "elrs",
    "walksnail",
    "hdzero",
    "dji fpv",
    "o3",
    "digital fpv",
]

KEYWORD_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.I)


@dataclass
class FeedSource:
    name: str
    url: str
    scope: str  # "fpv" or "general"


@dataclass
class Item:
    title: str
    link: str
    source: str
    published: str
    published_ts: float
    summary: str


def load_sources() -> List[FeedSource]:
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = []
    for entry in data.get("sources", []):
        sources.append(FeedSource(**entry))
    return sources


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    headers = {"User-Agent": "fpv-daily-bot/1.0 (+https://github.com/)"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def parse_date(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    for key in ("published", "updated", "created"):
        if key in entry:
            try:
                return date_parser.parse(entry[key])
            except Exception:
                continue
    if "published_parsed" in entry and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def is_fpv_relevant(text: str) -> bool:
    if not text:
        return False
    return bool(KEYWORD_RE.search(text))


def normalize_summary(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def should_include(source: FeedSource, entry: feedparser.FeedParserDict) -> bool:
    if source.scope == "fpv":
        return True
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    return is_fpv_relevant(f"{title} {summary}")


def item_from_entry(source: FeedSource, entry: feedparser.FeedParserDict) -> Optional[Item]:
    title = entry.get("title", "").strip()
    link = entry.get("link", "").strip()
    if not title or not link:
        return None
    published_dt = parse_date(entry)
    if not published_dt:
        published_dt = datetime.now(timezone.utc)
    if not published_dt.tzinfo:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    summary = normalize_summary(entry.get("summary", "") or entry.get("description", ""))
    return Item(
        title=title,
        link=link,
        source=source.name,
        published=published_dt.astimezone(timezone.utc).isoformat(),
        published_ts=published_dt.timestamp(),
        summary=summary,
    )


def dedupe(items: Iterable[Item]) -> List[Item]:
    seen = set()
    unique = []
    for item in items:
        key = (item.link.lower(), item.title.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def is_youtube(link: str) -> bool:
    return "youtube.com" in link.lower() or "youtu.be" in link.lower()


def short_summary(text: str, max_len: int = 220) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def render_magazine(items: List[Item], date_str: str, limit: int = 30) -> str:
    lines = [
        f"# Your FPV Daily News — {date_str}",
        "",
        "A quick-read FPV magazine: top stories, videos, and community highlights.",
        "",
    ]

    if not items:
        lines.append("No FPV-related items found today. Check back tomorrow.")
        return "\n".join(lines) + "\n"

    videos = [i for i in items if is_youtube(i.link)]
    news = [i for i in items if not is_youtube(i.link)]

    def render_section(title: str, section_items: List[Item]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not section_items:
            lines.append("- No items today.")
            lines.append("")
            return
        for item in section_items[:limit]:
            published = item.published[:10]
            summary = short_summary(item.summary)
            if summary:
                lines.append(f"- [{item.title}]({item.link}) — {item.source} ({published})")
                lines.append(f"  {summary}")
            else:
                lines.append(f"- [{item.title}]({item.link}) — {item.source} ({published})")
        lines.append("")

    render_section("Top Stories", news)
    render_section("Videos", videos)

    return "\n".join(lines) + "\n"


def update_readme(latest_md: str, date_str: str) -> None:
    content = [
        "# your-fpv-daily-news",
        "",
        "Daily FPV drone news digest updated automatically.",
        "",
        f"**Latest digest:** {date_str}",
        "",
        "## Latest Issue",
        "",
        latest_md.strip(),
        "",
        "## How It Works",
        "",
        "- Pulls RSS/Atom feeds from FPV-first sources and broader drone publications.",
        "- Filters general drone feeds for FPV-relevant keywords.",
        "- Writes a dated JSON file plus a magazine-style Markdown issue each day.",
        "",
        "## Run Locally",
        "",
        "```bash",
        "python -m venv .venv",
        "source .venv/bin/activate",
        "pip install -r requirements.txt",
        "python scripts/fetch.py",
        "```",
        "",
        "## Customize",
        "",
        "- Edit `sources.yaml` to add or remove feeds.",
        "- Update the keyword list in `scripts/fetch.py` for tighter filtering.",
        "- Change the GitHub Actions schedule in `.github/workflows/daily.yml`.",
        "",
    ]
    README_PATH.write_text("\n".join(content), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today UTC)")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    if args.date:
        try:
            date_obj = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print("Invalid --date, expected YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        date_obj = datetime.now(timezone.utc).date()

    date_str = date_obj.isoformat()

    sources = load_sources()
    items: List[Item] = []

    for source in sources:
        try:
            feed = fetch_feed(source.url)
        except Exception as exc:
            print(f"Failed to fetch {source.url}: {exc}", file=sys.stderr)
            continue

        for entry in feed.entries:
            if not should_include(source, entry):
                continue
            item = item_from_entry(source, entry)
            if item:
                items.append(item)

    items = dedupe(items)
    items.sort(key=lambda i: i.published_ts, reverse=True)

    payload: Dict[str, Any] = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": [item.__dict__ for item in items],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    daily_json = DATA_DIR / f"{date_str}.json"
    daily_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    latest_json = DATA_DIR / "latest.json"
    latest_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    latest_md = render_magazine(items, date_str, limit=args.limit)
    latest_md_path = DATA_DIR / "latest.md"
    latest_md_path.write_text(latest_md, encoding="utf-8")

    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    issue_md_path = ISSUES_DIR / f"{date_str}.md"
    issue_md_path.write_text(latest_md, encoding="utf-8")

    update_readme(latest_md, date_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
