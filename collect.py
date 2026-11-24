#!/usr/bin/env python3
"""
kuda. — collector for culture events in Banja Luka (v0.3)

Sources (v1): Narodno pozorište RS, Banski dvor, SC Borik.
Culture only: sport and folk music are dropped. Unclear items (folklore, sevdah,
ethno, concerts at a sports venue with no clear genre) go to data/review.json.

Run:   python3 collect.py               # looks 90 days ahead
       python3 collect.py --days 60
       python3 collect.py --only borik  # one source (np, bd, borik)
       python3 collect.py --offline     # re-run on the pages saved last time (no internet needed)

Needs only Python 3.9+, nothing to install. If Python's SSL certificates are
missing (common on macOS), it falls back to curl.

Writes:
  data/events.json    culture events to publish
  data/review.json    unclear items: decide by hand
  data/excluded.json  dropped items, each with a reason
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
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

UA = "kuda-collector/0.3 (Banja Luka culture listings; one visit per day)"
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
        "base": "https://banskidvor.org", "kind": "tec", "trust": "culture", "default_category": "Ostalo",
        "ics": ["/events/?ical=1", "/events/list/?ical=1"], "html": ["/events/"],
    },
    {
        "key": "borik", "name": "SC Borik", "venue": "SC Borik", "aliases": ["SC Borik", "Sportski centar Borik"],
        "base": "https://scborik.com", "kind": "table", "trust": "mixed", "default_category": "Muzika",
        "html": ["/?page_id=6913"],
    },
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


def smart_case(text: str) -> str:
    """Tame CAPS: an all-caps title becomes Title Case; caps names inside normal text are capitalised."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    all_caps = not any(c.islower() for c in letters)
    out, first = [], True
    for tok in re.split(r"(\W+)", text):
        if not tok or not any(c.isalpha() for c in tok):
            out.append(tok)
            continue
        if tok.isupper() and tok in _KEEP_UPPER:
            out.append(tok)
        elif all_caps:
            low = tok.lower()
            out.append(low if low in _SMALL_WORDS and not first else low[:1].upper() + low[1:])
        elif tok.isupper() and len(tok) >= 4:
            out.append(tok[:1] + tok[1:].lower())
        else:
            out.append(tok)
        first = False
    return "".join(out)


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
         r"sportsk", r"trening", r"rekreativ", r"maraton", r"\bvs\.?\b", r"\b(kk|rk|fk|zrk|zok|zkk)\s",
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

FIELDS = ["id", "title", "title_original", "category", "start", "end", "all_day", "venue", "hall", "price", "age",
          "organizer", "image", "url", "ticket_url", "description", "source", "also_listed_at", "reason"]


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

    for src in SOURCES:
        if args.only and src["key"] != args.only:
            continue
        raw = collect_tec(src, args.days, today, log) if src["kind"] == "tec" else collect_table(src, today, log)
        fetched += len(raw)
        if not raw:
            log.append(f"WARNING: {src['name']} gave 0 events. The site may have changed.")
        for e in raw:
            if not e.get("start") or not e.get("title"):
                continue
            last_day = (e.get("end") or e["start"]).date()
            if e["start"].date() > horizon or last_day < today:
                continue
            e.update(source=src["name"], venue=src["venue"])
            e["id"] = make_id(src["key"], e)
            e["category"] = categorize(e, src["default_category"])
            e["title"] = smart_case(re.sub(r"(?i)^\s*predstav[ae]\s*:\s*", "", e["title"]).strip()) or e["title"]
            e["hall"] = clean_hall(e.get("hall"), src.get("aliases", []))
            verdict, reason = classify(e, src["trust"])
            e["reason"] = reason or None
            {"kept": kept, "review": review, "excluded": excluded}[verdict].append(e)

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
    print(f"kuda. collector v0.3 · {now:%d.%m.%Y %H:%M}" + (" · offline re-run" if args.offline else ""))
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
