#!/usr/bin/env python3
"""
Refresh the "Writing" section of index.html with the latest Learning Innovation
columns from Inside Higher Ed.

How it finds the columns (in order, stops at the first that yields results):
  1. Read the column page and auto-discover its RSS/Atom feed link.
  2. Try a short list of likely feed URLs.
  3. As a last resort, scrape article links off the column landing page.

Safety: the file is only rewritten when at least MIN_ITEMS fresh columns are
found AND the generated block differs from what's already there. If anything
fails, the existing (last-known-good) columns are left untouched and the script
exits 0 so the scheduled job doesn't spam failure emails.

No third-party packages — Python standard library only.
"""

import html
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- config
COLUMN_URL = "https://www.insidehighered.com/opinion/columns/learning-innovation"
COLUMN_SLUG = "learning-innovation"          # links must contain this to count
MAX_ITEMS = 3                                # how many to show on the site
MIN_ITEMS = 1                                # don't overwrite unless we got >=1
INDEX = Path(__file__).resolve().parent.parent / "index.html"
START = "<!-- COLUMNS:START"                  # marker prefixes (comment bodies vary)
END = "<!-- COLUMNS:END -->"
UA = "Mozilla/5.0 (compatible; joshmkim.com-site-builder/1.0)"
TIMEOUT = 30

# article URLs look like .../learning-innovation/2026/05/21/some-slug
ARTICLE_RE = re.compile(
    r"https?://[^\s\"'<>]*?/" + re.escape(COLUMN_SLUG) + r"/\d{4}/\d{2}/\d{2}/[^\s\"'<>]+"
)


# ---------------------------------------------------------------- helpers
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def strip_tags(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_dek(text, limit=170):
    text = strip_tags(text)
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:—- ")
        text = cut + "\u2026"
    return text


def fmt_date(dt):
    # cross-platform "May 2026" (no leading-zero tricks needed for month name)
    return dt.strftime("%b %Y")


def parse_dt(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    # RSS pubDate (RFC 822)
    try:
        d = parsedate_to_datetime(raw)
        if d:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # Atom / ISO 8601
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- sources
def discover_feed(page_html):
    """Find <link rel="alternate" type="application/(rss|atom)+xml" href="...">."""
    for m in re.finditer(r"<link\b[^>]*>", page_html, re.I):
        tag = m.group(0)
        if re.search(r'type\s*=\s*["\']application/(rss|atom)\+xml', tag, re.I):
            href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if href:
                return urllib.parse.urljoin(COLUMN_URL, href.group(1))
    return None


def items_from_feed(xml_text):
    """Return [(dt, url, title, dek)] from an RSS or Atom feed."""
    items = []
    root = ET.fromstring(xml_text)
    tag = lambda e: e.tag.split("}")[-1].lower()  # strip namespace

    # RSS: channel/item ; Atom: feed/entry
    entries = [e for e in root.iter() if tag(e) in ("item", "entry")]
    for e in entries:
        url = title = date = dek = ""
        for child in e:
            t = tag(child)
            if t == "title":
                title = (child.text or "").strip()
            elif t == "link":
                # RSS link is text; Atom link is href attribute
                if child.text and child.text.strip():
                    url = child.text.strip()
                else:
                    rel = child.get("rel", "alternate")
                    if rel == "alternate" and child.get("href"):
                        url = child.get("href").strip()
            elif t in ("pubdate", "published", "updated", "date") and not date:
                date = (child.text or "").strip()
            elif t in ("description", "summary", "subtitle") and not dek:
                dek = child.text or ""
            elif t == "encoded" and not dek:  # content:encoded
                dek = child.text or ""
        if url and title:
            items.append((parse_dt(date), url, strip_tags(title), clean_dek(dek)))
    return items


def items_from_landing(page_html):
    """Scrape dated article links straight off the column page (fallback)."""
    seen, items = set(), []
    for m in ARTICLE_RE.finditer(page_html):
        url = html.unescape(m.group(0)).rstrip(').,"\'')
        if url in seen:
            continue
        seen.add(url)
        # title: nearest anchor text wrapping this href, else slug -> words
        amatch = re.search(
            r'<a\b[^>]*href=["\']' + re.escape(m.group(0)) + r'["\'][^>]*>(.*?)</a>',
            page_html, re.I | re.S,
        )
        title = strip_tags(amatch.group(1)) if amatch else ""
        if not title:
            slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            title = slug.title()
        date = None
        dm = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
        if dm:
            date = datetime(int(dm[1]), int(dm[2]), int(dm[3]), tzinfo=timezone.utc)
        items.append((date, url, title, ""))
    return items


def collect():
    page = ""
    try:
        page = fetch(COLUMN_URL)
    except Exception as e:  # noqa: BLE001
        print(f"  note: could not fetch column page: {e}")

    candidates = []
    if page:
        feed = discover_feed(page)
        if feed:
            candidates.append(feed)
            print(f"  discovered feed: {feed}")
    candidates += [
        COLUMN_URL + "/feed",
        COLUMN_URL + "/rss",
        "https://www.insidehighered.com/rss/opinion/columns/" + COLUMN_SLUG,
        "https://www.insidehighered.com/taxonomy/term/" + COLUMN_SLUG + "/feed",
    ]

    for url in candidates:
        try:
            xml_text = fetch(url)
            got = items_from_feed(xml_text)
            got = [i for i in got if COLUMN_SLUG in i[1]]  # keep this column only
            if got:
                print(f"  using feed: {url} ({len(got)} items)")
                return got
        except Exception:  # noqa: BLE001
            continue

    if page:
        got = items_from_landing(page)
        if got:
            print(f"  using landing-page scrape ({len(got)} items)")
            return got

    return []


# ---------------------------------------------------------------- render
def render(items):
    items.sort(key=lambda i: (i[0] is not None, i[0]), reverse=True)
    rows = []
    for dt, url, title, dek in items[:MAX_ITEMS]:
        date_label = fmt_date(dt) if dt else ""
        dek_html = (
            f'\n            <span class="piece__dek">{html.escape(dek)}</span>'
            if dek else ""
        )
        rows.append(
            f'        <a class="piece" href="{html.escape(url)}" target="_blank" rel="noopener">\n'
            f'          <span class="piece__date">{html.escape(date_label)}</span>\n'
            f'          <span>\n'
            f'            <span class="piece__title">{html.escape(title)}</span>'
            f'{dek_html}\n'
            f'          </span>\n'
            f'          <span class="piece__arrow">\u2192</span>\n'
            f'        </a>'
        )
    return "\n".join(rows)


def main():
    text = INDEX.read_text(encoding="utf-8")
    s = text.find(START)
    e = text.find(END)
    if s == -1 or e == -1:
        print("ERROR: COLUMNS markers not found in index.html"); sys.exit(0)
    s_line_end = text.find("\n", s)                      # keep the START comment line
    start_comment = text[s:s_line_end]

    print("Fetching latest Learning Innovation columns...")
    items = collect()
    if len(items) < MIN_ITEMS:
        print("  no columns parsed — leaving existing block untouched.")
        sys.exit(0)

    block = render(items)
    new_text = text[:s] + start_comment + "\n" + block + "\n        " + text[e:]

    if new_text == text:
        print("  already up to date — no change.")
        sys.exit(0)

    INDEX.write_text(new_text, encoding="utf-8")
    titles = ", ".join(i[2] for i in items[:MAX_ITEMS])
    print(f"  updated index.html with: {titles}")


if __name__ == "__main__":
    main()

