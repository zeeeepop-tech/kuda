#!/usr/bin/env python3
"""
kuda. — builds the website folder (_site) from data/events.json.

    python3 build.py

Writes _site/ with:
  index.html + data/events.js   the homepage (events now carry their page and picture paths)
  e/<event>/index.html          one page per event, with link previews and Google event data
  e/<event>/kalendar.ics        "add to calendar" file for that event
  img/                          pictures resized for the site; posters keep their whole design
  og.png, favicon-32.png, apple-touch-icon.png   share image and icons
  sitemap.xml, robots.txt, 404.html

Runs on GitHub every morning after collect.py. Pictures need Pillow (pip install pillow) and the
share image and icons need Google Chrome; without them the pages still build, using the venues'
original pictures. images.json lets you swap in better pictures (see the file).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

SITE_URL = "https://kudaugrad.com"
CONTACT = "kudaugrad@gmail.com"  # corrections, questions, posters
TAGLINE = "Pozorište, koncerti, film, izložbe i razgovori u Banjaluci, svaki dan na jednom mjestu."
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"
CACHE = ROOT / ".cache"  # downloaded and resized pictures, kept between runs
UA = "kuda-build/0.1 (Banja Luka culture listings; kudaugrad.com)"
FONTS = ("https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800"
         "&family=Inter:wght@400;500;600;700&display=swap")

try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:
    Image = None

DAYS = ["ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja"]
MONTHS = ["januar", "februar", "mart", "april", "maj", "jun", "jul", "avgust", "septembar", "oktobar", "novembar",
          "decembar"]
SHORT_VENUE = {"Narodno pozorište Republike Srpske": "Narodno pozorište RS", "Banski dvor Kulturni centar": "Banski dvor"}
SCHEMA_TYPE = {"Pozorište": "TheaterEvent", "Muzika": "MusicEvent", "Film": "ScreeningEvent", "Kino": "ScreeningEvent",
               "Izložbe": "ExhibitionEvent", "Za djecu": "ChildrensEvent", "Razgovori": "EducationEvent"}
TILE = {"Pozorište": ("#FF4D00", "#0F0F0F"), "Muzika": ("#2B4BFF", "#FFFFFF"), "Izložbe": ("#F5D90A", "#0F0F0F"),
        "Film": ("#0F5132", "#E9F3EC"), "Kino": ("#0F5132", "#E9F3EC"), "Za djecu": ("#FF9EC4", "#0F0F0F"),
        "Razgovori": ("#B9B3FF", "#0F0F0F")}

# ------------------------------------------------------------------ small helpers


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def fold(s) -> str:
    s = str(s or "").lower().replace("đ", "dj")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def slugify(s, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")[:limit].rstrip("-") or "dogadjaj"


def long_date(d: dt.datetime) -> str:
    return f"{DAYS[d.weekday()].capitalize()}, {d.day}. {MONTHS[d.month - 1]} {d.year}."


def short_date(d: dt.datetime) -> str:
    return f"{DAYS[d.weekday()][:3].capitalize()} {d.day}. {MONTHS[d.month - 1][:3]}."


def date_range(start: dt.datetime, end: dt.datetime) -> str:
    """The days of an event that runs over several days: "1–20. decembar" or "28. novembar – 5. januar"."""
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day}. {MONTHS[end.month - 1]}"
    return f"{start.day}. {MONTHS[start.month - 1]} – {end.day}. {MONTHS[end.month - 1]}"


def place(e: dict) -> str:
    return SHORT_VENUE.get(e.get("venue"), e.get("venue") or e.get("source") or "")


def utc(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(",.;:–-") + "…"


ROLES = ("Autor|Autorka|Pisac|Režija|Reditelj|Rediteljka|Dramaturg|Dramaturškinja|Dramatizacija|Adaptacija|Prevod|"
         "Prevodilac|Scenograf|Scenografkinja|Scenografija|Kostimograf|Kostimografkinja|Kostimografija|Kompozitor|"
         "Kompozitorka|Koreograf|Koreografkinja|Koreografija|Dizajn svjetla|Lektor|Lektorka|Producent|Producentkinja|"
         "Sufler i inspicijent|Inspicijent|Asistent režije|Dirigent|Uloge|Igraju")


def credits(s: str) -> str:
    return re.sub(rf"\s+(?=(?:{ROLES})\s*:)", "\n", s or "")

# ------------------------------------------------------------------ pictures


WP_SIZED = re.compile(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp)$)", re.I)


def download(url: str) -> bytes:
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")  # spaces, č, ć … in file names
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/avif,image/webp,image/*;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(30_000_000)


def fetch(src: str) -> bytes | None:
    """A picture from the web (cached) or from a file in this folder."""
    if not re.match(r"https?://", src):
        path = ROOT / src
        return path.read_bytes() if path.is_file() else None
    path = CACHE / "src" / hashlib.sha1(src.encode()).hexdigest()[:24]
    if path.exists():
        return path.read_bytes()
    tries = [src]
    plain = src.split("?")[0]
    if "/wp-content/uploads/" in plain and WP_SIZED.search(plain):
        tries.insert(0, WP_SIZED.sub("", plain))  # "…-732x1024.jpg" → the original "….jpg"
    data, problem = None, None
    for url in tries:
        try:
            data = download(url)
            break
        except Exception as ex:
            problem = ex
    if not data:
        print(f"  picture not downloaded: {src} ({problem})")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    time.sleep(0.3)  # be gentle with the venues' servers
    return data


def framed(im, w: int, h: int):
    """The whole picture centred on a blurred, darkened copy of itself, so posters keep their text.
    Small pictures are enlarged to fill the height (up to 2.5×): Cinestar's posters are only 300×400."""
    back = ImageOps.fit(im, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(max(w, h) // 40))
    back = Image.blend(back, Image.new("RGB", (w, h), (15, 15, 15)), 0.5)
    scale = min(w / im.width, h / im.height, 2.5)
    front = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    back.paste(front, ((w - front.width) // 2, (h - front.height) // 2))
    return back


def shrunk(im, w: int, h: int):
    out = im.copy()
    out.thumbnail((w, h), Image.LANCZOS)
    return out


def pictures(e: dict) -> dict:
    """Resized copies of an event's picture (card, large, share image). {} keeps the original."""
    src = e.get("image")
    if not src or Image is None:
        return {}
    kino = e.get("kind") == "kino"
    wide = e.get("backdrop")  # a film's wide still from TMDB
    key = hashlib.sha1((f"{src}|{wide}|2" if wide else f"{src}|2").encode()).hexdigest()[:16] + ("k" if kino else "")
    names = {"thumb": f"img/{key}-t.webp", "large": f"img/{key}-l.webp", "og": f"img/{key}-og.jpg"}
    store = CACHE / "out"
    if not all((store / n).exists() for n in names.values()):
        data = fetch(src)
        if not data:
            return {}
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        except Exception as ex:
            print(f"  picture not readable: {src} ({ex})")
            return {}
        if im.width < 300 or im.height < 200:  # too small to look good large
            return {}
        portrait = im.height > im.width * 1.05
        big = im
        if wide and (wdata := fetch(wide)):
            try:
                big = ImageOps.exif_transpose(Image.open(io.BytesIO(wdata))).convert("RGB")
            except Exception:
                big = im
        tall = big.height > big.width * 1.05
        (store / "img").mkdir(parents=True, exist_ok=True)
        if kino:  # film posters stay posters: the page shows them upright
            thumb = shrunk(im, 360, 540)
        else:
            thumb = framed(im, 720, 540) if portrait else shrunk(im, 720, 540)
        thumb.save(store / names["thumb"], "WEBP", quality=78, method=6)
        (framed(big, 1400, 800) if tall else shrunk(big, 1600, 1000)).save(store / names["large"], "WEBP", quality=80, method=6)
        og = framed(big, 1200, 630) if tall else ImageOps.fit(big, (1200, 630), Image.LANCZOS, centering=(0.5, 0.4))
        og.save(store / names["og"], "JPEG", quality=84, optimize=True, progressive=True)
    for n in names.values():
        (OUT / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store / n, OUT / n)
    return names


def apply_image_rules(events: list) -> None:
    """images.json: better pictures for some events, and venue photos for events that have none."""
    path = ROOT / "images.json"
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except ValueError as ex:
        print(f"images.json is not valid JSON, ignoring it ({ex})")
        return
    by_title = {fold(k): v for k, v in (rules.get("events") or {}).items() if v}
    by_venue = {fold(k): v for k, v in (rules.get("venues") or {}).items() if v}
    for e in events:
        title = fold(f"{e.get('title')} {e.get('title_original') or ''}")
        chosen = next((v for k, v in by_title.items() if k and k in title), None)
        if chosen:
            e["image"] = chosen
        elif not e.get("image"):
            venue = by_venue.get(fold(e.get("venue")))
            if venue:
                e["image"] = venue

# ------------------------------------------------------------------ share image and icons (drawn with Chrome)


def chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if Path(mac).exists() else None


def render_png(markup: str, w: int, h: int, out: Path) -> bool:
    browser = chrome()
    if not browser:
        return False
    page = CACHE / f"render-{out.stem}.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(markup, encoding="utf-8")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=1",
           f"--window-size={w},{h}", "--virtual-time-budget=8000", f"--screenshot={out}", page.as_uri()]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=90)
        return out.exists()
    except Exception as ex:
        print(f"  could not draw {out.name} ({ex})")
        return False


def brand_images() -> None:
    base = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{FONTS}">
<style>html,body{{margin:0;height:100%}}body{{background:#0F0F0F;color:#fff;font-family:Inter,system-ui,sans-serif;overflow:hidden}}
.logo{{font-family:"Bricolage Grotesque",Inter,sans-serif;font-weight:800;letter-spacing:-.045em;line-height:1}}.logo span{{color:#FF4D00}}</style>"""
    og = base + f"""<style>.wrap{{position:relative;height:630px;padding:64px 72px;box-sizing:border-box}}
.logo{{font-size:168px}}.tag{{margin-top:28px;max-width:760px;font-size:38px;line-height:1.25;color:#E4E4DD}}
.url{{position:absolute;left:72px;bottom:56px;font-size:28px;font-weight:600;color:#B5B5AD}}
.shapes{{position:absolute;right:-40px;bottom:-40px;width:420px;height:420px}}
.c{{position:absolute;border-radius:50%}}.a{{width:300px;height:300px;right:150px;bottom:150px;background:#FF4D00}}
.b{{width:230px;height:230px;right:20px;bottom:60px;background:#2B4BFF}}.y{{width:150px;height:150px;right:250px;bottom:20px;background:#F5D90A}}
</style></head><body><div class="wrap"><div class="logo">kuda<span>.</span></div>
<div class="tag">{esc(TAGLINE)}</div><div class="url">kudaugrad.com · ažurira se svako jutro</div>
<div class="shapes"><div class="c a"></div><div class="c b"></div><div class="c y"></div></div></div></body></html>"""
    render_png(og, 1200, 630, OUT / "og.png")
    icon = base + """<style>body{display:flex;align-items:center;justify-content:center}.logo{font-size:118px;transform:translateY(-6px)}</style>
</head><body><div class="logo">k<span>.</span></div></body></html>"""
    if render_png(icon, 180, 180, OUT / "apple-touch-icon.png") and Image is not None:
        Image.open(OUT / "apple-touch-icon.png").convert("RGB").resize((32, 32), Image.LANCZOS).save(OUT / "favicon-32.png")

# ------------------------------------------------------------------ pictures for Instagram: today's stories, a weekend carousel

TILE = {"Pozorište": ("#FF4D00", "#0F0F0F"), "Muzika": ("#2B4BFF", "#FFFFFF"), "Izložbe": ("#F5D90A", "#0F0F0F"),
        "Film": ("#0F5132", "#E9F3EC"), "Kino": ("#0F5132", "#E9F3EC"), "Za djecu": ("#FF9EC4", "#0F0F0F"),
        "Razgovori": ("#B9B3FF", "#0F0F0F"), "Ostalo": ("#0F0F0F", "#FFFFFF")}
MON_SHORT = ["jan.", "feb.", "mar.", "apr.", "maj", "jun", "jul", "avg.", "sep.", "okt.", "nov.", "dec."]
SOCIAL_CSS = """*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{font-family:Inter,system-ui,sans-serif;color:#0F0F0F;background:#fff;overflow:hidden;-webkit-font-smoothing:antialiased}
.d,.logo,.tile{font-family:"Bricolage Grotesque",Inter,sans-serif;font-weight:800;line-height:1}
.d{letter-spacing:-.03em}.logo{letter-spacing:-.045em}.logo span{color:#FF4D00}
.c2,.c3{display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden}.c2{-webkit-line-clamp:2}.c3{-webkit-line-clamp:3}
.one{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pic{position:relative;overflow:hidden;background:#F3F3EF}.pic img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.tile{position:absolute;inset:0;padding:.45em .55em;text-transform:uppercase;line-height:.95;letter-spacing:-.02em;overflow:hidden}
.eyebrow{font-weight:600;letter-spacing:.1em;text-transform:uppercase}
.badge{display:inline-block;padding:12px 24px;border-radius:999px;background:#FF4D00;color:#0F0F0F;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.dots{position:absolute;right:-60px;bottom:-60px;width:520px;height:520px}.dots i{position:absolute;border-radius:50%}
.dots .a{width:360px;height:360px;right:180px;bottom:180px;background:#FF4D00}.dots .b{width:280px;height:280px;right:30px;bottom:70px;background:#2B4BFF}
.dots .y{width:180px;height:180px;right:300px;bottom:30px;background:#F5D90A}"""


def _local_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Sarajevo")).date()
    except Exception:
        return dt.date.today()


def _times(e: dict) -> tuple:
    start = dt.datetime.fromisoformat(e["start"])
    return start, dt.datetime.fromisoformat(e["end"]) if e.get("end") else None


def _runs(e: dict) -> bool:  # an exhibition or anything else over several days
    start, end = _times(e)
    return bool(end) and end - start > dt.timedelta(days=1)


def _pic(e: dict, kind: str, size: int) -> str:
    """The event's resized picture, or its category colour with the title, like the site's tiles."""
    f = OUT / e[kind] if e.get(kind) else None
    if f and f.exists():
        return f'<img src="{f.as_uri()}" alt="">'
    bg, fg = TILE.get(e.get("category"), TILE["Ostalo"])
    return f'<div class="tile" style="background:{bg};color:{fg};font-size:{size}px">{esc(e["title"])}</div>'


def _label(e: dict, day: dt.date) -> str:
    start, end = _times(e)
    if _runs(e) and start.date() != day:
        return f"do {end.day}. {MON_SHORT[end.month - 1]}"
    return "cijeli dan" if e.get("all_day") else f"{start:%H:%M}"


def _page(body: str, w: int, h: int, extra_css: str = "") -> str:
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{FONTS}">'
            f'<style>{SOCIAL_CSS}body{{width:{w}px;height:{h}px;position:relative}}{extra_css}</style></head>'
            f'<body>{body}</body></html>')


def _plural(n: int, one: str, few: str, many: str) -> str:
    return one if n % 10 == 1 and n % 100 != 11 else few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def story_list(day: dt.date, items: list, more: int, films: int, running: int) -> str:
    rows = "".join(f"""<div class="row"><div class="pic th">{_pic(e, "thumb", 30)}</div><div class="tx">
<p class="meta"><b>{esc(_label(e, day))}</b> · {esc(e.get("category") or "")}</p><p class="d t c2">{esc(e["title"])}</p>
<p class="pl one">{esc(place(e))}</p></div></div>""" for e in items)
    rest = [f"još {more} {_plural(more, 'događaj', 'događaja', 'događaja')}" if more else "",
            f"{films} {_plural(films, 'film', 'filma', 'filmova')} u kinu" if films else "",
            f"{running} {_plural(running, 'izložba', 'izložbe', 'izložbi')} u toku" if running else ""]
    rest = [r for r in rest if r]
    note = (", ".join(rest[:-1]) + " i " + rest[-1] if len(rest) > 1 else rest[0]) if rest else ""
    css = """.wrap{position:absolute;left:80px;right:80px;top:250px}.top{display:flex;align-items:center;gap:18px}.logo{font-size:64px}
.city{padding:10px 22px;border:2px solid #D6D6CF;border-radius:999px;font-size:28px;font-weight:600}
.eyebrow{margin-top:48px;font-size:30px;color:#5E5E58}h1{margin-top:16px;font-size:96px;line-height:.95}
.list{margin-top:48px;display:grid;gap:22px}.row{display:flex;gap:28px;align-items:center;padding-bottom:22px;border-bottom:2px solid #E2E2DC}
.th{flex:none;width:150px;height:150px;border-radius:24px}.tx{min-width:0;flex:1}.meta{font-size:28px;color:#5E5E58}.meta b{color:#0F0F0F}
.t{margin-top:8px;font-size:46px;line-height:1.02}.pl{margin-top:8px;font-size:28px;color:#5E5E58}
.note{margin-top:26px;font-size:30px;color:#5E5E58}.cta{position:absolute;left:80px;top:1530px;padding:16px 30px;border-radius:999px;background:#0F0F0F;color:#fff;font-size:32px;font-weight:700}
.cta span{color:#FF4D00}"""
    body = f"""<div class="wrap"><div class="top"><div class="logo">kuda<span>.</span></div><div class="city">Banjaluka</div></div>
<p class="eyebrow">Danas u Banjaluci</p><h1 class="d">{DAYS[day.weekday()].capitalize()},<br>{day.day}. {MONTHS[day.month - 1]}</h1>
<div class="list">{rows}</div>{f'<p class="note">I {esc(note)}.</p>' if note else ""}</div>
<div class="cta">Sve na kudaugrad.com <span>→</span></div>"""
    return _page(body, 1080, 1920, css)


def story_pick(e: dict, day: dt.date) -> str:
    start, _ = _times(e)
    when = f"Danas u {start:%H:%M}" if not e.get("all_day") else "Danas"
    tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in (e.get("price"), e.get("age") and f"Uzrast {e['age']}") if t)
    css = """body{background:#0F0F0F;color:#fff}.logo{position:absolute;left:80px;top:250px;font-size:64px}
.pic{position:absolute;left:80px;right:80px;top:360px;height:690px;border-radius:36px}.badge{position:absolute;left:80px;top:1100px;font-size:28px}
h1{position:absolute;left:80px;right:80px;top:1176px;font-size:84px;line-height:.98}
.meta{position:absolute;left:80px;right:80px;top:1460px;font-size:38px;color:#E4E4DD}.tags{position:absolute;left:80px;top:1530px;display:flex;gap:14px}
.tag{padding:10px 20px;border:2px solid #5E5E58;border-radius:999px;font-size:28px;font-weight:600}"""
    body = f"""<div class="logo">kuda<span>.</span></div><div class="pic">{_pic(e, "large", 72)}</div><div class="badge">Preporuka dana</div>
<h1 class="d c3">{esc(e["title"])}</h1><p class="meta one">{esc(when)} · {esc(place(e))}</p>{f'<div class="tags">{tags}</div>' if tags else ""}"""
    return _page(body, 1080, 1920, css)


def slide_cover(first: dt.date, last: dt.date, picked: int, total: int) -> str:
    days = f"{first.day}–{last.day}. {MONTHS[last.month - 1]}" if first.month == last.month else \
        f"{first.day}. {MONTHS[first.month - 1]} – {last.day}. {MONTHS[last.month - 1]}"
    css = """body{background:#0F0F0F;color:#fff}.logo{position:absolute;left:80px;top:80px;font-size:56px}
.eyebrow{position:absolute;left:80px;top:300px;font-size:30px;color:#B5B5AD}h1{position:absolute;left:80px;right:80px;top:360px;font-size:150px;line-height:.92}
.days{position:absolute;left:80px;top:690px;font-size:64px;color:#FF4D00}.count{position:absolute;left:80px;top:790px;font-size:34px;color:#E4E4DD}
.url{position:absolute;left:80px;bottom:72px;font-size:30px;font-weight:600;color:#B5B5AD}"""
    body = f"""<div class="logo">kuda<span>.</span></div><p class="eyebrow">Kulturna mapa grada</p><h1 class="d">Vikend u<br>Banjaluci</h1>
<p class="d days">{days}</p><p class="count">{f"{total} {_plural(total, 'događaj', 'događaja', 'događaja')} za vikend" if picked == total else f"Izdvojili smo {picked} od {total} događaja"}</p>
<div class="dots"><i class="a"></i><i class="b"></i><i class="y"></i></div><p class="url">kudaugrad.com</p>"""
    return _page(body, 1080, 1350, css)


def slide_event(e: dict, n: int, of: int) -> str:
    start, _ = _times(e)
    when = DAYS[start.weekday()] + ("" if e.get("all_day") else f" {start:%H:%M}")
    css = """.pic{position:absolute;left:0;top:0;width:1080px;height:810px}.eyebrow{position:absolute;left:72px;top:866px;font-size:30px;font-weight:700;color:#FF4D00}
h1{position:absolute;left:72px;right:72px;top:918px;font-size:68px;line-height:1}.pl{position:absolute;left:72px;right:72px;top:1080px;font-size:34px;color:#5E5E58}
.logo{position:absolute;left:72px;bottom:56px;font-size:40px}.n{position:absolute;right:72px;bottom:60px;font-size:28px;font-weight:600;color:#5E5E58}"""
    body = f"""<div class="pic">{_pic(e, "large", 84)}</div><p class="eyebrow">{esc(when)} · {esc(e.get("category") or "")}</p>
<h1 class="d c2">{esc(e["title"])}</h1><p class="pl one">{esc(place(e))}{" · " + esc(e["price"]) if e.get("price") else ""}</p>
<div class="logo">kuda<span>.</span></div><p class="n">{n}/{of}</p>"""
    return _page(body, 1080, 1350, css)


def slide_end(n: int, of: int) -> str:
    css = """body{background:#FF4D00}.lead{position:absolute;left:80px;top:330px;font-size:44px;font-weight:700}
h1{position:absolute;left:80px;right:60px;top:400px;font-size:112px}.sub{position:absolute;left:80px;right:80px;top:560px;font-size:36px;line-height:1.3}
.logo{position:absolute;left:80px;bottom:64px;font-size:48px}.logo span{color:#fff}.n{position:absolute;right:72px;bottom:68px;font-size:28px;font-weight:600}"""
    body = f"""<p class="lead">Sve ostalo za vikend:</p><h1 class="d">kudaugrad.com</h1>
<p class="sub">Predstave, koncerti, filmovi i izložbe za svaki dan.<br><b>Link je u opisu profila.</b></p><div class="logo">kuda<span>.</span></div><p class="n">{n}/{of}</p>"""
    return _page(body, 1080, 1350, css)


def _caption(title: str, groups: list, tail: str, tags: str) -> str:
    lines = [title, ""]
    for head_line, items in groups:
        if head_line:
            lines.append(head_line)
        lines += [f"{t} {e['title']} · {place(e)}" for t, e in items]
        lines.append("")
    return "\n".join(lines + [tail, "", tags]).strip() + "\n"


def social(events: list) -> str:
    """Instagram pictures made from the same events as the site, in _site/social/ with a page to save them from:
    two stories for today (the list, then Preporuka dana) and, for the coming weekend, a carousel."""
    if not chrome():
        return "Instagram pictures skipped (Chrome not found)"
    folder = OUT / "social"
    folder.mkdir(parents=True, exist_ok=True)
    day = _local_today()
    culture = [e for e in events if e.get("kind") != "kino"]
    made = []

    def draw(markup: str, w: int, h: int, name: str) -> None:
        png = folder / f"{name}.png"
        if not render_png(markup, w, h, png):
            return
        if Image is not None:
            Image.open(png).convert("RGB").save(folder / f"{name}.jpg", quality=90)
            png.unlink()
            made.append(f"{name}.jpg")
        else:
            made.append(png.name)

    # today: what's on, then the day's pick (same choice as "Preporuka dana" on the site)
    on = [e for e in culture if _times(e)[0].date() == day]
    running = [e for e in culture if _runs(e) and _times(e)[0].date() < day <= _times(e)[1].date()]
    films = len({fold(e["title"]) for e in events if e.get("kind") == "kino" and _times(e)[0].date() == day})
    on.sort(key=lambda e: (e.get("all_day", False), _times(e)[0]))
    shown = (on or running)[:4]
    if shown:
        draw(story_list(day, shown, max(0, len(on) - len(shown)), films, len(running) if on else 0), 1080, 1920, "danas-1")
    evening = lambda e: not e.get("all_day") and 18 <= _times(e)[0].hour <= 21  # same tie-break as the page
    pick = max(on, key=lambda e: (e.get("score", 0), evening(e), -_times(e)[0].timestamp()), default=None)
    if pick:
        draw(story_pick(pick, day), 1080, 1920, "danas-2")
    today_text = _caption(f"Danas u Banjaluci · {DAYS[day.weekday()]}, {day.day}. {MONTHS[day.month - 1]}",
                          [("", [(_label(e, day), e) for e in on + running])],
                          "Sve na kudaugrad.com" + (f" (i {films} {_plural(films, 'film', 'filma', 'filmova')} u kinu)" if films else ""),
                          "#banjaluka #kultura #kudaugrad")

    # the weekend: Friday to Sunday (from today once it has started)
    friday = day + dt.timedelta(days=(4 - day.weekday()) % 7) if day.weekday() < 4 else day
    sunday = day + dt.timedelta(days=6 - day.weekday())
    weekend = [e for e in culture if friday <= _times(e)[0].date() <= sunday]
    weekend.sort(key=lambda e: _times(e)[0])
    seen, firsts = set(), []
    for e in weekend:
        if fold(e["title"]) not in seen:
            seen.add(fold(e["title"]))
            firsts.append(e)
    picks = sorted(sorted(firsts, key=lambda e: (-e.get("score", 0), not evening(e), _times(e)[0]))[:5], key=lambda e: _times(e)[0])
    weekend_text = ""
    if picks:
        of = len(picks) + 2
        draw(slide_cover(friday, sunday, len(picks), len(weekend)), 1080, 1350, "vikend-1")
        for i, e in enumerate(picks, start=2):
            draw(slide_event(e, i, of), 1080, 1350, f"vikend-{i}")
        draw(slide_end(of, of), 1080, 1350, f"vikend-{of}")
        groups = []
        for d in sorted({_times(e)[0].date() for e in weekend}):
            groups.append((DAYS[d.weekday()].capitalize(), [(_label(e, d), e) for e in weekend if _times(e)[0].date() == d]))
        weekend_text = _caption(f"Vikend u Banjaluci · {friday.day}–{sunday.day}. {MONTHS[sunday.month - 1]}", groups,
                                "Sve događaje, filmove i izložbe nađi na kudaugrad.com (link je u opisu profila).",
                                "#banjaluka #kultura #vikend #kudaugrad")
    (folder / "danas.txt").write_text(today_text, encoding="utf-8")
    if weekend_text:
        (folder / "vikend.txt").write_text(weekend_text, encoding="utf-8")
    (folder / "index.html").write_text(social_page(day, made, today_text, weekend_text), encoding="utf-8")
    return f"Instagram pictures: {len(made)} (stories for today, weekend carousel)"


def social_page(day: dt.date, made: list, today_text: str, weekend_text: str) -> str:
    v = day.strftime("%Y%m%d")
    fig = lambda name, label: (f'<figure><a href="{name}?v={v}" download><img src="{name}?v={v}" alt="{esc(label)}" loading="lazy"></a>'
                               f'<figcaption>{esc(label)} · <a href="{name}?v={v}" download>Preuzmi</a></figcaption></figure>')
    stories = [n for n in made if n.startswith("danas")]
    slides = sorted((n for n in made if n.startswith("vikend")), key=lambda n: int(re.sub(r"\D", "", n)))
    block = lambda key, label: (f'<h3>Tekst objave</h3><textarea id="{key}" readonly>{esc(label)}</textarea>'
                                f'<button class="btn" type="button" data-copy="{key}">Kopiraj tekst</button>')
    return f"""<!DOCTYPE html><html lang="sr-Latn"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>kuda. — za Instagram</title><link rel="stylesheet" href="{FONTS.replace('&', '&amp;')}">
<link rel="stylesheet" href="../assets/event.css"><style>main{{padding:20px 16px 64px}}h1{{font:800 34px/1.05 var(--display);margin:6px 0 10px}}
h2{{font:800 24px/1.1 var(--display);margin:34px 0 12px}}h3{{font-size:15px;margin:18px 0 8px}}.how{{color:var(--muted);font-size:15px}}
.row{{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px}}figure{{flex:none;width:190px;margin:0}}figure img{{display:block;width:100%;border-radius:12px;border:1px solid var(--line)}}
figcaption{{margin-top:6px;font-size:13px;color:var(--muted)}}textarea{{display:block;width:100%;min-height:200px;margin-bottom:10px;padding:12px;border:1px solid var(--line-strong);border-radius:12px;font:14px/1.5 var(--text)}}
</style></head><body><main class="wrap"><p class="eyebrow" style="font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">Za Instagram · {day.day}. {MONTHS[day.month - 1]}</p>
<h1>Slike za objavu</h1><p class="how">Na telefonu drži prst na slici i izaberi <b>Sačuvaj</b>. U storyju dodaj link sticker: kudaugrad.com. Slike se prave svako jutro.</p>
<h2>Story za danas</h2>{f'<div class="row">{"".join(fig(n, f"Story {i}") for i, n in enumerate(stories, 1))}</div>' if stories else '<p class="how">Danas nema događaja za story.</p>'}
{block("cap-danas", today_text) if today_text.strip() else ""}
<h2>Vikend · carousel</h2>{f'<div class="row">{"".join(fig(n, f"{i}/{len(slides)}") for i, n in enumerate(slides, 1))}</div>' + block("cap-vikend", weekend_text) if slides else '<p class="how">Za ovaj vikend još nema događaja.</p>'}
</main><script>document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {{
const t = document.getElementById(b.dataset.copy); try {{ await navigator.clipboard.writeText(t.value); b.textContent = "Kopirano"; }} catch (_) {{ t.select(); }} }}));</script>
</body></html>"""

# ------------------------------------------------------------------ event pages

CSS = """
:root{--ink:#0F0F0F;--muted:#5E5E58;--line:#E2E2DC;--line-strong:#D6D6CF;--soft:#F6F6F2;--soft-2:#F3F3EF;--accent:#FF4D00;
--display:"Bricolage Grotesque","Inter",system-ui,sans-serif;--text:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:#fff;color:var(--ink);font:16px/1.5 var(--text)}
a{color:inherit}:focus-visible{outline:2px solid #2B4BFF;outline-offset:2px}button{font:inherit;color:inherit;cursor:pointer}
.wrap{max-width:1128px;margin:0 auto;padding:0 16px}
.top{border-bottom:1px solid var(--line)}.bar{display:flex;align-items:center;gap:10px;min-height:60px}
.logo{font:800 28px/1 var(--display);letter-spacing:-.04em;text-decoration:none}.logo span{color:var(--accent)}
.city{display:inline-flex;align-items:center;min-height:32px;padding:0 10px;border:1px solid var(--line-strong);border-radius:999px;font-size:13px;font-weight:600}
.back{margin-left:auto;display:inline-flex;align-items:center;gap:6px;min-height:44px;font-size:14px;font-weight:600;text-decoration:none}
.event{display:grid;gap:22px;padding-top:16px;padding-bottom:56px}
.hero{position:relative;margin:0;border-radius:20px;overflow:hidden;background:var(--soft-2);aspect-ratio:4/3}
.hero .bg{filter:blur(22px) brightness(.6);transform:scale(1.15)}.hero .fg{object-fit:contain}
.hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero .tile:has(~ img){opacity:0}
.hero figcaption{position:absolute;right:10px;bottom:10px;padding:3px 9px;border-radius:999px;background:rgba(255,255,255,.92);font-size:11px;font-weight:600}
.tile{position:absolute;inset:0;padding:22px;font:800 clamp(30px,6vw,56px)/.95 var(--display);letter-spacing:-.03em;text-transform:uppercase;overflow:hidden}
.eyebrow{margin:0;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{margin:6px 0 0;font:800 clamp(34px,6vw,56px)/1 var(--display);letter-spacing:-.03em}
.orig{margin:6px 0 0;color:var(--muted)}
.body{display:flex;flex-direction:column;gap:18px;min-width:0}
.facts{margin:0;padding:0;list-style:none;border:1px solid var(--line);border-radius:16px}
.facts li{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-bottom:1px solid var(--line)}.facts li:last-child{border-bottom:0}
.facts svg{flex:none;margin-top:2px;color:var(--muted)}.facts b{display:block}.facts span{display:block;font-size:14px;color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:10px}
.btn{display:inline-flex;align-items:center;gap:8px;min-height:46px;padding:0 18px;border:1px solid var(--line-strong);border-radius:999px;background:#fff;font-size:15px;font-weight:600;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);font-weight:700}
.mini{margin:0 0 8px;font-size:14px;font-weight:700}.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;min-height:44px;padding:0 15px;border:1px solid var(--line-strong);border-radius:999px;font-size:14px;font-weight:600;text-decoration:none}
.desc{margin:0;font-size:17px;line-height:1.65;white-space:pre-line}
.desc.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.more{align-self:flex-start;margin:-10px 0 0;padding:0;border:0;background:none;font-size:15px;font-weight:700;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}
.source{margin:0;padding:12px 14px;border-radius:14px;background:var(--soft);font-size:14px;color:var(--muted)}.source b{color:var(--ink);font-weight:600}
footer{background:var(--ink);color:#fff}footer .wrap{padding-top:32px;padding-bottom:32px}footer p{margin:6px 0 0;max-width:560px;font-size:14px;color:#B5B5AD}footer a{color:#fff;text-underline-offset:3px}footer .links{margin-top:12px}
@media(min-width:720px){.wrap{padding:0 24px}.bar{min-height:72px}.event{padding-top:28px}}
@media(min-width:960px){.event{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:40px;align-items:start}.hero{aspect-ratio:4/3;position:sticky;top:24px}}
"""

ICONS = {
    "cal": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "pin": '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
    "ticket": '<path d="M2 9a3 3 0 0 0 0 6v4h20v-4a3 3 0 0 0 0-6V5H2z"/><path d="M14 5v2M14 11v2M14 17v2"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "film": '<rect x="2" y="2" width="20" height="20" rx="2.18"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
    "out": '<path d="M7 17L17 7M8 7h9v9"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.59 13.51l6.83 3.98M15.41 6.51l-6.82 3.98"/>',
    "back": '<path d="M15 18l-6-6 6-6"/>',
}


def icon(name: str, size: int = 20) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def safe(u):
    return u if isinstance(u, str) and re.match(r"https?://", u) else None


def gcal(e: dict, start: dt.datetime, end: dt.datetime) -> str:
    dates = (f"{start:%Y%m%d}/{start + dt.timedelta(days=1):%Y%m%d}" if e.get("all_day") else f"{utc(start)}/{utc(end)}")
    q = {"action": "TEMPLATE", "text": e["title"], "dates": dates,
         "location": ", ".join(x for x in (place(e), e.get("hall"), "Banjaluka") if x),
         "details": (e.get("description") or "") + (f"\n\n{e['url']}" if safe(e.get("url")) else "")}
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)


def ics(e: dict, start: dt.datetime, end: dt.datetime, page_url: str) -> str:
    x = lambda s: str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//kuda//Banjaluka//SR", "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
             f"UID:{e['id']}@kudaugrad.com", f"DTSTAMP:{utc(dt.datetime.now(dt.timezone.utc))}",
             f"DTSTART;VALUE=DATE:{start:%Y%m%d}" if e.get("all_day") else f"DTSTART:{utc(start)}",
             f"DTEND;VALUE=DATE:{start + dt.timedelta(days=1):%Y%m%d}" if e.get("all_day") else f"DTEND:{utc(end)}",
             f"SUMMARY:{x(e['title'])}", f"LOCATION:{x(', '.join(v for v in (place(e), e.get('hall'), 'Banjaluka') if v))}",
             f"URL:{page_url}", f"DESCRIPTION:{x(e.get('description'))}", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def price_value(price: str | None):
    if not price:
        return None
    if fold(price).startswith("ulaz slobodan"):
        return 0
    m = re.search(r"(\d+(?:[.,]\d+)?)", price)
    return float(m.group(1).replace(",", ".")) if m else None


def schema(e: dict, start: dt.datetime, end: dt.datetime | None, page_url: str, og: str | None) -> dict:
    data = {
        "@context": "https://schema.org", "@type": SCHEMA_TYPE.get(e.get("category"), "Event"),
        "name": e["title"], "url": page_url,
        "startDate": start.date().isoformat() if e.get("all_day") else start.isoformat(),
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {"@type": "Place", "name": ", ".join(x for x in (place(e), e.get("hall")) if x),
                     "address": {"@type": "PostalAddress", "addressLocality": "Banja Luka", "addressCountry": "BA"}},
        "organizer": {"@type": "Organization", "name": e.get("organizer") or e.get("source")},
    }
    if end and not e.get("all_day"):
        data["endDate"] = end.isoformat()
    if og:
        data["image"] = [og]
    if e.get("description"):
        data["description"] = clip(e["description"], 300)
    offer_url = safe(e.get("ticket_url")) or safe(e.get("url"))
    value = price_value(e.get("price"))
    if offer_url or value is not None:
        offer = {"@type": "Offer", "url": offer_url or page_url, "priceCurrency": "BAM"}
        if value is not None:
            offer["price"] = value
        data["offers"] = offer
    return data


GOATCOUNTER = '<script data-goatcounter="https://kudaugrad.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>'  # visitor stats, no cookies


def head(title: str, description: str, url: str, image: str, prefix: str, extra: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="sr-Latn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(url)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="kuda.">
<meta property="og:locale" content="sr_RS">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="{esc(image)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#FFFFFF">
<link rel="icon" href="{prefix}favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="{prefix}apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS.replace('&', '&amp;')}">
<link rel="stylesheet" href="{prefix}assets/event.css">
{GOATCOUNTER}
{extra}</head>"""


def mailto(subject: str, body: str = "") -> str:
    return f"mailto:{CONTACT}?subject={urllib.parse.quote(subject)}" + (f"&body={urllib.parse.quote(body)}" if body else "")


def footer(sources: list) -> str:
    return f"""<footer><div class="wrap"><span class="logo">kuda<span>.</span></span>
<p>Nezavisni vodič kroz kulturu Banjaluke. Podaci se preuzimaju sa zvaničnih stranica ustanova, a svaki događaj vodi na izvor.</p>
<p class="links"><a href="/dodaj/">Dodaj događaj</a> · Kontakt: <a href="mailto:{CONTACT}">{CONTACT}</a></p>
<p>Izvori: {esc(" · ".join(sources))}</p></div></footer>"""


def event_page(e: dict, others: list, sources: list) -> str:
    start, end = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"]) if e.get("end") else None
    end_or_guess = end or start + dt.timedelta(hours=2)
    page_url = f"{SITE_URL}/{e['page']}"
    og = f"{SITE_URL}/{e['og']}" if e.get("og") else f"{SITE_URL}/og.png"
    shows = e.get("showtimes") or []
    span = bool(end) and end - start > dt.timedelta(days=1)  # runs over several days, e.g. an exhibition
    when = ("Projekcije: " + ", ".join(s["time"] for s in shows) if shows else
            ("" if e.get("all_day") or start < dt.datetime.now(start.tzinfo) else
             ("Otvaranje u " if e.get("category") == "Izložbe" else "Početak u ") + f"{start:%H:%M}") if span else
            "Početak na sajtu izvora" if e.get("all_day") else
            f"{start:%H:%M}" + (f"–{end:%H:%M}" if end and end.date() == start.date() else ""))
    where = ", ".join(x for x in (place(e), e.get("hall")) if x)
    title = f"{e['title']} · {short_date(start)}{'' if e.get('all_day') or shows else f', {start:%H:%M}'} · {place(e)}"
    description = clip(e.get("description") or f"{e.get('category', '')} · {where} · {long_date(start)}", 155)
    url, ticket = safe(e.get("url")), safe(e.get("ticket_url"))
    bg, fg = TILE.get(e.get("category"), ("#0F0F0F", "#FFFFFF"))
    picture = (f'<img src="../../{esc(e["large"])}" alt="" width="1400" height="800">' if e.get("large") else
               f'<img class="bg" src="{esc(e["image"])}" alt="" aria-hidden="true" referrerpolicy="no-referrer">'
               f'<img class="fg" src="{esc(e["image"])}" alt="" referrerpolicy="no-referrer">' if safe(e.get("image")) else "")
    hall = f"<span>{esc(e.get('hall'))}</span>" if e.get("hall") else ""
    facts = [f'<li>{icon("cal")}<div><b>{esc(date_range(start, end) if span else long_date(start))}</b>{f"<span>{esc(when)}</span>" if when else ""}</div></li>',
             f'<li>{icon("pin")}<div><b>{esc(place(e))}</b>{hall}</div></li>']
    if e.get("genre") or e.get("duration"):
        duration = f"<span>{esc(e.get('duration'))}</span>" if e.get("duration") else ""
        facts.append(f'<li>{icon("film")}<div><b>{esc(e.get("genre") or "Film")}</b>{duration}</div></li>')
    spans = "".join(f"<span>{esc(s)}</span>" for s in (f"Uzrast {e['age']}" if e.get("age") else None, e.get("note")) if s)
    facts.append(f'<li>{icon("ticket")}<div><b>{esc(e.get("price") or "Cijena na sajtu izvora")}</b>{spans}</div></li>')
    if e.get("organizer"):
        facts.append(f'<li>{icon("user")}<div><b>{esc(e["organizer"])}</b><span>Organizator</span></div></li>')
    actions = []
    if ticket:
        actions.append(f'<a class="btn primary" href="{esc(ticket)}" target="_blank" rel="noopener" '
                       f'data-goatcounter-click="karte/{esc(e["id"])}" data-goatcounter-title="{esc(e["title"])}">Karte {icon("out", 16)}</a>')
    if url:
        actions.append(f'<a class="btn{"" if ticket else " primary"}" href="{esc(url)}" target="_blank" rel="noopener" '
                       f'data-goatcounter-click="izvor/{esc(e["id"])}" data-goatcounter-title="{esc(e["title"])}">Više na sajtu izvora {icon("out", 16)}</a>')
    actions.append(f'<a class="btn" href="kalendar.ics" download>{icon("cal")}Dodaj u kalendar</a>')
    actions.append(f'<a class="btn" href="{esc(gcal(e, start, end_or_guess))}" target="_blank" rel="noopener">Google Kalendar {icon("out", 16)}</a>')
    actions.append(f'<button class="btn" type="button" data-share>{icon("share", 18)}<span>Podijeli</span></button>')
    blocks = []
    if shows:
        blocks.append('<div><h2 class="mini">Karte za projekciju</h2><div class="chips">' + "".join(
            f'<a class="chip" href="{esc(safe(s.get("ticket")) or "#")}" target="_blank" rel="noopener">'
            f'{esc(" · ".join(x for x in (s.get("time"), s.get("hall"), s.get("format")) if x))}</a>' for s in shows) + "</div></div>")
    if others:
        label = "Drugi dani" if shows else "Druga izvođenja"
        blocks.append(f'<div><h2 class="mini">{label}</h2><div class="chips">' + "".join(
            f'<a class="chip" href="../../{esc(o["page"])}">{esc(short_date(dt.datetime.fromisoformat(o["start"])))}'
            f'{"" if o.get("all_day") or o.get("showtimes") else " · " + dt.datetime.fromisoformat(o["start"]).strftime("%H:%M")}</a>'
            for o in others) + "</div></div>")
    source_link = f' · <a href="{esc(url)}" target="_blank" rel="noopener">originalna objava</a>' if url else ""
    tmdb = (" Poster i fotografija filma: TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB."
            if e.get("image_credit") == "TMDB" else "")
    fix = mailto(f"Ispravka: {e['title']} ({short_date(start)})", f"Događaj: {page_url}\n\nŠta treba ispraviti:\n")
    blocks.append(f'<p class="source">Izvor: <b>{esc(e.get("source"))}</b>{source_link}. Podaci se preuzimaju automatski, '
                  f'pa konačne informacije provjeri kod izvora.{tmdb} Nešto nije tačno? <a href="{esc(fix)}">Javi nam</a>.</p>')
    jsonld = json.dumps(schema(e, start, end, page_url, og), ensure_ascii=False).replace("</", "<\\/")
    return head(title, description, page_url, og, "../../", f'<script type="application/ld+json">{jsonld}</script>\n') + f"""
<body>
<header class="top"><div class="wrap bar"><a class="logo" href="../../" aria-label="kuda. — svi događaji">kuda<span>.</span></a>
<span class="city">Banjaluka</span><a class="back" href="../../">{icon("back", 18)}Svi događaji</a></div></header>
<main class="wrap event">
<figure class="hero"><div class="tile" style="background:{bg};color:{fg}" aria-hidden="true">{esc(e['title'])}</div>{picture}
{f'<figcaption>Foto: {esc(e.get("image_credit") or e.get("source"))}</figcaption>' if picture else ''}</figure>
<div class="body">
<div><p class="eyebrow">{esc(e.get('category'))}</p><h1>{esc(e['title'])}</h1></div>
{f'<p class="desc clamp" id="desc">{esc(credits(e["description"]))}</p><button class="more" type="button" data-more aria-expanded="false" aria-controls="desc" hidden>Prikaži više</button>' if e.get("description") else ""}
<ul class="facts">{''.join(facts)}</ul>
<div class="actions">{''.join(actions)}</div>
{''.join(blocks)}
</div>
</main>
{footer(sources)}
<script>
document.querySelector("[data-share]").addEventListener("click", async (ev) => {{
  const label = ev.currentTarget.querySelector("span");
  try {{
    if (navigator.share) {{ await navigator.share({{ title: document.title, url: location.href }}); return; }}
    await navigator.clipboard.writeText(location.href);
    label.textContent = "Link je kopiran";
  }} catch (_) {{ /* cancelled */ }}
}});
const desc = document.getElementById("desc"), more = document.querySelector("[data-more]");
if (desc && more) {{
  more.hidden = desc.scrollHeight <= desc.clientHeight + 2;
  more.addEventListener("click", () => {{
    const open = !desc.classList.toggle("clamp");
    more.textContent = open ? "Prikaži manje" : "Prikaži više";
    more.setAttribute("aria-expanded", String(open));
  }});
}}
document.addEventListener("error", (ev) => {{ if (ev.target.tagName === "IMG") ev.target.remove(); }}, true);
</script>
</body>
</html>
"""


def not_found_page(sources: list) -> str:
    return head("kuda. — stranica ne postoji", TAGLINE, SITE_URL + "/", SITE_URL + "/og.png", "/") + f"""
<body>
<header class="top"><div class="wrap bar"><a class="logo" href="/">kuda<span>.</span></a><span class="city">Banjaluka</span></div></header>
<main class="wrap event" style="display:block;padding-top:48px">
<p class="eyebrow">Greška 404</p><h1>Ovaj događaj je prošao ili stranica ne postoji.</h1>
<p class="orig" style="margin-top:14px">Pogledaj šta se dešava danas i narednih dana.</p>
<p style="margin-top:24px"><a class="btn primary" href="/">Svi događaji</a></p>
</main>
{footer(sources)}
</body>
</html>
"""

def add_page(form: str | None, sources: list) -> str:
    """kudaugrad.com/dodaj, a short address for organizers: it leads to the form, or to the mail until there is one."""
    action = (f'<a class="btn primary" href="{esc(form)}">Otvori formu {icon("out", 16)}</a>' if form else
              f'<a class="btn primary" href="{esc(mailto("Novi događaj"))}">Pošalji na {CONTACT}</a>')
    return head("Dodaj događaj — kuda.", "Pošalji kulturni događaj u Banjaluci, objavljujemo ga na kudaugrad.com.",
                SITE_URL + "/dodaj/", SITE_URL + "/og.png", "/") + f"""
<body>
<header class="top"><div class="wrap bar"><a class="logo" href="/">kuda<span>.</span></a><span class="city">Banjaluka</span><a class="back" href="/">{icon("back", 18)}Svi događaji</a></div></header>
<main class="wrap event" style="display:block;padding-top:48px">
<p class="eyebrow">Za organizatore</p><h1>Dodaj događaj</h1>
<p class="orig" style="margin-top:14px">Pošalji kulturni događaj u Banjaluci: predstavu, koncert, izložbu, projekciju ili promociju. Objavljujemo ga kad ga pregledamo, obično za dan-dva.</p>
<p style="margin-top:24px">{action}</p>
<p class="orig" style="margin-top:18px">Plakat, ispravke i pitanja: <a href="mailto:{CONTACT}">{CONTACT}</a></p>
</main>
{footer(sources)}
</body>
</html>
"""

# ------------------------------------------------------------------ build


def main() -> int:
    data = json.loads((ROOT / "data" / "events.json").read_text(encoding="utf-8"))
    events = data.get("events", [])
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    (OUT / "assets").mkdir()
    (OUT / "assets" / "event.css").write_text(CSS.strip() + "\n", encoding="utf-8")
    apply_image_rules(events)

    made = 0
    for e in events:
        e["page"] = f"e/{slugify(e['title'], 50)}-{slugify(e['id'], 30)}/"
        e.update(pictures(e))
        made += bool(e.get("thumb"))
    print(f"pictures: {made} of {sum(1 for e in events if e.get('image'))} resized" + ("" if Image else " (Pillow not installed)"))

    sources = sorted({e["source"] for e in events if e.get("source")})
    groups: dict = {}
    for e in events:
        groups.setdefault((fold(e["title"]), e.get("venue")), []).append(e)
    for e in events:
        others = [o for o in groups[(fold(e["title"]), e.get("venue"))] if o is not e]
        folder = OUT / e["page"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(event_page(e, others, sources), encoding="utf-8")
        start = dt.datetime.fromisoformat(e["start"])
        end = dt.datetime.fromisoformat(e["end"]) if e.get("end") else start + dt.timedelta(hours=2)
        (folder / "kalendar.ics").write_text(ics(e, start, end, f"{SITE_URL}/{e['page']}"), encoding="utf-8")

    text = json.dumps(data, ensure_ascii=False, indent=1)
    (OUT / "data" / "events.json").write_text(text, encoding="utf-8")
    (OUT / "data" / "events.js").write_text(f"window.KUDA_DATA = {text};\n", encoding="utf-8")
    shutil.copyfile(ROOT / "index.html", OUT / "index.html")
    (OUT / "404.html").write_text(not_found_page(sources), encoding="utf-8")
    (OUT / "dodaj").mkdir()
    (OUT / "dodaj" / "index.html").write_text(add_page(data.get("form"), sources), encoding="utf-8")

    today = dt.date.today().isoformat()
    urls = [f"{SITE_URL}/", f"{SITE_URL}/dodaj/"] + [f"{SITE_URL}/{e['page']}" for e in events]
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{esc(u)}</loc><lastmod>{today}</lastmod></url>\n" for u in urls) + "</urlset>\n",
        encoding="utf-8")
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")

    # keep the picture cache to what the current events use, so it doesn't grow forever
    used_src = {hashlib.sha1(e[k].encode()).hexdigest()[:24] for e in events for k in ("image", "backdrop") if e.get(k)}
    used_out = {Path(e[k]).name for e in events for k in ("thumb", "large", "og") if e.get(k)}
    for f in (CACHE / "src").glob("*") if (CACHE / "src").exists() else []:
        if f.name not in used_src:
            f.unlink()
    for f in (CACHE / "out" / "img").glob("*") if (CACHE / "out" / "img").exists() else []:
        if f.name not in used_out:
            f.unlink()

    brand_images()  # no daily numbers on it: Facebook and Viber keep old copies of share images for weeks
    print(social(events))
    for folder, dirs, files in os.walk(OUT):  # GitHub Pages refuses files that aren't readable by everyone
        os.chmod(folder, 0o755)
        for name in files:
            os.chmod(os.path.join(folder, name), 0o644)
    print(f"built {len(events)} event pages, sitemap with {len(urls)} addresses"
          + ("" if (OUT / "og.png").exists() else "; share image and icons skipped (Chrome not found)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
