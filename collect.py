#!/usr/bin/env python3
"""
kuda. — collector for culture events in Banja Luka (v0.5)

Sources (v1): Narodno pozorište RS, Banski dvor, SC Borik.
Culture only: sport and folk music are dropped. Unclear items (folklore, sevdah,
ethno, concerts at a sports venue with no clear genre) go to data/review.json.

Run:   python3 collect.py               # looks 90 days ahead
       python3 collect.py --days 60
       python3 collect.py --only borik  # one source (np, bd, borik)
       python3 collect.py --offline     # re-run on the pages saved last time (no internet needed)
       python3 collect.py --probe       # also save pages of sources that are still being added

Needs only Python 3.9+, nothing to install. If Python's SSL certificates are
missing (common on macOS), it falls back to curl.

Writes:
  data/events.json    culture events to publish
  data/review.json    unclear items: decide by hand
  data/excluded.json  dropped items, each with a reason
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Sarajevo")
except Exception:  # very old Python or no time-zone database
    TZ = None

UA = "kuda-collector/0.5 (Banja Luka culture listings; one visit per day)"
DELAY_SECONDS = 1.0  # pause between requests to the same run

SOURCES = [
    {
        "key": "np", "name": "Narodno pozorište RS", "venue": "Narodno pozorište Republike Srpske",
        "aliases": ["Narodno pozorište Republike Srpske", "Narodno pozorište RS"],
        "base": "https://www.np.rs.ba", "kind": "tec", "trust": "culture", "default_category": "Pozorište",
        "ics": ["/repertoar/?ical=1", "/repertoar/list/?ical=1"], "html": ["/repertoar/"],
    },
    {
        "key": "bd", "name": "Banski dvor", "venue": "Banski dvor Kulturni centar",
        "aliases": ["Banski dvor Kulturni centar", "Kulturni centar Banski dvor", "Banski dvor"],
        "enrich_images": True,  # the feed often has no picture, but each event page does
        "base": "https://banskidvor.org", "kind": "tec", "trust": "culture", "default_category": "Ostalo",
        "ics": ["/events/?ical=1", "/events/list/?ical=1"], "html": ["/events/"],
    },
    {
        "key": "gpj", "name": "Gradsko pozorište Jazavac", "venue": "Gradsko pozorište Jazavac",
        "aliases": ["Gradsko pozorište Jazavac"], "base": "https://gpj.ba", "kind": "mec", "trust": "culture",
        "default_category": "Pozorište", "html": ["/repertoar/"],
    },
    {
        "key": "borik", "name": "SC Borik", "venue": "SC Borik", "aliases": ["SC Borik", "Sportski centar Borik"],
        "hall_venues": {"pozorisna sala": "Gradsko pozorište Jazavac"},  # Borik's theatre hall is Jazavac's stage
        "base": "https://scborik.com", "kind": "table", "trust": "mixed", "default_category": "Muzika",
        "html": ["/?page_id=6913"],
    },
    {
        "key": "gig", "name": "Gigstix", "venue": "Banjaluka", "aliases": [], "base": "https://gigstix.ba",
        "kind": "gigstix", "trust": "mixed", "default_category": "Muzika",
        "html": ["/location/banja-luka/?post_type=event"],
    },
    {
        "key": "cs", "name": "Cinestar Delta Planet", "venue": "Cinestar Delta Planet", "aliases": ["Cinestar"],
        "base": "https://cinestarcinemas.ba", "kind": "cinestar", "trust": "culture", "default_category": "Kino",
        "html": ["/banja-luka-delta-planet"],
    },
    # Cineplexx Palas is left out on purpose: its robots.txt asks bots not to collect from the site.
]

# ------------------------------------------------------------------ fetching

_robots: dict = {}
_last_request = [0.0]
RAW = {"dir": None, "offline": False}  # saved copy of every page, so parsing can be re-run without internet


def _raw_path(url: str) -> Path:
    return RAW["dir"] / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".bin")


def _download(url: str, accept: str) -> bytes:
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Language": "sr,bs,hr;q=0.9,en;q=0.5"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as ex:
        if "CERTIFICATE_VERIFY_FAILED" in str(ex) or isinstance(ex.reason, ssl.SSLError):
            cmd = ["curl", "-fsSL", "--max-time", "30", "-A", UA, "-H", f"Accept: {accept}", url]
            return subprocess.run(cmd, check=True, capture_output=True).stdout
        raise


def _robots_for(url: str):
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(_download(root + "/robots.txt", "text/plain").decode("utf-8", "replace").splitlines())
            rp.modified()
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                rp.disallow_all = True
            else:
                rp = None  # no robots.txt: nothing is off limits
        except Exception:
            rp = None
        _robots[root] = rp
    return _robots[root]


def get(url: str, accept: str = "*/*") -> bytes:
    if RAW["offline"]:
        path = _raw_path(url)
        if not path.exists():
            raise FileNotFoundError("not in the saved copy")
        return path.read_bytes()
    rp = _robots_for(url)
    if rp is not None and not rp.can_fetch(UA, url):
        raise PermissionError("robots.txt asks bots not to fetch this page")
    wait = DELAY_SECONDS - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    try:
        body = _download(url, accept)
    finally:
        _last_request[0] = time.time()
    if RAW["dir"] is not None:
        RAW["dir"].mkdir(parents=True, exist_ok=True)
        _raw_path(url).write_bytes(body)
    return body


def err(ex: Exception) -> str:
    if isinstance(ex, urllib.error.HTTPError):
        return f"HTTP {ex.code}"
    return f"{type(ex).__name__}: {ex}"[:120]

# ------------------------------------------------------------------ text helpers

_CYR = dict(zip("абвгдђежзијклљмнњопрстћуфхцчџш",
                ["a", "b", "v", "g", "d", "đ", "e", "ž", "z", "i", "j", "k", "l", "lj", "m",
                 "n", "nj", "o", "p", "r", "s", "t", "ć", "u", "f", "h", "c", "č", "dž", "š"]))


def to_latin(text: str | None) -> str:
    """Serbian Cyrillic to Latin; other characters stay as they are."""
    text = text or ""
    out = []
    for i, ch in enumerate(text):
        low = ch.lower()
        lat = _CYR.get(low)
        if lat is None:
            out.append(ch)
            continue
        if ch != low:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            lat = lat.upper() if nxt.isupper() else lat.capitalize()
        out.append(lat)
    return "".join(out)


def strip_html(text: str | None) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def one_line(text: str | None, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", strip_html(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:–-") + "…"
    return text


def fold(text: str | None) -> str:
    """Lower-case Latin without diacritics, for keyword matching (č→c, đ→dj …)."""
    text = to_latin(text).lower().replace("đ", "dj")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


_SMALL_WORDS = {"i", "u", "sa", "s", "za", "na", "od", "do", "po", "iz", "o", "a", "k", "ka", "uz", "bez", "ili",
                "je", "se", "pod", "nad", "pred", "the", "of", "and", "in"}
_KEEP_UPPER = {"RS", "NP", "NPRS", "SO", "BIH", "KUD", "SC", "GP", "DJ", "MC", "TV", "BL", "KSB", "OK", "ŽOK", "ŽKK",
               "KK", "RK", "FK", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"}


def _name_words(text: str) -> set:
    """Lower-case words a text writes with a capital letter in the middle of a sentence: names, places, bands."""
    names, plain = set(), set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        for w in re.findall(r"[^\W\d_]+", sentence)[1:]:
            if w[:1].isupper() and not w.isupper():
                names.add(w.lower())
            elif w.islower():
                plain.add(w)
    return names - plain


def smart_case(text: str, sentence: bool = False, context: str = "") -> str:
    """Tame CAPS. With sentence=True (titles) an all-caps title becomes sentence case, as Serbian writes titles
    ("Šta će nići kad posadiš srce"); words the description (context) writes with a capital keep it. Otherwise
    (organizers) all caps become Title Case. Caps names inside normal text are capitalised."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    all_caps = not any(c.islower() for c in letters)
    names = _name_words(context) if all_caps and sentence else set()
    out, first, start = [], True, True
    for tok in re.split(r"(\W+)", text):
        if not tok or not any(c.isalpha() for c in tok):
            out.append(tok)
            if re.search(r"[.!?:]|[„“\"«]$", tok or ""):  # a subtitle, a new sentence or a quoted title starts here
                start = True
            continue
        if tok.isupper() and tok in _KEEP_UPPER:
            out.append(tok)
        elif all_caps and sentence:
            low = tok.lower()
            out.append(low[:1].upper() + low[1:] if start or low in names else low)
        elif all_caps:
            low = tok.lower()
            out.append(low if low in _SMALL_WORDS and not first else low[:1].upper() + low[1:])
        elif tok.isupper() and len(tok) >= 4:
            out.append(tok[:1] + tok[1:].lower())
        else:
            out.append(tok)
        first = start = False
    return "".join(out)


def title_key(text: str) -> str:
    """A title reduced to plain words, for matching ("Čiča, Miča…" = "CICA MICA…")."""
    return re.sub(r"[^a-z0-9]+", " ", fold(text)).strip()


def clean_hall(hall: str | None, aliases: list) -> str | None:
    """'Narodno pozorište RS – Scena ”Petar Kočić”' -> 'Scena „Petar Kočić“'."""
    h = (hall or "").strip()
    for name in aliases:
        if fold(h) == fold(name):
            return None
        h = re.sub(rf"^{re.escape(name)}\s*[–—\-,:|]\s*", "", h, flags=re.I)
    h = re.sub(r"[”“\"]([^”“\"]+)[”“\"]", r"„\1“", h)
    if h and not any(c.islower() for c in h):
        h = h[:1] + h[1:].lower()
    return h or None


def price_from_text(text: str | None) -> str | None:
    f = fold(text)
    if re.search(r"ulaz\w*\s+(je\s+)?(slobodan|besplatan)|(slobodan|besplatan)\s+ulaz", f):
        return "Ulaz slobodan"
    m = re.search(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*(km\b|konvertibiln)", f)
    return f"{m.group(1)} KM" if m else None


def age_from_text(text: str | None) -> str | None:
    m = re.match(r"\s*\(?\s*(\d{1,2})\s*\+\s*\)?", text or "")
    return f"{m.group(1)}+" if m and 3 <= int(m.group(1)) <= 18 else None


def clean_description(text: str | None) -> str:
    text = re.sub(r"(?i)kupite\s+ulaznic\w*\s*(>>|»|›|>)?", " ", text or "")
    text = re.sub(r"^\s*\(?\s*\d{1,2}\s*\+\s*\)?\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()

# ------------------------------------------------------------------ dates

_MONTHS = [("januar", 1), ("februar", 2), ("mart", 3), ("april", 4), ("maj", 5), ("jun", 6),
           ("jul", 7), ("avgust", 8), ("august", 8), ("septemb", 9), ("oktob", 10),
           ("novemb", 11), ("decemb", 12)]


def local(d: dt.datetime) -> dt.datetime:
    if TZ is None:
        return d.replace(tzinfo=None)
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)


def parse_iso(value) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.fromisoformat(value) if fmt is None else dt.datetime.strptime(value, fmt)
            return local(d)
        except ValueError:
            continue
    return None


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _guess_year(month: int, day: int, today: dt.date) -> int:
    d = _safe_date(today.year, month, day)
    if d and d < today - dt.timedelta(days=120):
        return today.year + 1  # e.g. a January date seen in November
    return today.year


def date_from_text(text: str, today: dt.date) -> dt.date | None:
    f = fold(text)
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[./]\s*(\d{1,2})\s*[./]?\s*((?:19|20)\d{2})?(?!\d)", f):
        day, month = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year = int(m.group(3)) if m.group(3) else _guess_year(month, day, today)
        d = _safe_date(year, month, day)
        if d:
            return d
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*\.?\s*([a-z]{3,})\.?(?:\s*((?:19|20)\d{2}))?", f):
        month = next((n for name, n in _MONTHS if m.group(2).startswith(name)), None)
        if not month:
            continue
        day = int(m.group(1))
        year = int(m.group(3)) if m.group(3) else _guess_year(month, day, today)
        d = _safe_date(year, month, day)
        if d:
            return d
    return None


def time_from_text(text: str) -> tuple | None:
    f = fold(text).strip()
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", f)
    if not m and len(f) <= 6:
        m = re.fullmatch(r"([01]?\d|2[0-3])\s*[.,h]\s*([0-5]\d)\s*h?", f)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*h\b", f)
    return (int(m.group(1)), 0) if m else None

# ------------------------------------------------------------------ The Events Calendar (WordPress) sites

def tec_api(base: str, days: int, today: dt.date) -> list:
    query = {"start_date": today.isoformat(), "end_date": (today + dt.timedelta(days=days)).isoformat(), "per_page": 50}
    url = f"{base}/wp-json/tribe/events/v1/events?" + urllib.parse.urlencode(query)
    events, pages = [], 0
    while url and pages < 20:
        data = json.loads(get(url, "application/json"))
        events.extend(data.get("events") or [])
        url, pages = data.get("next_rest_url"), pages + 1
    return events


def from_tec(e: dict) -> dict:
    venue = e.get("venue") or {}
    if isinstance(venue, list):
        venue = venue[0] if venue else {}
    image = e.get("image")
    title = one_line(e.get("title"))
    text = to_latin(one_line(e.get("description") or e.get("excerpt")))
    return {
        "source_id": str(e.get("id") or "") or None,
        "title_original": title, "title": to_latin(title),
        "start": parse_iso(e.get("start_date")), "end": parse_iso(e.get("end_date")),
        "all_day": bool(e.get("all_day")),
        "hall": to_latin(one_line(venue.get("venue"))) if isinstance(venue, dict) else None,
        "price": to_latin(one_line(e.get("cost"))) or price_from_text(text),
        "age": age_from_text(text),
        "image": image.get("url") if isinstance(image, dict) else None,
        "url": e.get("url"), "ticket_url": e.get("website") or None,
        "description": one_line(clean_description(text), 280) or None,
        "source_categories": [to_latin(one_line(c.get("name"))) for c in e.get("categories") or [] if isinstance(c, dict)],
    }


def parse_ics(text: str) -> list:
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))  # unfold long lines
    events = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", text, re.S):
        ev = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            value = value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
            ev.setdefault(name.upper(), (params, value.strip()))
        events.append(ev)
    return events


def _ics_time(item) -> tuple:
    if not item:
        return None, False
    params, value = item
    if re.fullmatch(r"\d{8}", value):
        return local(dt.datetime.strptime(value, "%Y%m%d")), True
    d = dt.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        d = d.replace(tzinfo=dt.timezone.utc)
    return local(d), False


def from_ics(ev: dict) -> dict:
    def val(name: str) -> str:
        return ev[name][1] if name in ev else ""

    start, all_day = _ics_time(ev.get("DTSTART"))
    end, _ = _ics_time(ev.get("DTEND"))
    attach_params = ev.get("ATTACH", ("", ""))[0].lower()
    uid = val("UID")
    title = one_line(val("SUMMARY"))
    return {
        "source_id": hashlib.sha1(uid.encode()).hexdigest()[:10] if uid else None,
        "title_original": title, "title": to_latin(title),
        "start": start, "end": end, "all_day": all_day,
        "hall": to_latin(one_line(val("LOCATION")).split(",")[0]) or None,
        "price": price_from_text(to_latin(val("DESCRIPTION"))),
        "age": age_from_text(to_latin(one_line(val("DESCRIPTION")))),
        "image": val("ATTACH") if "image" in attach_params else None,
        "url": val("URL") or None, "ticket_url": None,
        "description": one_line(clean_description(to_latin(one_line(val("DESCRIPTION")))), 280) or None,
        "source_categories": [to_latin(c.strip()) for c in val("CATEGORIES").split(",") if c.strip()],
    }


def jsonld_events(page: str) -> list:
    found = []
    for raw in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                if "@graph" in x:
                    stack.append(x["@graph"])
                kinds = x.get("@type") if isinstance(x.get("@type"), list) else [x.get("@type")]
                if any(isinstance(k, str) and k.endswith("Event") for k in kinds):
                    found.append(x)
    return found


def from_jsonld(x: dict) -> dict:
    loc = x.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    image = x.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    offers = x.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price") if isinstance(offers, dict) else None
    title = one_line(x.get("name"))
    return {
        "source_id": None, "title_original": title, "title": to_latin(title),
        "start": parse_iso(x.get("startDate")), "end": parse_iso(x.get("endDate")), "all_day": False,
        "hall": to_latin(one_line(loc.get("name") if isinstance(loc, dict) else str(loc))) or None,
        "price": f"{price} KM" if price not in (None, "", "0", 0) else None,
        "image": image, "url": x.get("url"), "ticket_url": None,
        "description": one_line(clean_description(to_latin(one_line(x.get("description")))), 280) or None,
        "source_categories": [],
    }


def collect_tec(src: dict, days: int, today: dt.date, log: list) -> list:
    """Try the REST API, then the iCal feed, then JSON-LD on the HTML page."""
    try:
        raw = tec_api(src["base"], days, today)
        log.append(f"{src['name']}: REST API, {len(raw)} events")
        return [from_tec(e) for e in raw]
    except Exception as ex:
        log.append(f"{src['name']}: REST API not available ({err(ex)}), trying iCal")
    for path in src.get("ics", []):
        try:
            text = get(src["base"] + path, "text/calendar").decode("utf-8", "replace")
            if "BEGIN:VEVENT" not in text:
                raise ValueError("no events in the feed")
            raw = parse_ics(text)
            log.append(f"{src['name']}: iCal {path}, {len(raw)} events")
            return [from_ics(e) for e in raw]
        except Exception as ex:
            log.append(f"{src['name']}: iCal {path} failed ({err(ex)})")
    for path in src.get("html", []):
        try:
            raw = jsonld_events(get(src["base"] + path, "text/html").decode("utf-8", "replace"))
            if not raw:
                raise ValueError("no event data on the page")
            log.append(f"{src['name']}: page data {path}, {len(raw)} events")
            return [from_jsonld(e) for e in raw]
        except Exception as ex:
            log.append(f"{src['name']}: page {path} failed ({err(ex)})")
    return []

def page_image(url: str) -> str | None:
    """The main picture of an event page: og:image, twitter:image, the WordPress featured image,
    or else the first picture in the text."""
    page = get(url, "text/html").decode("utf-8", "replace")
    patterns = [
        r"<meta[^>]+property=[\"']og:image(?::url)?[\"'][^>]+content=[\"']([^\"']+)",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image(?::url)?[\"']",
        r"<meta[^>]+name=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)",
        r"<img[^>]+class=[\"'][^\"']*wp-post-image[^\"']*[\"'][^>]+src=[\"']([^\"']+)",
        r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]+class=[\"'][^\"']*wp-post-image",
    ]
    for pattern in patterns:
        m = re.search(pattern, page, re.I)
        if m:
            return urllib.parse.urljoin(url, html.unescape(m.group(1).strip()))
    # no main picture set: the first picture placed in the text, in its larger size
    inline = re.search(r"(?is)<img[^>]+class=[\"'][^\"']*\bwp-image-\d+[^>]*>", page)
    return best_src(inline.group(0), url) if inline else None


def enrich_images(src: dict, events: list, log: list) -> None:
    """Fill missing pictures from each event's own page; skip a picture several different events share (a site logo)."""
    todo = [e for e in events if e["source"] == src["name"] and not e.get("image") and e.get("url")][:40]
    found = {}
    for e in todo:
        try:
            found[e["id"]] = page_image(e["url"])
        except Exception as ex:
            log.append(f"{src['name']}: no picture from {e['url']} ({err(ex)})")
    titles = {}
    for e in todo:
        if found.get(e["id"]):
            titles.setdefault(found[e["id"]], set()).add(fold(e["title"]))
    added = 0
    for e in todo:
        img = found.get(e["id"])
        if img and len(titles[img]) == 1:
            e["image"] = img
            added += 1
    if todo:
        log.append(f"{src['name']}: pictures from event pages for {added} of {len(todo)} events")

# ------------------------------------------------------------------ sharper film pictures from TMDB

TMDB = "https://api.themoviedb.org/3"
FILMS_CACHE = Path(__file__).resolve().parent / ".cache" / "films.json"  # kept between runs by the GitHub workflow


def _tmdb(path: str, **params) -> dict:
    """One TMDB API call. TMDB_API_KEY may hold the short API key or the long read access token."""
    key = os.environ.get("TMDB_API_KEY", "").strip()
    headers = {"Accept": "application/json", "User-Agent": UA}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    with urllib.request.urlopen(urllib.request.Request(f"{TMDB}{path}?{query}", headers=headers), timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _film_page_facts(url: str) -> tuple:
    """The original title and the year from a Cinestar film page ("Izvorni naslov", "Početak prikazivanja")."""
    page = get(url, "text/html").decode("utf-8", "replace")
    gap = r"\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*"  # tags may sit between a label and its value
    orig = re.search(rf"(?is)Izvorni naslov:?{gap}([^<]{{1,150}})", page)
    when = re.search(rf"(?is)Početak prikazivanja:?{gap}(\d{{1,2}})\.\s*(\d{{1,2}})\.\s*(\d{{4}})", page)
    title = re.sub(r"^[\s:–-]+", "", one_line(html.unescape(orig.group(1)))) if orig else ""
    return (title or None, int(when.group(3)) if when else None)


def _tmdb_match(titles: list, year) -> dict | None:
    """The TMDB film with exactly this title (original or local), preferring the right year."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", fold(t or ""))
    titles = [t for t in dict.fromkeys(titles) if t]
    wanted = {norm(t) for t in titles}
    for query in titles:
        for y in ([year, year - 1, None] if year else [None]):
            found = _tmdb("/search/movie", query=query, primary_release_year=y, include_adult="false", language="hr-HR")
            hits = [m for m in found.get("results", []) if m.get("poster_path")
                    and wanted & {norm(m.get("title")), norm(m.get("original_title"))}]
            if hits:
                return max(hits, key=lambda m: m.get("popularity") or 0)
    return None


def _tmdb_pictures(movie_id: int) -> tuple:
    """A poster (the local one when TMDB has it) and a wide still without text."""
    m = _tmdb(f"/movie/{movie_id}", append_to_response="images", include_image_language="hr,sr,bs,en,null")
    imgs = m.get("images") or {}
    best = lambda items: sorted(items, key=lambda i: (i.get("vote_average") or 0, i.get("width") or 0), reverse=True)
    posters = (best([p for p in imgs.get("posters", []) if p.get("iso_639_1") in ("hr", "sr", "bs")])
               or best([p for p in imgs.get("posters", []) if p.get("iso_639_1") == "en"]))
    stills = best([b for b in imgs.get("backdrops", []) if not b.get("iso_639_1")])
    poster = posters[0]["file_path"] if posters else m.get("poster_path")
    still = stills[0]["file_path"] if stills else m.get("backdrop_path")
    return (f"https://image.tmdb.org/t/p/w780{poster}" if poster else None,
            f"https://image.tmdb.org/t/p/w1280{still}" if still else None)


def film_pictures(events: list, log: list, offline: bool = False) -> None:
    """Sharper posters and wide stills for the cinema's films from TMDB (needs TMDB_API_KEY). Each film is looked up
    once and remembered in .cache/films.json; a film TMDB doesn't know yet is asked about again after a week."""
    films: dict = {}
    for e in events:
        if e.get("kind") == "kino" and e.get("url"):
            films.setdefault(e["url"], []).append(e)
    if not films:
        return
    try:
        cache = json.loads(FILMS_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    today = dt.date.today()
    can_ask = bool(os.environ.get("TMDB_API_KEY", "").strip()) and not offline
    asked = found = 0
    for url, items in films.items():
        known = cache.get(url)
        stale = known and not known.get("poster") and known.get("checked", "") < (today - dt.timedelta(days=7)).isoformat()
        if can_ask and (not known or stale):
            asked += 1
            try:
                original, year = _film_page_facts(url)
                hit = _tmdb_match([original, items[0]["title"]], year)
                poster, still = _tmdb_pictures(hit["id"]) if hit else (None, None)
                known = cache[url] = {"tmdb": hit["id"] if hit else None, "poster": poster, "backdrop": still,
                                      "checked": today.isoformat()}
            except Exception as ex:
                log.append(f"TMDB: {items[0]['title']} not looked up ({err(ex)})")
                if "401" in str(ex):  # wrong key: don't try every film
                    can_ask = False
                continue
        if known:
            known["seen"] = today.isoformat()
            if known.get("poster"):
                found += 1
                for e in items:
                    e["image"], e["backdrop"], e["image_credit"] = known["poster"], known.get("backdrop"), "TMDB"
    keep_from = (today - dt.timedelta(days=60)).isoformat()
    cache = {u: v for u, v in cache.items() if v.get("seen", "") >= keep_from}
    FILMS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FILMS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    log.append(f"TMDB: sharper pictures for {found} of {len(films)} films ({asked} looked up now)"
               + ("" if os.environ.get("TMDB_API_KEY") or offline else " · add TMDB_API_KEY to look up new films"))

# ------------------------------------------------------------------ your Google Sheet: events and pictures by hand

SHEET_CATEGORIES = ["Pozorište", "Muzika", "Film", "Izložbe", "Za djecu", "Razgovori", "Ostalo"]


def _sheet_rows(url: str) -> list:
    """One tab as a list of rows, each a dict keyed by its folded heading ("naslov", "datum", …)."""
    raw = _download(url, "text/csv").decode("utf-8-sig", "replace")
    if raw.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raise ValueError("the sheet is not shared as 'Anyone with the link can view'")
    rows = list(csv.reader(io.StringIO(raw)))
    head = [fold(h).strip() for h in rows[0]] if rows else []
    return [dict(zip(head, (c.strip() for c in r))) for r in rows[1:] if any(c.strip() for c in r)]


def _cell(row: dict, name: str) -> str:
    return next((v for k, v in row.items() if k.startswith(name)), "")


def _sheet_picture(value: str):
    """A picture from the sheet: an image link, a Google Drive link (shared with anyone), or a file in the repository."""
    value = value.strip()
    drive = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^#]*&)?id=)([\w-]{20,})", value)
    if drive:
        return f"https://drive.google.com/thumbnail?id={drive.group(1)}&sz=w2000"
    return (value if re.match(r"https?://", value) else value.lstrip("/")) or None


def from_sheet(today: dt.date, horizon: dt.date, log: list) -> tuple:
    """Events and picture swaps from the Google Sheet in SHEET_URL (shared as 'Anyone with the link can view').
    Tab "Događaji": events added by hand. Tab "Slike": a word from a title and a better picture for it.
    Rows starting with "Primjer" (the template's examples) are skipped."""
    sheet = re.search(r"/spreadsheets/d/([\w-]{20,})", os.environ.get("SHEET_URL", ""))
    if not sheet:
        return [], []
    base = f"https://docs.google.com/spreadsheets/d/{sheet.group(1)}"
    events, swaps = [], []
    tab = lambda name: f"{base}/gviz/tq?tqx=out:csv&headers=1&sheet={urllib.parse.quote(name)}"
    try:
        rows = _sheet_rows(tab("Događaji"))
    except Exception as ex:
        log.append(f"Sheet: events not read ({err(ex)})")
        rows = []
    categories = {fold(c): c for c in SHEET_CATEGORIES} | {"kino": "Film"}
    for n, row in enumerate(rows, start=2):
        title, when = one_line(_cell(row, "naslov")), _cell(row, "datum")
        iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", when)
        day = dt.date(*map(int, iso.groups())) if iso else date_from_text(when, today) if when else None
        if fold(title).startswith("primjer"):
            continue  # the template's example row
        if not title or not day:
            log.append(f"Sheet, row {n}: skipped ({'no title' if not title else 'date not recognised, write it as 25.11.2026'})")
            continue
        if day < today or day > horizon:
            continue
        hm = time_from_text(_cell(row, "vrijeme")) if _cell(row, "vrijeme") else None
        e = {"title": title, "start": _day_start(day, hm), "end": None, "all_day": hm is None, "kind": None,
             "venue": one_line(_cell(row, "mjesto")) or None, "hall": one_line(_cell(row, "sala")) or None,
             "price": one_line(_cell(row, "cijena")) or None, "description": _cell(row, "opis") or None,
             "image": _sheet_picture(_cell(row, "slika")), "url": _cell(row, "link") or None,
             "ticket_url": _cell(row, "karte") or None, "source": "kuda.",
             "source_id": f"{day:%Y%m%d}-" + re.sub(r"[^a-z0-9]+", "-", fold(title)).strip("-")[:40]}
        e["category"] = categories.get(fold(_cell(row, "kategorija"))) or categorize(e, "Ostalo")
        if fold(_cell(row, "istakni")) in ("da", "x", "true", "1", "yes"):
            e["highlight"] = True
        e["id"] = make_id("rucno", e)
        events.append(e)
    try:
        for row in _sheet_rows(tab("Slike")):
            word, picture = fold(_cell(row, "naslov")).strip(), _sheet_picture(_cell(row, "slika"))
            if word and picture and not word.startswith("primjer"):
                swaps.append((word, picture))
    except Exception as ex:
        log.append(f"Sheet: pictures not read ({err(ex)})")
    log.append(f"Sheet: {len(events)} events added by hand, {len(swaps)} picture swaps")
    return events, swaps


def swap_pictures(events: list, swaps: list) -> None:
    """The sheet's "Slike" tab: an event whose title contains the word gets that picture (also over TMDB's)."""
    for e in events:
        title = fold(e["title"])
        for word, picture in swaps:
            if word in title:
                e["image"] = picture
                e.pop("backdrop", None)
                e.pop("image_credit", None)
                break

# ------------------------------------------------------------------ probing sources that are still being added

PROBE: list = []  # (source, page, regex of links on it to save too). Add a new site here before writing its reader.


def probe(log: list) -> None:
    report = []
    for name, url, link_rx in PROBE:
        try:
            body = get(url, "text/html,application/json;q=0.9,*/*;q=0.8")
        except Exception as ex:
            report.append({"source": name, "url": url, "error": err(ex)})
            continue
        report.append({"source": name, "url": url, "file": _raw_path(url).name, "bytes": len(body)})
        if not link_rx:
            continue
        links = []
        for href in re.findall(r"href=[\"']([^\"'#]+)", body.decode("utf-8", "replace"), re.I):
            link = urllib.parse.urljoin(url, html.unescape(href.strip()))
            if re.match(link_rx, link) and link != url and link not in links:
                links.append(link)
        for link in links[:4]:
            try:
                b = get(link, "text/html")
                report.append({"source": name, "url": link, "file": _raw_path(link).name, "bytes": len(b)})
            except Exception as ex:
                report.append({"source": name, "url": link, "error": err(ex)})
    (RAW["dir"] / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in report if "file" in r)
    log.append(f"probe: saved {ok} of {len(report)} pages from new sources")

def best_src(img_tag: str, base: str) -> str | None:
    """A mid-size picture (about 800px wide) from an <img> tag's srcset, else its src."""
    srcset = re.search(r"srcset=[\"']([^\"']+)", img_tag)
    if srcset:
        options = []
        for part in srcset.group(1).split(","):
            bits = part.strip().split()
            if len(bits) == 2 and bits[1].endswith("w") and bits[1][:-1].isdigit():
                options.append((int(bits[1][:-1]), bits[0]))
        if options:
            options.sort()
            wide = [u for w, u in options if w >= 600]
            return urllib.parse.urljoin(base, html.unescape(wide[0] if wide else options[-1][1]))
    src = re.search(r"src=[\"']([^\"']+)", img_tag)
    return urllib.parse.urljoin(base, html.unescape(src.group(1))) if src else None


def _day_start(day: dt.date, hm=None) -> dt.datetime:
    return local(dt.datetime(day.year, day.month, day.day, *(hm or (0, 0))))

# ------------------------------------------------------------------ Gradsko pozorište Jazavac (Modern Events Calendar)

def _split_caps_title(raw: str) -> tuple:
    """'ŠEĆER JE SITAN, OSIM KAD JE KOCKA, komedija N. Pejakovića' -> (caps title, 'komedija N. Pejakovića')."""
    best = (raw, "")
    for m in re.finditer(r",\s+", raw):
        head = raw[:m.start()]
        if head and not any(c.islower() for c in head):
            best = (head, raw[m.end():])
    return best


def _mec_times(base: str, event_id: str, page_url: str) -> tuple:
    """(start, end, None) from the event's iCal export, or (None, None, (h, m)) from its page, or nothing."""
    try:
        text = get(f"{base}/?method=ical&id={event_id}", "text/calendar").decode("utf-8", "replace")
        if "BEGIN:VEVENT" in text:
            ev = parse_ics(text)[0]
            start, all_day = _ics_time(ev.get("DTSTART"))
            if start and not all_day:
                return start, _ics_time(ev.get("DTEND"))[0], None
    except Exception:
        pass
    try:
        page = get(page_url, "text/html").decode("utf-8", "replace")
        block = re.search(r"(?is)mec-single-event-time.{0,800}", page)
        hm = time_from_text(strip_html(block.group(0))) if block else None
        if hm:
            return None, None, hm
    except Exception:
        pass
    return None, None, None


def collect_mec(src: dict, today: dt.date, horizon: dt.date, log: list) -> list:
    url = src["base"] + src["html"][0]
    try:
        page = get(url, "text/html").decode("utf-8", "replace")
    except Exception as ex:
        log.append(f"{src['name']}: {src['html'][0]} failed ({err(ex)})")
        return []
    events, timed = [], 0
    for art in re.findall(r"(?is)<article[^>]*mec-event-article.*?</article>", page):
        label = re.search(r"mec-start-date-label[^>]*>([^<]+)<", art)
        link = re.search(r"(?is)<h4[^>]*mec-event-title[^>]*>\s*<a[^>]*data-event-id=\"(\d+)\"[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", art)
        day = date_from_text(label.group(1), today) if label else None
        if not (link and day) or day < today or day > horizon:
            continue
        event_id, href = link.group(1), html.unescape(link.group(2))
        title, subtitle = _split_caps_title(one_line(link.group(3)))
        start, end, hm = _mec_times(src["base"], event_id, href) if len(events) < 30 else (None, None, None)
        if hm:
            start = _day_start(day, hm)
        timed += start is not None
        img = re.search(r"<img[^>]+>", art)
        soldout = re.search(r"class=[\"']?soldout[\"']?>([^<]+)<", art)
        events.append({
            "source_id": event_id, "title_original": title, "title": to_latin(title),
            "start": start or _day_start(day), "end": end, "all_day": start is None,
            "hall": None, "price": None, "image": best_src(img.group(0), url) if img else None,
            "url": href, "ticket_url": None, "description": to_latin(subtitle) or None,
            "note": one_line(soldout.group(1)) if soldout else None, "source_categories": [],
        })
    log.append(f"{src['name']}: repertoire page, {len(events)} shows ({timed} with a start time)")
    return events

# ------------------------------------------------------------------ Gigstix (ticket site)

def collect_gigstix(src: dict, today: dt.date, horizon: dt.date, log: list) -> list:
    url = src["base"] + src["html"][0]
    try:
        page = get(url, "text/html").decode("utf-8", "replace")
    except Exception as ex:
        log.append(f"{src['name']}: listing failed ({err(ex)})")
        return []
    events = []
    for card in re.split(r"<div class=\"gt-event-style-1\"", page)[1:]:
        link = re.search(r"(?is)<div class=\"gt-title\">\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", card)
        date_txt = re.search(r"(?is)<div class=\"gt-date\">.*?<span>([^<]+)</span>", card)
        day = date_from_text(date_txt.group(1), today) if date_txt else None
        if not (link and day) or day < today or day > horizon:
            continue
        href, title = html.unescape(link.group(1)), one_line(link.group(2))
        cats = re.search(r"(?is)<div class=\"gt-category\">(.*?)</div>", card)
        info = re.search(r"(?is)<div class=\"gt-text\">(.*?)</div>", card)
        parts = [p.strip() for p in one_line(info.group(1)).split("|")] if info else []
        img = re.search(r"<img[^>]+>", card)
        e = {
            "source_id": href.rstrip("/").rsplit("/", 1)[-1][:60], "title_original": title, "title": to_latin(title),
            "start": _day_start(day), "end": None, "all_day": True,
            "venue": to_latin(parts[2]) if len(parts) >= 3 else None, "hall": None, "price": None,
            "image": best_src(img.group(0), url) if img else None, "url": href, "ticket_url": None,
            "description": None,
            "source_categories": [to_latin(one_line(c)) for c in re.findall(r"<a[^>]*>([^<]+)</a>", cats.group(1))] if cats else [],
        }
        try:
            detail = get(href, "text/html").decode("utf-8", "replace")
            about = re.search(r"(?is)gt-section-title\">\s*O doga\w*\s*</div>\s*<div class=\"gt-content\">(.*?)</div>", detail)
            if about:
                paragraphs = [one_line(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", about.group(1))]
                hm = time_from_text(" ".join(paragraphs[:2]))
                if hm:
                    e["start"], e["all_day"] = _day_start(day, hm), False
                e["description"] = to_latin(one_line(" ".join(paragraphs[1:] or paragraphs), 280)) or None
            ticket = re.search(r"href=\"(https://bilet\.gigstix\.ba/[^\"]*sectionGroup[^\"]*)\"", detail)
            e["ticket_url"] = ticket.group(1) if ticket else None
        except Exception as ex:
            log.append(f"{src['name']}: event page {href} failed ({err(ex)})")
        events.append(e)
    log.append(f"{src['name']}: listing, {len(events)} events in Banja Luka")
    return events

# ------------------------------------------------------------------ Cinestar (films with showtimes)

def collect_cinestar(src: dict, today: dt.date, horizon: dt.date, log: list) -> list:
    url = src["base"] + src["html"][0]
    try:
        page = get(url, "text/html").decode("utf-8", "replace")
    except Exception as ex:
        log.append(f"{src['name']}: programme failed ({err(ex)})")
        return []
    events, films = [], 0
    for block in re.split(r"<div class=\"movie-item", page)[1:]:
        link = re.search(r"(?is)<a href=\"([^\"]+)\">\s*<h2>(.*?)</h2>", block)
        if not link:
            continue
        films += 1
        href = html.unescape(link.group(1))
        # badges such as "Pretprodaja" or "Ponosni na domaći film" sit inside the title as <span class='filmlabel'>
        labels = [one_line(x) for x in re.findall(r"(?is)filmlabel[^>]*>(.*?)</span>", link.group(2))]
        title = one_line(re.sub(r"(?is)<span[^>]*filmlabel.*?</span>", " ", link.group(2)))
        label_text = {"pretprodaja": "Ulaznice u pretprodaji", "ponosni na domaci film": "Domaći film"}
        genre_m = re.search(r"data-genre=\"([^\"]*)\"", block)
        genre = genre_m.group(1).split("/")[0].strip() if genre_m else ""
        poster = re.search(r"(?is)class=\"poster-wrapper\">\s*<img[^>]+src=\"([^\"]+)\"", block)
        duration = re.search(r"class=\"duration\">\s*([^<]+?)\s*<", block)
        desc = re.search(r"(?is)</div>\s*<p>(.*?)</p>", block)
        film_id = (re.search(r"/(\d+)/?$", href) or re.search(r"(\d+)", href) or re.search(r"(.+)", title)).group(1)
        kind = fold(genre)
        base_event = {
            "title_original": title, "title": to_latin(title), "end": None, "all_day": False, "price": None,
            "image": urllib.parse.urljoin(url, html.unescape(poster.group(1))) if poster else None,
            "url": href, "description": to_latin(one_line(desc.group(1), 280)) if desc else None,
            "genre": to_latin(genre) or None, "duration": one_line(duration.group(1)) if duration else None,
            "category": {"opera": "Muzika", "balet": "Pozorište"}.get(kind, "Kino"),
            "note": " · ".join(label_text.get(fold(x), x) for x in labels) or None,
            "source_categories": [genre] if genre else [],
        }
        for day_html in re.split(r"<div class=\"day-wrapper\">", block)[1:]:
            label = re.search(r"<div class=\"day\">([^<]+)</div>", day_html)
            day = date_from_text(label.group(1), today) if label else None
            if not day or day < today or day > horizon:
                continue
            shows = []
            for perf_href, inner in re.findall(r"(?is)<a href=\"([^\"]+)\" class=\"perf\"[^>]*>(.*?)</a>", day_html):
                t = re.search(r"class=\"time\">\s*([^<]+?)\s*<", inner)
                hm = time_from_text(t.group(1)) if t else None
                if not hm:
                    continue
                hall = re.search(r"class=\"venue\">\s*([^<]+?)\s*<", inner)
                fmt = re.search(r"class=\"format\"[^>]*>\s*([^<]+?)\s*<", inner)
                shows.append({"time": f"{hm[0]:02d}:{hm[1]:02d}", "hall": hall.group(1) if hall else None,
                              "format": fmt.group(1) if fmt else None, "ticket": html.unescape(perf_href)})
            if not shows:
                continue
            shows.sort(key=lambda s: s["time"])
            if kind in ("opera", "balet"):  # opera and ballet broadcasts are one-off events
                for s in shows:
                    hm = tuple(map(int, s["time"].split(":")))
                    events.append(dict(base_event, source_id=f"{film_id}-{day:%Y%m%d}-{hm[0]:02d}{hm[1]:02d}",
                                       start=_day_start(day, hm), hall=s["hall"], ticket_url=s["ticket"]))
            else:
                hm = tuple(map(int, shows[0]["time"].split(":")))
                events.append(dict(base_event, source_id=f"{film_id}-{day:%Y%m%d}", kind="kino", showtimes=shows,
                                   start=_day_start(day, hm), hall=None, ticket_url=None))
    log.append(f"{src['name']}: programme, {films} films, {len(events)} film-days")
    return events


def load_rules(path: Path) -> tuple:
    """rules.json: words in titles. "keep" and "drop" override the automatic culture filter; "highlight" pins an
    event to "Ne propusti" on the page (and makes it Preporuka dana on its day). "titles" lists titles written the way
    they should look, for names that sentence case gets wrong ("Kroz život sa Veljom")."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], [], [], {}
    except ValueError as ex:
        print(f"rules.json is not valid JSON, ignoring it ({ex})")
        return [], [], [], {}
    words = lambda name: [fold(x) for x in data.get(name, []) if x]
    titles = {title_key(t): t.strip() for t in data.get("titles", []) if isinstance(t, str) and t.strip()}
    return words("keep"), words("drop"), words("highlight"), titles

# ------------------------------------------------------------------ plain HTML tables (SC Borik)

class _TableParser(HTMLParser):
    """Text of every cell of every top-level <table>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list = []
        self._depth, self._table, self._row, self._cell = 0, None, None, None

    def _end_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
        self._cell = None

    def _end_row(self):
        self._end_cell()
        if self._row is not None and self._table is not None and any(self._row):
            self._table.append(self._row)
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            if self._row is None:
                self._row = []
            self._cell = []
        elif tag in ("br", "p", "div", "li") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1:
                self._end_row()
                if self._table:
                    self.tables.append(self._table)
                self._table = None
            self._depth = max(0, self._depth - 1)
        elif self._depth == 1 and tag in ("td", "th"):
            self._end_cell()
        elif self._depth == 1 and tag == "tr":
            self._end_row()

    def handle_data(self, data):
        if self._cell is not None and self._depth == 1:
            self._cell.append(data)


_HEADER_WORDS = {
    "date": ("datum", "dan"),
    "time": ("vrijeme", "vreme", "sat", "pocetak", "termin"),
    "venue": ("objekat", "objekt", "sala", "mjesto", "mesto", "lokacija", "dvorana", "teren"),
    "org": ("takmicenje", "organizator", "liga", "kategorija", "uzrast"),
    "title": ("opis", "dogadjaj", "dogadaj", "utakmica", "naziv", "sadrzaj", "program", "manifestacija", "susret", "aktivnost"),
}


def _header_map(row: list) -> dict:
    found = {}
    for idx, cell in enumerate(row):
        words = fold(cell).replace("/", " ").split()
        for field, keys in _HEADER_WORDS.items():
            if field not in found and any(w.startswith(k) for w in words for k in keys):
                found[field] = idx
                break
    return found if len(found) >= 2 and "date" in found else {}


def events_from_tables(page: str, page_url: str, today: dt.date) -> list:
    parser = _TableParser()
    parser.feed(page)
    events = []
    for table in parser.tables:
        cols, current_date = {}, None
        for row in table:
            if not cols:
                cols = _header_map(row)
                if cols:
                    continue
            if cols:
                def cell(field, row=row):
                    i = cols.get(field)
                    return row[i] if i is not None and i < len(row) else ""

                date_txt, time_txt, venue_txt, title_txt, org_txt = (cell(f) for f in ("date", "time", "venue", "title", "org"))
                if not title_txt:
                    rest = [c for i, c in enumerate(row) if i not in cols.values() and c]
                    title_txt = max(rest, key=len) if rest else ""
            else:  # no header row: guess columns from their content
                date_txt = next((c for c in row if date_from_text(c, today)), "")
                time_txt = next((c for c in row if c != date_txt and time_from_text(c)), "")
                rest = [c for c in row if c and c not in (date_txt, time_txt)]
                title_txt = max(rest, key=len) if rest else ""
                venue_txt = next((c for c in rest if c != title_txt and len(c) <= 30), "")
                org_txt = ""
            day = date_from_text(date_txt, today) if date_txt else None
            if day:
                current_date = day
            if not title_txt:
                continue  # a date-only row that heads the rows below it
            day = day or current_date
            if not day:
                continue
            hm = time_from_text(time_txt) if time_txt else time_from_text(date_txt)
            title = one_line(title_txt)
            events.append({
                "source_id": None, "title_original": title, "title": to_latin(title),
                "start": local(dt.datetime(day.year, day.month, day.day, *(hm or (0, 0)))),
                "end": None, "all_day": hm is None,
                "hall": to_latin(venue_txt) or None, "price": None, "image": None,
                "url": page_url, "ticket_url": None, "description": None,
                "organizer": smart_case(to_latin(org_txt)) or None, "source_categories": [],
            })
    return events


def collect_table(src: dict, today: dt.date, log: list) -> list:
    for path in src["html"]:
        url = src["base"] + path
        try:
            page = get(url, "text/html").decode("utf-8", "replace")
        except Exception as ex:
            log.append(f"{src['name']}: {path} failed ({err(ex)})")
            continue
        events = events_from_tables(page, url, today)
        log.append(f"{src['name']}: table on {path}, {len(events)} rows with a date")
        return events
    return []

# ------------------------------------------------------------------ culture filter

SPORT = [r"\bliga\b", r"\bligi\b", r"\bligu\b", r"superlig", r"premijer\s*lig", r"utakmic", r"\bkup\b", r"\bkupa\b",
         r"turnir", r"prvenstv", r"takmicenj", r"rukomet", r"kosark", r"fudbal", r"futsal", r"odbojk", r"karate",
         r"dzudo", r"\bjudo\b", r"tekvondo", r"taekwondo", r"kik\s*-?\s*boks", r"kickbox", r"\bboks", r"\bmma\b",
         r"plivan", r"kuglan", r"atletik", r"gimnastik", r"\bsah\b", r"\bsaha\b", r"tenis", r"hokej", r"memorijal",
         r"sportsk\w*\s+(dan|takmi|igre|igara|manifest|susret|kamp|skol)", r"trening", r"rekreativ", r"maraton", r"\bvs\.?\b", r"\b(kk|rk|fk|zrk|zok|zkk)\s",
         r"seniori", r"juniori", r"kadeti", r"pioniri", r"veterani", r"klizanj", r"biciklis", r"streljast",
         r"rvanj", r"badminton", r"olimpijad"]

FOLK = [r"narodn\w*\s+(muzik|pjesm|pesm|kol[ao]?\b|veselj|zabav|orkest|pjevac|pevac)", r"narodnjac",
        r"turbo\s*-?\s*folk", r"\bfolk\b", r"kafansk"]

# Well-known folk / pop-folk performers. Extend this list as you go.
FOLK_ARTISTS = [r"seka aleksic", r"\bceca\b", r"aca lukas", r"halid beslic", r"mile kitic", r"sasa matic",
                r"dragana mirkovic", r"lepa brena", r"aco pejovic", r"dara bubamara", r"indira radic",
                r"jelena karleusa", r"milica pavlovic", r"aleksandra prijovic", r"ana bekuta", r"mitar miric",
                r"haris dzinovic", r"boban rajovic", r"dejan matic", r"zorica brunclik", r"sinan sakic",
                r"saban saulic", r"semsa suljakovic", r"hanka paldum", r"dzej ramadanovski", r"tropico band",
                r"vesna zmijanac", r"mira skoric", r"kemal malovcic"]

REVIEW = [r"folklor", r"\bkud\b", r"izvorn", r"sevdah", r"\betno\b", r"ethno", r"starogradsk", r"tamburas", r"gusl",
          r"\bkolo\b"]

STRONG_CULTURE = [r"\bhor\b", r"\bhora\b", r"horsk", r"orkest", r"filharmon", r"simfon", r"balet", r"\bopera\b",
                  r"\bopere\b", r"mjuzikl", r"predstav", r"pozorist", r"teatar", r"izlozb", r"\bfilm", r"\bkino",
                  r"recital", r"jazz", r"dzez", r"\brok\b", r"rock", r"klasicn", r"kamern", r"pijanist", r"klavir",
                  r"smotra", r"kabare", r"stand\s*-?\s*up", r"stend\s*-?\s*ap", r"knjig", r"promocij", r"tribin", r"predavanj", r"sonat"]

GENERIC_SHOW = [r"koncer", r"nastup", r"spektakl", r"turneja", r"festival", r"show\b"]

CATEGORY_RULES = [
    ("Za djecu", [r"za djecu", r"za decu", r"djecj", r"djecij", r"decij", r"lutkar", r"bajk", r"malisan"]),
    ("Film", [r"\bfilm", r"\bkino", r"projekcij"]),
    ("Izložbe", [r"izlozb", r"postavk", r"vernisaz"]),
    ("Razgovori", [r"promocij", r"knjig", r"tribin", r"predavanj", r"razgovor", r"poezij", r"knjizevn",
                   r"okrugli sto", r"panel"]),
    ("Muzika", [r"koncer", r"jazz", r"dzez", r"filharmon", r"orkest", r"\bhor\b", r"horsk", r"recital", r"gudack",
                r"kvartet", r"\btrio\b", r"\bbend", r"simfon", r"\bopera\b", r"pijanist", r"gitar"]),
    ("Pozorište", [r"predstav", r"premijer", r"\bdram", r"komedij", r"monodram", r"teatar", r"pozorist", r"balet",
                   r"mjuzikl", r"repriz", r"stand\s*-?\s*up", r"stend\s*-?\s*ap", r"kabare"]),
]


def _any(patterns: list, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(e: dict, trust: str) -> tuple:
    """Returns ('kept' | 'review' | 'excluded', reason)."""
    parts = [e.get("title_original") or "", e.get("title") or "", " ".join(e.get("source_categories") or [])]
    if trust != "culture":  # for mixed venues the organiser/league column says a lot
        parts += [e.get("organizer") or "", e.get("description") or ""]
    text = fold(" ".join(parts))
    if _any(SPORT, text):
        return "excluded", "sport"
    if _any(FOLK, text) or _any(FOLK_ARTISTS, text):
        return "excluded", "folk music"
    if _any(REVIEW, text):
        return "review", "folklore or traditional music: your call"
    if trust == "culture" or _any(STRONG_CULTURE, text):
        return "kept", ""
    if _any(GENERIC_SHOW, text):
        return "review", "show at a sports venue with no clear genre"
    return "excluded", "not culture"


def categorize(e: dict, default: str) -> str:
    """Title first, then the site's own categories; the description only when the venue has no default."""
    passes = [fold(f"{e.get('title_original') or ''} {e.get('title') or ''}"),
              fold(" ".join(e.get("source_categories") or []))]
    if default == "Ostalo":
        passes.append(fold(e.get("description") or ""))
    for text in passes:
        for name, patterns in CATEGORY_RULES:
            if _any(patterns, text):
                return name
    return default

# ------------------------------------------------------------------ output

FIELDS = ["id", "kind", "title", "title_original", "category", "start", "end", "all_day", "venue", "hall", "price", "age",
          "organizer", "genre", "duration", "showtimes", "note", "image", "backdrop", "image_credit", "url", "ticket_url",
          "description", "source", "also_listed_at", "highlight", "reason"]


def make_id(key: str, e: dict) -> str:
    if e.get("source_id"):
        return f"{key}-{e['source_id']}"
    basis = f"{e.get('title')}|{e['start'].isoformat()}|{e.get('hall') or ''}"
    return f"{key}-" + hashlib.sha1(basis.encode()).hexdigest()[:10]


def dedupe(events: list) -> list:
    seen, out = {}, []
    for e in events:
        key = (re.sub(r"[^a-z0-9]+", " ", fold(e["title"])).strip(), e["start"].date().isoformat())
        first = seen.get(key)
        if first is None:
            seen[key] = e
            out.append(e)
            continue
        if first.get("all_day") and not e.get("all_day"):  # e.g. Jazavac lists the date, Borik the time
            first["start"], first["end"], first["all_day"] = e["start"], e.get("end"), False
        for k, v in e.items():  # fill gaps in the first copy
            if first.get(k) in (None, "", []) and v not in (None, "", []):
                first[k] = v
        if e["source"] != first["source"]:
            first.setdefault("also_listed_at", []).append(e["source"])
    return out


def serialize(e: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = e.get(k)
        if isinstance(v, dt.datetime):
            v = v.isoformat()
        if k == "title_original" and not re.search(r"[Ѐ-ӿ]", v or ""):
            continue  # keep the original title only when it was in Cyrillic
        if v is None or v == "" or v == []:
            continue
        out[k] = v
    return out


def when(e: dict) -> str:
    return e["start"].strftime("%d.%m.") if e.get("all_day") else e["start"].strftime("%d.%m. %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect culture events in Banja Luka.")
    ap.add_argument("--days", type=int, default=90, help="how many days ahead (default 90)")
    ap.add_argument("--only", choices=[s["key"] for s in SOURCES], help="run one source")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data"), help="output folder")
    ap.add_argument("--offline", action="store_true", help="re-run on the pages saved by the last online run")
    ap.add_argument("--probe", action="store_true", help="also save pages of sources that are still being added")
    ap.add_argument("--today", help=argparse.SUPPRESS)  # YYYY-MM-DD, for testing
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    RAW["dir"], RAW["offline"] = out / "raw", args.offline
    index_file = out / "raw" / "index.json"
    now = dt.datetime.now(TZ) if TZ else dt.datetime.now()
    today = now.date()
    if args.offline and index_file.exists():  # use the same dates as the saved run, so the saved pages match
        saved = json.loads(index_file.read_text(encoding="utf-8"))
        today, args.days = dt.date.fromisoformat(saved["today"]), saved.get("days", args.days)
    if args.today:
        today = dt.date.fromisoformat(args.today)
    horizon = today + dt.timedelta(days=args.days)
    log: list = []
    kept, review, excluded = [], [], []
    fetched = 0
    keep_rules, drop_rules, highlight_rules, title_fixes = load_rules(Path(__file__).resolve().parent / "rules.json")

    for src in SOURCES:
        if args.only and src["key"] != args.only:
            continue
        kind = src["kind"]
        if kind == "tec":
            raw = collect_tec(src, args.days, today, log)
        elif kind == "table":
            raw = collect_table(src, today, log)
        elif kind == "mec":
            raw = collect_mec(src, today, horizon, log)
        elif kind == "gigstix":
            raw = collect_gigstix(src, today, horizon, log)
        elif kind == "cinestar":
            raw = collect_cinestar(src, today, horizon, log)
        else:
            raw = []
        fetched += len(raw)
        if not raw:
            log.append(f"WARNING: {src['name']} gave 0 events. The site may have changed.")
        for e in raw:
            if not e.get("start") or not e.get("title"):
                continue
            last_day = (e.get("end") or e["start"]).date()
            if e["start"].date() > horizon or last_day < today:
                continue
            e["source"] = src["name"]
            if not e.get("venue"):
                e["venue"] = src["venue"]
            e["id"] = make_id(src["key"], e)
            e["category"] = e.get("category") or categorize(e, src["default_category"])
            title = re.sub(r"(?i)^\s*predstav[ae]\s*:\s*", "", e["title"]).strip()
            e["title"] = smart_case(title, sentence=True, context=e.get("description") or "") or e["title"]
            e["title"] = title_fixes.get(title_key(e["title"]), e["title"])  # rules.json "titles"
            e["hall"] = clean_hall(e.get("hall"), src.get("aliases", []))
            venue_by_hall = src.get("hall_venues", {})
            if e.get("hall") and fold(e["hall"]) in venue_by_hall:
                e["venue"], e["hall"] = venue_by_hall[fold(e["hall"])], None
            if e.get("kind") == "kino":
                verdict, reason = "kept", ""
            else:
                verdict, reason = classify(e, src["trust"])
                words = fold(f"{e.get('title_original') or ''} {e['title']}")
                if any(k in words for k in drop_rules):
                    verdict, reason = "excluded", "removed in rules.json"
                elif any(k in words for k in keep_rules):
                    verdict, reason = "kept", ""
                if any(k in words for k in highlight_rules):
                    e["highlight"] = True
            e["reason"] = reason or None
            {"kept": kept, "review": review, "excluded": excluded}[verdict].append(e)

    sheet_events, swaps = ([], []) if args.only or args.offline else from_sheet(today, horizon, log)
    kept[:0] = sheet_events  # first, so a correction in the sheet wins over the same event from the venue
    for src in SOURCES:
        if src.get("enrich_images") and not (args.only and src["key"] != args.only):
            enrich_images(src, kept + review, log)
    film_pictures(kept, log, args.offline)
    swap_pictures(kept, swaps)
    if args.probe and not args.offline:
        probe(log)

    kept = sorted(dedupe(kept), key=lambda e: e["start"])
    review.sort(key=lambda e: e["start"])
    excluded.sort(key=lambda e: e["start"])

    if not args.offline:
        index_file.parent.mkdir(parents=True, exist_ok=True)
        index_file.write_text(json.dumps({"today": today.isoformat(), "days": args.days}), encoding="utf-8")
    meta = {"generated_at": now.isoformat(timespec="seconds"), "timezone": "Europe/Sarajevo",
            "days_ahead": args.days, "offline_rerun": args.offline, "log": log}
    for name, items in (("events", kept), ("review", review), ("excluded", excluded)):
        payload = dict(meta, count=len(items), events=[serialize(e) for e in items])
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (out / f"{name}.json").write_text(text, encoding="utf-8")
        if name == "events":  # the page loads this copy; a .js file works even when index.html is opened from disk
            (out / "events.js").write_text(f"window.KUDA_DATA = {text};\n", encoding="utf-8")

    reasons: dict = {}
    for e in excluded:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    print(f"kuda. collector v0.5 · {now:%d.%m.%Y %H:%M}" + (" · offline re-run" if args.offline else ""))
    for line in log:
        print("  " + line)
    print(f"\nKept {len(kept)} · to review {len(review)} · excluded {len(excluded)}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) + ")" if reasons else ""))
    for label, items in (("Kept", kept), ("To review", review), ("Excluded", excluded)):
        if items:
            print(f"\n{label}:")
            for e in items[:60]:
                extra = f"  [{e['reason']}]" if label != "Kept" and e.get("reason") else f"  [{e['category']}]"
                hall = f" · {e['hall']}" if e.get("hall") else ""
                print(f"  {when(e):<12} {e['title']} — {e['source']}{hall}{extra}")
    print(f"\nSaved to {out}/events.json, events.js, review.json, excluded.json")
    return 0 if fetched else 1


if __name__ == "__main__":
    sys.exit(main())
