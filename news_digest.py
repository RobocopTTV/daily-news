#!/usr/bin/env python3
"""Daily news digest generator.

Fetches free RSS feeds (Google News + ABC Australia), picks the top items
per category (politics / economy / stocks / tech / AI / sports, with a
US / China / Australia focus), summarizes them in Chinese via the free
Gemini API tier, and renders a single-file styled HTML report.

Usage:
    python news_digest.py                 # full run (needs network; LLM optional)
    python news_digest.py --no-llm        # skip LLM, headlines only
    python news_digest.py --test          # offline smoke test with sample data
    python news_digest.py --out-dir DIR   # output directory (default: reports)

Environment:
    GEMINI_API_KEY   Google AI Studio key (free tier). Optional but strongly
                     recommended — without it the report has no Chinese
                     summaries/commentary, only raw headlines.

Standard library only. No third-party dependencies. (v1.0)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration — edit freely
# --------------------------------------------------------------------------

# Each feed: (category, url, note). Google News search feeds use `when:1d`
# to return only items from the last 24 hours.
FEEDS = [
    ("politics", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en", "world"),
    ("politics", "https://news.google.com/rss/search?q=US+politics+when:1d&hl=en-US&gl=US&ceid=US:en", "us-politics"),
    ("politics", "https://news.google.com/rss/search?q=China+when:1d&hl=en-US&gl=US&ceid=US:en", "china"),
    ("politics", "https://www.abc.net.au/news/feed/51120/rss.xml", "abc-au-top"),
    ("economy", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-AU&gl=AU&ceid=AU:en", "business-au"),
    ("economy", "https://news.google.com/rss/search?q=economy+when:1d&hl=en-US&gl=US&ceid=US:en", "economy-us"),
    ("stocks", "https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US&gl=US&ceid=US:en", "stocks"),
    ("stocks", "https://news.google.com/rss/search?q=ASX+OR+%22A-shares%22+when:1d&hl=en-AU&gl=AU&ceid=AU:en", "asx-ashares"),
    ("tech", "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en", "tech"),
    ("ai", "https://news.google.com/rss/search?q=artificial+intelligence+OR+OpenAI+OR+Anthropic+when:1d&hl=en-US&gl=US&ceid=US:en", "ai"),
    ("sports", "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-AU&gl=AU&ceid=AU:en", "sports-au"),
]

MAX_PER_CATEGORY = 5          # news items kept per category
MAX_ITEM_AGE_HOURS = 36       # ignore items older than this (when dated)
SNIPPET_MAX_CHARS = 300       # snippet length sent to the LLM
GEMINI_MODEL = "gemini-2.0-flash"
REPORT_TIMEZONE = "Australia/Sydney"

CATEGORY_META = {
    # key: (Chinese label, emoji)
    "politics": ("政治", "🏛️"),
    "economy": ("经济", "📈"),
    "stocks": ("股票", "💹"),
    "tech": ("科技", "🔬"),
    "ai": ("AI", "🤖"),
    "sports": ("体育", "⚽"),
}
CATEGORY_ORDER = ["politics", "economy", "stocks", "tech", "ai", "sports"]

REGION_RULES = [
    ("🇨🇳 中国", re.compile(r"\b(china|chinese|beijing|shanghai|hong kong|taiwan|xi jinping|yuan|renminbi|pboc)\b", re.I)),
    ("🇦🇺 澳洲", re.compile(r"\b(australia|australian|sydney|melbourne|canberra|asx|rba|aussie|albanese)\b", re.I)),
    ("🇺🇸 美国", re.compile(r"\b(u\.?s\.?a?\b|united states|america|american|washington|white house|congress|fed(eral reserve)?|wall street|nasdaq|s&p|dow)\b", re.I)),
]
DEFAULT_REGION = "🌏 国际"

USER_AGENT = "Mozilla/5.0 (compatible; NewsDigestBot/1.0)"

# --------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom into a list of raw item dicts."""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    # RSS 2.0
    for node in root.iter("item"):
        items.append(
            {
                "title": (node.findtext("title") or "").strip(),
                "link": (node.findtext("link") or "").strip(),
                "snippet": strip_html(node.findtext("description") or ""),
                "source": (node.findtext("source") or "").strip(),
                "published": (node.findtext("pubDate") or "").strip(),
            }
        )
    if items:
        return items

    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for node in root.findall(".//a:entry", ns):
        link_el = node.find("a:link", ns)
        items.append(
            {
                "title": (node.findtext("a:title", default="", namespaces=ns) or "").strip(),
                "link": link_el.get("href", "") if link_el is not None else "",
                "snippet": strip_html(node.findtext("a:summary", default="", namespaces=ns) or ""),
                "source": "",
                "published": (node.findtext("a:updated", default="", namespaces=ns) or "").strip(),
            }
        )
    return items


def parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def clean_google_title(title: str) -> tuple[str, str]:
    """Google News titles look like 'Headline - Source'. Split them."""
    match = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", title)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return title, ""


def detect_region(text: str) -> str:
    for label, pattern in REGION_RULES:
        if pattern.search(text):
            return label
    return DEFAULT_REGION


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", title.lower())[:80]


def collect_items(feeds=FEEDS, verbose: bool = True) -> list[dict]:
    """Fetch all feeds, dedupe, tag and trim to MAX_PER_CATEGORY each."""
    now = dt.datetime.now(dt.timezone.utc)
    seen: set[str] = set()
    by_category: dict[str, list[dict]] = {key: [] for key in CATEGORY_ORDER}
    failures = []

    for category, url, note in feeds:
        try:
            raw_items = parse_feed(http_get(url))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            failures.append(f"{note}: {exc}")
            if verbose:
                print(f"[warn] feed failed ({note}): {exc}", file=sys.stderr)
            continue

        for raw in raw_items:
            title, google_source = clean_google_title(raw["title"])
            if not title or not raw["link"]:
                continue
            key = normalize_title(title)
            if not key or key in seen:
                continue

            published = parse_date(raw["published"])
            if published and (now - published) > dt.timedelta(hours=MAX_ITEM_AGE_HOURS):
                continue

            seen.add(key)
            by_category[category].append(
                {
                    "category": category,
                    "title": title,
                    "link": raw["link"],
                    "snippet": raw["snippet"][:SNIPPET_MAX_CHARS],
                    "source": raw["source"] or google_source or "Unknown",
                    "published": published,
                    "region": detect_region(f"{title} {raw['snippet']}"),
                }
            )

    selected = []
    for category in CATEGORY_ORDER:
        pool = by_category[category]
        # Newest first; undated items sink to the end but are still usable.
        pool.sort(key=lambda item: item["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        selected.extend(pool[:MAX_PER_CATEGORY])

    for index, item in enumerate(selected):
        item["id"] = index
    if verbose:
        print(f"[info] selected {len(selected)} items ({len(failures)} feed failures)")
    return selected


# --------------------------------------------------------------------------
# LLM summarization (Gemini free tier)
# --------------------------------------------------------------------------

LLM_INSTRUCTIONS = """\
You are the editor of a Chinese daily news digest for a reader in Australia
who follows the US, China and Australia closely.

For EVERY item in the JSON list below, write in SIMPLIFIED CHINESE:
- "title_zh": a natural Chinese headline (translate, don't transliterate)
- "summary_zh": a 2-3 sentence factual summary. Base it ONLY on the given
  title and snippet; do not invent specifics that are not stated.
- "comment_zh": a 1-2 sentence analyst note (impact, background, or what to
  watch). Be measured, not sensational.

Then produce:
- "top_ids": ids of the 4-5 most significant items across all categories
- "overview_zh": a 150-250 character Chinese overview connecting the day's
  main threads (politics/economy/markets, US-China-Australia angles).

Return STRICT JSON only, shaped as:
{"items": [{"id": 0, "title_zh": "...", "summary_zh": "...", "comment_zh": "..."}],
 "top_ids": [..], "overview_zh": "..."}
"""


def summarize_with_gemini(items: list[dict], api_key: str) -> dict | None:
    payload_items = [
        {
            "id": item["id"],
            "category": item["category"],
            "title": item["title"],
            "snippet": item["snippet"],
            "source": item["source"],
        }
        for item in items
    ]
    body = {
        "contents": [
            {
                "parts": [
                    {"text": LLM_INSTRUCTIONS},
                    {"text": json.dumps(payload_items, ensure_ascii=False)},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on any failure
        print(f"[warn] Gemini call failed, falling back to headlines-only: {exc}", file=sys.stderr)
        return None


def apply_summaries(items: list[dict], llm_result: dict | None) -> tuple[list[int], str, bool]:
    """Merge LLM output into items. Returns (top_ids, overview, llm_used)."""
    if not llm_result:
        for item in items:
            item["title_zh"] = item["title"]
            item["summary_zh"] = item["snippet"] or "(未生成摘要:未配置 GEMINI_API_KEY 或调用失败)"
            item["comment_zh"] = ""
        return [item["id"] for item in items[:5]], "", False

    merged = {entry.get("id"): entry for entry in llm_result.get("items", [])}
    for item in items:
        entry = merged.get(item["id"], {})
        item["title_zh"] = entry.get("title_zh") or item["title"]
        item["summary_zh"] = entry.get("summary_zh") or item["snippet"]
        item["comment_zh"] = entry.get("comment_zh") or ""
    top_ids = [i for i in llm_result.get("top_ids", []) if isinstance(i, int)][:5]
    if not top_ids:
        top_ids = [item["id"] for item in items[:5]]
    return top_ids, llm_result.get("overview_zh", ""), True


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

CSS = """
:root { --bg:#f6f7f9; --card:#ffffff; --ink:#1c2733; --muted:#66788a;
        --accent:#0f6fde; --line:#e4e9ee; --quote-bg:#f0f6ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif; line-height:1.65; }
.wrap { max-width:860px; margin:0 auto; padding:0 16px 48px; }
header.masthead { padding:28px 0 12px; border-bottom:2px solid var(--ink); }
header.masthead h1 { margin:0; font-size:30px; letter-spacing:2px; }
header.masthead .date { color:var(--muted); margin-top:6px; font-size:14px; }
nav.toc { position:sticky; top:0; z-index:9; background:var(--bg);
          display:flex; gap:6px; overflow-x:auto; padding:10px 0; border-bottom:1px solid var(--line); }
nav.toc a { white-space:nowrap; text-decoration:none; color:var(--ink); font-size:14px;
            padding:5px 12px; border-radius:16px; border:1px solid var(--line); background:var(--card); }
nav.toc a:hover { border-color:var(--accent); color:var(--accent); }
section { margin-top:30px; }
section h2 { font-size:20px; border-left:5px solid var(--accent); padding-left:10px; margin:0 0 14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:14px 16px; margin-bottom:12px; }
.card h3 { margin:0 0 6px; font-size:16px; }
.card h3 a { color:var(--ink); text-decoration:none; }
.card h3 a:hover { color:var(--accent); }
.meta { font-size:12px; color:var(--muted); margin-bottom:8px; }
.meta .tag { background:#eef2f6; border-radius:4px; padding:1px 6px; margin-right:6px; }
.summary { margin:0 0 8px; font-size:14.5px; }
.comment { border-left:3px solid var(--accent); background:var(--quote-bg);
           padding:8px 12px; border-radius:0 8px 8px 0; font-size:13.5px; color:#274a6d; }
.overview { background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:16px 18px; font-size:15px; }
.top-item { padding:10px 0; border-bottom:1px dashed var(--line); font-size:15px; }
.top-item:last-child { border-bottom:none; }
.top-item a { color:var(--ink); text-decoration:none; font-weight:600; }
.top-item a:hover { color:var(--accent); }
footer { margin-top:40px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); padding-top:12px; }
.notice { background:#fff7e6; border:1px solid #ffe1a8; color:#8a6100;
          border-radius:8px; padding:10px 14px; font-size:13px; margin-top:16px; }
"""

WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def report_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(REPORT_TIMEZONE))
    except Exception:  # noqa: BLE001 - Windows without tzdata, etc.
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=10)))


def render_item(item: dict) -> str:
    published = item["published"].astimezone().strftime("%m-%d %H:%M") if item["published"] else ""
    comment_html = (
        f'<div class="comment">💬 简评:{html.escape(item["comment_zh"])}</div>'
        if item.get("comment_zh")
        else ""
    )
    return f"""
    <div class="card" id="item-{item['id']}">
      <h3><a href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['title_zh'])}</a></h3>
      <div class="meta"><span class="tag">{item['region']}</span>
        <span class="tag">{html.escape(item['source'])}</span> {published}</div>
      <p class="summary">{html.escape(item['summary_zh'])}</p>
      {comment_html}
    </div>"""


def render_html(items: list[dict], top_ids: list[int], overview: str, llm_used: bool) -> str:
    now = report_now()
    date_str = now.strftime("%Y-%m-%d")
    weekday = WEEKDAYS_ZH[now.weekday()]
    by_id = {item["id"]: item for item in items}

    top_html = "".join(
        f'<div class="top-item">{by_id[i]["region"]} <a href="#item-{i}">{html.escape(by_id[i]["title_zh"])}</a></div>'
        for i in top_ids
        if i in by_id
    )

    sections = []
    for category in CATEGORY_ORDER:
        cat_items = [item for item in items if item["category"] == category]
        if not cat_items:
            continue
        label, emoji = CATEGORY_META[category]
        cards = "".join(render_item(item) for item in cat_items)
        sections.append(f'<section id="{category}"><h2>{emoji} {label}</h2>{cards}</section>')

    overview_html = (
        f'<section id="overview"><h2>📝 今日综述</h2><div class="overview">{html.escape(overview)}</div></section>'
        if overview
        else ""
    )
    notice_html = (
        ""
        if llm_used
        else '<div class="notice">⚠️ 本期未启用 AI 摘要(缺少 GEMINI_API_KEY 或调用失败),以下为原文标题与摘录。</div>'
    )
    nav_links = ['<a href="#top-news">今日要闻</a>'] + [
        f'<a href="#{category}">{CATEGORY_META[category][0]}</a>'
        for category in CATEGORY_ORDER
        if any(item["category"] == category for item in items)
    ]
    if overview:
        nav_links.append('<a href="#overview">综述</a>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>新闻日报 {date_str}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1>新闻日报</h1>
    <div class="date">{date_str} {weekday} · 生成于 {now.strftime("%H:%M %Z")} · 共 {len(items)} 条</div>
  </header>
  <nav class="toc">{''.join(nav_links)}</nav>
  {notice_html}
  <section id="top-news"><h2>🔥 今日要闻</h2>{top_html}</section>
  {''.join(sections)}
  {overview_html}
  <footer>数据来源:Google News RSS / ABC News Australia · 摘要与简评由 AI 生成,仅供参考,请以原文为准。</footer>
</div>
</body>
</html>"""


# --------------------------------------------------------------------------
# Sample data for offline testing
# --------------------------------------------------------------------------


def sample_items() -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    samples = [
        ("politics", "US and China resume trade talks in Geneva", "Reuters", "🇺🇸 美国"),
        ("economy", "RBA holds cash rate steady at 3.60pc", "ABC News", "🇦🇺 澳洲"),
        ("stocks", "ASX 200 closes at record high on mining rally", "AFR", "🇦🇺 澳洲"),
        ("tech", "Apple unveils new M5 chip line for MacBooks", "The Verge", "🇺🇸 美国"),
        ("ai", "Anthropic releases new frontier model", "TechCrunch", "🌏 国际"),
        ("sports", "Matildas name squad for Asian Cup qualifiers", "ABC Sport", "🇦🇺 澳洲"),
    ]
    items = []
    for index, (category, title, source, region) in enumerate(samples):
        items.append(
            {
                "id": index,
                "category": category,
                "title": title,
                "title_zh": title,
                "link": "https://example.com/" + str(index),
                "snippet": "Sample snippet for offline testing. " * 3,
                "source": source,
                "published": now,
                "region": region,
            }
        )
    return items


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily news digest HTML")
    parser.add_argument("--out-dir", default="reports", help="output directory")
    parser.add_argument("--no-llm", action="store_true", help="skip Gemini summarization")
    parser.add_argument("--test", action="store_true", help="offline test with sample data")
    args = parser.parse_args()

    if args.test:
        items = sample_items()
        top_ids, overview, llm_used = apply_summaries(items, None)
    else:
        items = collect_items()
        if not items:
            print("[error] no items collected from any feed", file=sys.stderr)
            return 1
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        llm_result = None
        if api_key and not args.no_llm:
            llm_result = summarize_with_gemini(items, api_key)
        top_ids, overview, llm_used = apply_summaries(items, llm_result)

    html_text = render_html(items, top_ids, overview, llm_used)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = report_now().strftime("%Y-%m-%d")
    out_file = out_dir / f"{date_str}.html"
    out_file.write_text(html_text, encoding="utf-8")
    (out_dir / "latest.html").write_text(html_text, encoding="utf-8")

    # Machine-readable copy so other agents (e.g. a Claude scheduled task)
    # can pull the curated items via the GitHub contents API and write their
    # own report on top of this data.
    json_payload = {
        "generated_at": report_now().isoformat(),
        "date": date_str,
        "llm_used": llm_used,
        "overview_zh": overview,
        "top_ids": top_ids,
        "items": [
            {
                "id": item["id"],
                "category": item["category"],
                "region": item["region"],
                "title": item["title"],
                "title_zh": item.get("title_zh", ""),
                "summary_zh": item.get("summary_zh", ""),
                "comment_zh": item.get("comment_zh", ""),
                "snippet": item["snippet"],
                "link": item["link"],
                "source": item["source"],
                "published": item["published"].isoformat() if item["published"] else None,
            }
            for item in items
        ],
    }
    (out_dir / "latest.json").write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[done] report written to {out_file} (+ latest.html, latest.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
