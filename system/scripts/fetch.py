#!/usr/bin/env python3
import argparse
import html
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
import subprocess
import json

import feedparser
import requests
import yaml
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[2]
ISSUES_DIR = ROOT / "News"
SOURCES_PATH = ROOT / "system" / "sources.yaml"

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

MAGAZINE_IMAGE_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ec/FPV_Quadrocopter.jpg/1280px-FPV_Quadrocopter.jpg"
)
MAGAZINE_IMAGE_CREDIT = "FPV Quadrocopter by Clicklabs (CC BY-SA 4.0)"

GEAR_KEYWORDS = [
    "frame",
    "motor",
    "esc",
    "flight controller",
    "fc",
    "goggles",
    "vtx",
    "antenna",
    "receiver",
    "rx",
    "tx",
    "lipo",
    "battery",
    "charger",
    "firmware",
    "betaflight",
    "expresslrs",
    "elrs",
    "walksnail",
    "hdzero",
    "dji",
    "o3",
    "launch",
    "release",
    "announces",
    "announcement",
    "new product",
    "version",
    "v2",
    "v3",
    "v4",
    "acquisition",
    "acquires",
    "merger",
    "partnership",
]

GEAR_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in GEAR_KEYWORDS) + r")\b", re.I)

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "your",
    "you",
    "are",
    "our",
    "their",
    "they",
    "new",
    "best",
    "review",
    "guide",
    "how",
    "why",
    "what",
    "when",
    "where",
    "about",
    "vs",
    "using",
    "build",
    "fpv",
    "drone",
    "drones",
    "whoop",
    "cinewhoop",
    "racing",
    "freestyle",
}

CREW_FALLBACKS = [
    "Joshua Bardwell",
    "Rotor Riot",
    "Mr Steele",
    "Le Drib",
]

QUICK_FIXES = [
    "Re‑tension your prop screws and motor bolts — tiny looseness equals big vibes mid‑air.",
    "Do a 30‑second gyro calibration before the first pack; it saves a whole day of wobbles.",
    "Check your VTX antenna before power‑up to avoid a silent range killer.",
    "If your quad drifts, verify your arm bends and re‑level the FC — it’s often hardware, not PID.",
    "Bind/range‑check your RX after any firmware update — it’s the fastest sanity check.",
    "Clean your lenses (camera + goggles) — it’s free “HD” and better lines.",
]

FLIGHT_MOODS = [
    "Low and smooth — paint the lines.",
    "Fast, tight, and fearless.",
    "Quiet freestyle with big flow.",
    "Crisp turns, clean landings.",
    "Send it, then butter it.",
    "Chill cruise, sharp tricks.",
]


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
    resp = requests.get(url, headers=headers, timeout=(5, 20))
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


def is_gear_related(text: str) -> bool:
    if not text:
        return False
    return bool(GEAR_RE.search(text))


def normalize_summary(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
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
        return None
    if not published_dt.tzinfo:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    summary = normalize_summary(entry.get("summary", "") or entry.get("description", ""))
    if summary.lower().startswith(title.lower()):
        summary = summary[len(title) :].lstrip(" -:—")
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


def short_summary(text: str, max_len: int = 160) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 3].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]
    return trimmed + "..."


def mini_article(text: str, max_len: int = 360) -> str:
    if not text:
        return ""
    text = normalize_summary(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    picked = " ".join(s for s in sentences[:2] if s)
    if not picked:
        picked = text
    return short_summary(picked, max_len=max_len)


def render_magazine(items: List[Item], date_str: str) -> str:
    lines = [
        f"# Your FPV Daily News — {date_str}",
        "",
        "_A clean, daily FPV magazine with the best stories, gear updates, and videos — in plain language._",
        "",
        f"![FPV racing quadcopter]({MAGAZINE_IMAGE_URL})",
        "",
        "---",
        "",
        "## At a Glance",
        "",
        "- Top Stories (fast read, clear takeaways)",
        "- Gear & Market Watch (new releases, firmware, companies)",
        "- Videos (what to watch today)",
        "",
        "---",
        "",
        "## Editor's Note",
        "",
        "Fresh FPV drops and community highlights, distilled for a quick read — no digging required.",
        "",
    ]

    if not items:
        lines.append("No FPV-related items found today. Check back tomorrow.")
        return "\n".join(lines) + "\n"

    videos = [i for i in items if is_youtube(i.link)]
    news = [i for i in items if not is_youtube(i.link)]
    gear = [i for i in news if is_gear_related(f"{i.title} {i.summary}")]
    general = [i for i in news if i not in gear]

    # Pilot's Pick: freshest non-video item with a solid summary
    pick_candidates = [i for i in news if len(i.summary) >= 80]
    pilots_pick = pick_candidates[0] if pick_candidates else (news[0] if news else None)

    # This Week's Trend: top repeated keywords
    words = []
    for item in items:
        text = f"{item.title} {item.summary}".lower()
        text = re.sub(r"[^a-z0-9\s-]", " ", text)
        for w in text.split():
            if len(w) < 4 or w in STOPWORDS:
                continue
            words.append(w)
    trend_words = [w for w, _ in Counter(words).most_common(5)]

    def render_section(title: str, section_items: List[Item], max_items: int) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not section_items:
            lines.append("No items today.")
            lines.append("")
            return
        for item in section_items[:max_items]:
            published = item.published[:10]
            summary = mini_article(item.summary)
            lines.append(f"### {item.title}")
            lines.append(f"_Source: {item.source} · {published}_")
            lines.append("")
            if summary:
                lines.append(f"**Mini‑article:** {summary}")
            else:
                lines.append("**Mini‑article:** No summary available in the feed.")
            lines.append("")
            lines.append(f"_Read more:_ {item.link}")
            lines.append("")

    # Crew Shoutout (pilot/channel spotlight)
    crew_candidates = [s.name.replace(" (YouTube)", "") for s in load_sources() if "youtube.com" in s.url]
    if not crew_candidates:
        crew_candidates = CREW_FALLBACKS
    crew_pick = crew_candidates[abs(hash(date_str)) % len(crew_candidates)]

    lines.append("## Crew Shoutout")
    lines.append("")
    lines.append(f"Today’s spotlight: **{crew_pick}** — go show some love and steal a new line.")
    lines.append("")

    # Quick Fix
    quick_fix = QUICK_FIXES[abs(hash(date_str + "fix")) % len(QUICK_FIXES)]
    lines.append("## Quick Fix")
    lines.append("")
    lines.append(f"{quick_fix}")
    lines.append("")

    # Flight Mood
    flight_mood = FLIGHT_MOODS[abs(hash(date_str + "mood")) % len(FLIGHT_MOODS)]
    lines.append("## Flight Mood")
    lines.append("")
    lines.append(f"**{flight_mood}**")
    lines.append("")

    if pilots_pick:
        lines.append("## Pilot’s Pick")
        lines.append("")
        lines.append(f"### {pilots_pick.title}")
        lines.append(f"_Source: {pilots_pick.source} · {pilots_pick.published[:10]}_")
        lines.append("")
        lines.append(f"**Why it’s worth your time:** {mini_article(pilots_pick.summary, max_len=420)}")
        lines.append("")
        lines.append(f"_Read more:_ {pilots_pick.link}")
        lines.append("")

    lines.append("## Fast Facts")
    lines.append("")
    lines.append(f"- Total items scanned: {len(items)}")
    lines.append(f"- Top Stories: {min(6, len(general))} · Gear: {min(4, len(gear))} · Videos: {min(4, len(videos))}")
    if trend_words:
        lines.append(f"- This Week’s Trend keywords: {', '.join(trend_words)}")
    lines.append("")

    lines.append("## This Week’s Trend")
    lines.append("")
    if trend_words:
        lines.append(f"Across sources, the most repeated topics are **{', '.join(trend_words)}**.")
    else:
        lines.append("Not enough data today to detect a clear trend.")
    lines.append("")

    render_section("Top Stories", general, 6)
    render_section("Gear & Market Watch", gear, 4)
    render_section("Videos", videos, 4)

    lines.append("---")
    lines.append("")
    lines.append("_More tomorrow. Fly safe and keep it smooth._")
    lines.append("")
    lines.append(f"_Image credit:_ {MAGAZINE_IMAGE_CREDIT}")
    lines.append("")

    return "\n".join(lines) + "\n"


def fetch_youtube_items(source: FeedSource, max_items: int = 6) -> List[Item]:
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--ignore-errors",
                "--no-warnings",
                "--skip-download",
                "--extractor-retries",
                "1",
                "--socket-timeout",
                "10",
                "--playlist-end",
                str(max_items),
                source.url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:
        raise RuntimeError(f"yt-dlp failed: {exc}") from exc

    items: List[Item] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = (data.get("title") or "").strip()
        link = (data.get("webpage_url") or "").strip()
        if not title or not link:
            continue
        ts = data.get("timestamp")
        if ts is None and data.get("upload_date"):
            try:
                ts = datetime.strptime(data["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                ts = None
        if ts is None:
            continue
        summary = normalize_summary(data.get("description") or "")
        items.append(
            Item(
                title=title,
                link=link,
                source=source.name,
                published=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                published_ts=float(ts),
                summary=summary,
            )
        )
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today UTC)")
    parser.add_argument("--days", type=int, default=14)
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
            if "youtube.com" in source.url:
                items.extend(fetch_youtube_items(source))
                continue
            feed = fetch_feed(source.url)
        except Exception as exc:
            print(f"Failed to fetch {source.url}: {exc}", file=sys.stderr)
            continue

        for entry in feed.entries[:50]:
            if not should_include(source, entry):
                continue
            item = item_from_entry(source, entry)
            if item:
                items.append(item)

    items = dedupe(items)
    items.sort(key=lambda i: i.published_ts, reverse=True)
    cutoff = datetime.combine(date_obj, datetime.min.time(), tzinfo=timezone.utc).timestamp() - (
        args.days * 24 * 60 * 60
    )
    items = [i for i in items if i.published_ts >= cutoff]

    latest_md = render_magazine(items, date_str)
    issue_dir = ISSUES_DIR / date_str
    issue_dir.mkdir(parents=True, exist_ok=True)
    issue_md_path = issue_dir / "README.md"
    issue_md_path.write_text(latest_md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
