#!/usr/bin/env python3
"""
Brahmins TV - Top 25 News Shots Vertical (9:16) Bulletin Builder
Uses edge-tts neural voices (en-IN-NeerjaNeural / en-IN-PrabhatNeural) with
retry & fallback resilience, expands HC/SC to High Court / Supreme Court,
converts 'zh' to 'l' for 'ழ', formats other acronyms cleanly, filters out
non-news evergreen essays, keeps audio/visual segments aligned, and renders
at 720x1280 (720p 9:16) within 2m 45s (165s).
"""

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

import edge_tts
from PIL import Image, ImageDraw, ImageFont

# 720p vertical (9:16): 720x1280
W, H = 720, 1280
FPS = 24
TARGET_MAX_ITEMS = 25          # Non-rigid target: airs fewer if fewer fresh stories exist
TARGET_MAX_SECONDS = 165.0     # 2m 45s cap for Shorts / Reels
DEDUP_LOOKBACK_HOURS = 36
MAX_STORY_AGE_HOURS = float(os.environ.get("MAX_STORY_AGE_HOURS", "5"))
SEGMENT_TAIL_PAD_SEC = 0.45    # Clean buffer so audio and cards stay aligned

TEMPLATE_PATH = os.environ.get("STUDIO_TEMPLATE_PATH", "assets/studio_template_9x16.png")

# Scaled proportionally for 720x1280 template slots
SLOT_PHOTO: Tuple[int, int, int, int] = (24, 279, 696, 740)
SLOT_NEWS: Tuple[int, int, int, int] = (29, 848, 691, 1072)

LOCAL_SOURCE_CANDIDATES = [
    "top50.json",
    "top50.csv",
    "archive.json",
    "archive.csv",
    "data/top50.json",
    "data/top50.csv",
    "data/archive.json",
    "data/archive.csv",
]

# 5-voice rotation (3 Female + 2 Male) using verified en-IN neural voices
FIVE_VOICE_ROSTER: List[Dict[str, Any]] = [
    {"id": "F1", "gender": "F", "voice": "en-IN-NeerjaNeural", "rate": "+0%"},
    {"id": "M1", "gender": "M", "voice": "en-IN-PrabhatNeural", "rate": "-2%"},
    {"id": "F2", "gender": "F", "voice": "en-IN-NeerjaNeural", "rate": "+2%"},
    {"id": "M2", "gender": "M", "voice": "en-IN-PrabhatNeural", "rate": "-5%"},
    {"id": "F3", "gender": "F", "voice": "en-IN-NeerjaNeural", "rate": "-2%"},
]

VIRAL_KEYWORDS = [
    "breaking", "shocking", "viral", "alert", "massive", "huge", "major",
    "row", "clash", "probe", "arrest", "scam", "crisis", "warning", "surge",
    "ban", "verdict", "resigns", "sacked", "live", "urgent", "blast", "flood",
]

EVERGREEN_TITLE_PATTERNS = [
    r"^the\s+evolution\s+of\b",
    r"^a\s+brief\s+history\s+of\b",
    r"^how\s+to\b",
    r"^why\s+.*\s+matters\b",
    r"^understanding\s+the\b",
    r"^explainer:\s*",
    r"\bculture\s+among\s+indian\s+youths?\b",
]

NEWS_ACTION_GUARD_WORDS = {
    "arrest", "arrested", "probe", "ban", "banned", "court", "verdict", "order",
    "bill", "parliament", "minister", "police", "crash", "fire", "killed", "dead",
    "injured", "launch", "launched", "announces", "announced", "strike", "protest",
    "summons", "raid", "seized", "rbi", "sebi", "cabinet", "election", "alert",
}

EXPLICIT_WORD_EXPANSIONS: Dict[str, str] = {
    "HC": "High Court",
    "SC": "Supreme Court",
    "M/s": "Messers",
}

PRONOUNCEABLE_ACRONYMS: Set[str] = {
    "ISRO", "NASA", "NATO", "AIIMS", "COVID", "NEET", "UPSC", "SEBI", "TRAI",
    "UIDAI", "NITI", "AYUSH", "FEMA", "PMLA", "UAPA", "POCSO", "MGNREGA",
    "BRICS", "QUAD", "ASEAN", "OPEC", "FIFA", "UEFA", "BCCI", "IPL", "WPL",
    "ICMR", "DRDO", "HAL", "BHEL", "ONGC", "NTPC", "GAIL", "IRCTC", "NDMA",
    "IMD", "NDRF", "SDRF", "CRPF", "BSF", "CISF", "NSG", "IAF", "INS",
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def clean_display_text(text: Any) -> str:
    s = str(text or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", s).strip()


def _replace_zh_with_l(text: str) -> str:
    def _sub(match: re.Match) -> str:
        tok = match.group(0)
        if tok.isupper() or tok[0].isupper():
            return "L"
        return "l"

    return re.sub(r"zh", _sub, text, flags=re.IGNORECASE)


def expand_for_tts(text: str) -> str:
    s = clean_display_text(text)
    s = re.sub(r"\bBrahmans?\b", "Braahmins", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrahmins\b", "Braahmins", s, flags=re.IGNORECASE)

    for abbr, full_form in EXPLICIT_WORD_EXPANSIONS.items():
        s = re.sub(r"\b" + re.escape(abbr) + r"\b", full_form, s)

    s = _replace_zh_with_l(s)

    def _format_plural_acronym(match: re.Match) -> str:
        base = match.group(1)
        if base in PRONOUNCEABLE_ACRONYMS:
            return base + "s"
        return " ".join(list(base)) + "s"

    s = re.sub(r"\b([A-Z]{2,6})s\b", _format_plural_acronym, s)

    def _format_acronym(match: re.Match) -> str:
        token = match.group(0)
        if token in ("I", "A"):
            return token
        if token in PRONOUNCEABLE_ACRONYMS:
            return token
        return " ".join(list(token))

    s = re.sub(r"\b[A-Z]{2,6}\b", _format_acronym, s)
    return re.sub(r"\s+", " ", s).strip()


def _is_evergreen_filler(title: str) -> bool:
    low = clean_display_text(title).lower()
    words = set(re.findall(r"[a-z]+", low))
    if words & NEWS_ACTION_GUARD_WORDS:
        return False
    for pat in EVERGREEN_TITLE_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def _get_ist_now() -> dt.datetime:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    return dt.datetime.now(ist)


def _story_fingerprint(title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", clean_display_text(title).lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def load_previously_used_fingerprints() -> Tuple[Set[str], List[Dict[str, Any]]]:
    cutoff = _get_ist_now() - dt.timedelta(hours=DEDUP_LOOKBACK_HOURS)
    entries: List[Dict[str, Any]] = []

    local_path = os.environ.get("USED_BULLETINS_FILE", "used_bulletins.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    entries.extend(data)
        except Exception as e:
            print(f"[WARN] Could not read local {local_path}: {e}")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if token and repo:
        try:
            rel_url = f"https://api.github.com/repos/{repo}/releases/tags/latest-bulletin"
            req = urllib.request.Request(
                rel_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "BrahminsTV-BulletinBuilder/3.3",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                rel_data = json.loads(resp.read().decode("utf-8"))
            for asset in rel_data.get("assets", []):
                if asset.get("name") == "used_bulletins.json" and asset.get("browser_download_url"):
                    areq = urllib.request.Request(
                        asset["browser_download_url"],
                        headers={"User-Agent": "BrahminsTV-BulletinBuilder/3.3"},
                    )
                    with urllib.request.urlopen(areq, timeout=8) as aresp:
                        remote_list = json.loads(aresp.read().decode("utf-8"))
                        if isinstance(remote_list, list):
                            entries.extend(remote_list)
                    break
        except Exception as e:
            print(f"[INFO] No prior used_bulletins.json in release yet: {e}")

    active_fps: Set[str] = set()
    kept_entries: List[Dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        fp = str(row.get("fp") or "").strip()
        used_at_str = str(row.get("used_at") or "").strip()
        keep = True
        if used_at_str:
            try:
                used_dt = dt.datetime.fromisoformat(used_at_str.replace("Z", "+00:00"))
                if used_dt.tzinfo is None:
                    used_dt = used_dt.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
                if used_dt.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))) < cutoff:
                    keep = False
            except Exception:
                pass
        if keep and fp:
            active_fps.add(fp)
            kept_entries.append({"fp": fp, "title": row.get("title", ""), "used_at": used_at_str})

    return active_fps, kept_entries


def save_used_fingerprints(selected_items: List[Dict[str, str]], prior_entries: List[Dict[str, Any]]) -> None:
    now_iso = _get_ist_now().isoformat()
    merged: Dict[str, Dict[str, Any]] = {}
    for row in prior_entries:
        if row.get("fp"):
            merged[row["fp"]] = row
    for it in selected_items:
        fp = _story_fingerprint(it["title"])
        merged[fp] = {"fp": fp, "title": it["title"][:120], "used_at": now_iso}

    out_list = list(merged.values())[-400:]
    out_path = os.environ.get("USED_BULLETINS_FILE", "used_bulletins.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Could not write {out_path}: {e}")


def _resolve_time_window() -> Tuple[int, int, str]:
    slot = os.environ.get("BULLETIN_SLOT", "").strip().lower()
    slot_map = {
        "morning": (22, 5, "1st Bulletin (10 PM - 5 AM)"),
        "1st": (22, 5, "1st Bulletin (10 PM - 5 AM)"),
        "12pm": (5, 11, "12 PM Bulletin (5 AM - 11 AM)"),
        "noon": (5, 11, "12 PM Bulletin (5 AM - 11 AM)"),
        "5pm": (11, 16, "5 PM Bulletin (11 AM - 4 PM)"),
        "evening": (11, 16, "5 PM Bulletin (11 AM - 4 PM)"),
        "10pm": (16, 21, "10 PM Bulletin (4 PM - 9 PM)"),
        "night": (16, 21, "10 PM Bulletin (4 PM - 9 PM)"),
    }
    if slot in slot_map:
        return slot_map[slot]

    now_h = _get_ist_now().hour
    if now_h >= 22 or now_h < 5:
        return 22, 5, "1st Bulletin (10 PM - 5 AM)"
    if 5 <= now_h < 11:
        return 5, 11, "12 PM Bulletin (5 AM - 11 AM)"
    if 11 <= now_h < 16:
        return 11, 16, "5 PM Bulletin (11 AM - 4 PM)"
    return 16, 21, "10 PM Bulletin (4 PM - 9 PM)"


def _parse_item_datetime(row: Dict[str, Any]) -> Optional[dt.datetime]:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    for key in (
        "published_at", "publishedAt", "pubDate", "pub_date",
        "created_at", "createdAt", "timestamp", "date", "time",
    ):
        val = row.get(key)
        if not val:
            continue
        if isinstance(val, (int, float)):
            try:
                ts = float(val)
                if ts > 1e12:
                    ts /= 1000.0
                return dt.datetime.fromtimestamp(ts, tz=ist)
            except Exception:
                continue
        s = str(val).strip()
        if not s:
            continue
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ist)
            return parsed.astimezone(ist)
        except Exception:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d-%m-%Y %H:%M",
            "%d/%m/%Y %H:%M",
            "%d %b %Y, %I:%M %p",
            "%d %b %Y, %I:%M%p",
        ):
            try:
                return dt.datetime.strptime(s, fmt).replace(tzinfo=ist)
            except Exception:
                continue
    return None


def _age_hours(item: Dict[str, Any]) -> Optional[float]:
    dt_obj = item.get("_dt")
    if isinstance(dt_obj, dt.datetime):
        return max(0.0, (_get_ist_now() - dt_obj).total_seconds() / 3600.0)
    return None


def _hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
    if start_h == end_h:
        return True
    if start_h < end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def _viral_score(item: Dict[str, Any], start_h: int, end_h: int, order_idx: int) -> float:
    score = 0.0
    dt_obj = item.get("_dt")
    age = _age_hours(item)

    if isinstance(dt_obj, dt.datetime) and age is not None:
        if _hour_in_window(dt_obj.hour, start_h, end_h):
            score += 60.0
        score += max(0.0, 24.0 - min(24.0, age * 3.0))

    text_blob = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for kw in VIRAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", text_blob):
            score += 8.0

    score += max(0.0, 12.0 - (order_idx * 0.2))
    return score


def _extract_rows_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("top50", "top_50", "archive", "items", "records", "news", "data", "articles", "rows"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = clean_display_text(
        row.get("title")
        or row.get("headline")
        or row.get("news_title")
        or row.get("subject")
        or row.get("Title")
        or row.get("Headline")
        or ""
    )
    if not title or len(title) < 8:
        return None

    if _is_evergreen_filler(title):
        print(f"[SKIP] Filtered non-news evergreen item: {title}")
        return None

    summary = clean_display_text(
        row.get("summary")
        or row.get("description")
        or row.get("excerpt")
        or row.get("body")
        or title
    )
    image_url = clean_display_text(
        row.get("image_url")
        or row.get("imageUrl")
        or row.get("image")
        or row.get("thumbnail")
        or row.get("og_image")
        or ""
    )
    if image_url.startswith("//"):
        image_url = "https:" + image_url

    return {
        "title": title,
        "summary": summary[:220],
        "image_url": image_url if image_url.startswith("http") else "",
        "_dt": _parse_item_datetime(row),
    }


def _load_records_from_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        if path.lower().endswith(".json"):
            data = json.load(f)
            return _extract_rows_from_payload(data)
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def _resolve_top50_or_archive_url() -> str:
    explicit = (
        os.environ.get("TOP50_JSON_URL", "").strip()
        or os.environ.get("ARCHIVE_JSON_URL", "").strip()
        or os.environ.get("NEWS_FEED_URL", "").strip()
    )
    base_url = explicit or os.environ.get("APPS_SCRIPT_URL", "").strip()
    if not base_url:
        return ""

    parsed = urllib.parse.urlparse(base_url)
    qs = urllib.parse.parse_qs(parsed.query)
    if not explicit and "view" not in qs:
        qs["view"] = ["archive"]
    token = os.environ.get("BTV_ACCESS_TOKEN", "").strip()
    if token and "token" not in qs:
        qs["token"] = [token]

    new_query = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def fetch_fresh_between_bulletins(
    target_limit: int = TARGET_MAX_ITEMS,
    used_fps: Optional[Set[str]] = None,
) -> List[Dict[str, str]]:
    start_h, end_h, slot_label = _resolve_time_window()
    used_set = used_fps or set()
    raw_rows: List[Dict[str, Any]] = []
    source_used = ""

    for env_key in ("TOP50_FILE", "ARCHIVE_FILE", "NEWS_ITEMS_FILE"):
        fpath = os.environ.get(env_key, "").strip()
        if fpath and os.path.exists(fpath):
            raw_rows = _load_records_from_file(fpath)
            source_used = fpath
            break

    if not raw_rows:
        for candidate in LOCAL_SOURCE_CANDIDATES:
            if os.path.exists(candidate):
                raw_rows = _load_records_from_file(candidate)
                source_used = candidate
                break

    if not raw_rows:
        feed_url = _resolve_top50_or_archive_url()
        if feed_url:
            try:
                req = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "BrahminsTV-BulletinBuilder/3.3"},
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                raw_rows = _extract_rows_from_payload(payload)
                source_used = feed_url
            except Exception as e:
                print(f"[WARN] Top 50 / Archive fetch failed ({feed_url}): {e}")

    fresh_unseen: List[Dict[str, Any]] = []
    seen_in_run: Set[str] = set()
    for r in raw_rows:
        norm = _normalize_row(r)
        if not norm:
            continue
        fp = _story_fingerprint(norm["title"])
        if fp in seen_in_run or fp in used_set:
            continue
        age = _age_hours(norm)
        if age is not None and age > MAX_STORY_AGE_HOURS:
            continue
        seen_in_run.add(fp)
        fresh_unseen.append(norm)

    scored = sorted(
        [(_viral_score(it, start_h, end_h, idx), idx, it) for idx, it in enumerate(fresh_unseen)],
        key=lambda t: (-t[0], t[1]),
    )
    selected = [t[2] for t in scored[:target_limit]]

    print(
        f"[INFO] Source: {source_used or 'NONE'} | Raw: {len(raw_rows)} | "
        f"MaxAge: {MAX_STORY_AGE_HOURS}h | Fresh unseen in window: {len(fresh_unseen)} | "
        f"Window: {slot_label} | Candidates: {len(selected)}"
    )
    return [{"title": it["title"], "summary": it["summary"], "image_url": it["image_url"]} for it in selected]


def generate_clean_fallback_visual(story_num: int, pw: int, ph: int) -> Image.Image:
    themes = [
        ((16, 22, 46), (48, 16, 28), (218, 172, 66)),
        ((20, 16, 44), (16, 44, 62), (225, 182, 74)),
        ((36, 15, 24), (18, 24, 52), (212, 164, 58)),
    ]
    c_top, c_bot, gold = themes[(story_num - 1) % len(themes)]
    card = Image.new("RGB", (pw, ph), c_top)
    draw = ImageDraw.Draw(card)

    for y in range(ph):
        ratio = y / max(1, ph - 1)
        r = int(c_top[0] * (1 - ratio) + c_bot[0] * ratio)
        g = int(c_top[1] * (1 - ratio) + c_bot[1] * ratio)
        b = int(c_top[2] * (1 - ratio) + c_bot[2] * ratio)
        draw.line([(0, y), (pw, y)], fill=(r, g, b), width=1)

    bar_w = 148
    bar_x = (pw - bar_w) // 2
    bar_y = ph // 2 - 2
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 4], fill=gold)
    return card


def load_story_photo(image_url: str, story_num: int) -> Image.Image:
    px1, py1, px2, py2 = SLOT_PHOTO
    pw, ph = px2 - px1, py2 - py1

    if image_url:
        try:
            req = urllib.request.Request(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            if img.width >= 200 and img.height >= 120:
                return img
        except Exception as e:
            print(f"[WARN] Image load failed for story {story_num}: {e}")

    return generate_clean_fallback_visual(story_num, pw, ph)


def fit_cover(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(box_w / src_w, box_h / src_h)
    nw = max(1, int(round(src_w * scale)))
    nh = max(1, int(round(src_h * scale)))
    return img.convert("RGB").resize((nw, nh), Image.Resampling.BICUBIC)


def wrap_lines_clean(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_w: int,
    max_lines: int = 4,
) -> List[str]:
    words = text.strip().split()
    if not words:
        return []
    lines: List[str] = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def build_headline_card(headline: str, story_num: int, total_stories: int) -> Image.Image:
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    box_w, box_h = nx2 - nx1, ny2 - ny1
    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    pad_x = 24
    top_margin = 32
    bottom_margin = 16
    inner_w = box_w - pad_x * 2
    inner_h = box_h - top_margin - bottom_margin

    counter_font = load_font(15)
    counter_txt = f"{story_num:02d} / {total_stories:02d}"
    cbbox = draw.textbbox((0, 0), counter_txt, font=counter_font)
    cw = cbbox[2] - cbbox[0]
    draw.text((box_w - cw - 18, 10), counter_txt, font=counter_font, fill=(245, 200, 75, 255))

    headline_clean = clean_display_text(headline)
    chosen_size = 28
    chosen_font = load_font(chosen_size)
    chosen_lines = wrap_lines_clean(draw, headline_clean, chosen_font, inner_w, max_lines=4)

    for sz in [28, 25, 23, 20]:
        f = load_font(sz)
        lines = wrap_lines_clean(draw, headline_clean, f, inner_w, max_lines=4)
        line_step = sz + 7
        if len(lines) * line_step <= inner_h:
            chosen_size = sz
            chosen_font = f
            chosen_lines = lines
            break

    line_step = chosen_size + 7
    total_text_h = len(chosen_lines) * line_step
    start_y = top_margin + max(0, (inner_h - total_text_h) // 2)

    for idx, line in enumerate(chosen_lines):
        bbox = draw.textbbox((0, 0), line, font=chosen_font)
        lw = bbox[2] - bbox[0]
        lx = (box_w - lw) // 2
        ly = start_y + idx * line_step
        draw.text((lx + 2, ly + 2), line, font=chosen_font, fill=(0, 0, 0, 235))
        draw.text((lx, ly), line, font=chosen_font, fill=(255, 255, 255, 255))

    return card


def render_frame_fast(
    base_template: Image.Image,
    base_photo: Image.Image,
    headline_card: Image.Image,
    progress: float,
    direction: int,
) -> bytes:
    t = max(0.0, min(1.0, progress))
    ease = 0.5 - 0.5 * math.cos(math.pi * t)

    frame = base_template.copy()

    px1, py1, px2, py2 = SLOT_PHOTO
    pw, ph = px2 - px1, py2 - py1
    zoom = 1.0 + 0.10 * ease
    zw = max(pw, int(round(base_photo.width * zoom)))
    zh = max(ph, int(round(base_photo.height * zoom)))
    zoomed = base_photo.resize((zw, zh), Image.Resampling.BILINEAR)

    max_dx = max(0, zw - pw)
    max_dy = max(0, zh - ph)
    pan_t = ease if direction >= 0 else (1.0 - ease)
    crop_x = int(round(max_dx * pan_t))
    crop_y = int(round(max_dy * 0.5 * (1.0 - ease)))
    crop_box = zoomed.crop((crop_x, crop_y, crop_x + pw, crop_y + ph)).convert("RGBA")
    frame.paste(crop_box, (px1, py1))

    nx1, ny1, nx2, ny2 = SLOT_NEWS
    enter_t = min(1.0, t / 0.14)
    slide_ease = 1.0 - (1.0 - enter_t) ** 3
    offset_x = int(round((1.0 - slide_ease) * 80))
    box_w, box_h = nx2 - nx1, ny2 - ny1
    visible_card = headline_card.crop((0, 0, max(1, box_w - offset_x), box_h))
    frame.alpha_composite(visible_card, (nx1 + offset_x, ny1))

    return frame.convert("RGB").tobytes()


def probe_duration_seconds(media_path: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", media_path,
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return float(out)
    except Exception:
        return 0.0


async def _fetch_edge_tts_with_retry(
    text: str,
    primary_voice: str,
    rate: str,
    out_mp3: str,
) -> None:
    fallback_voice = "en-IN-PrabhatNeural" if primary_voice == "en-IN-NeerjaNeural" else "en-IN-NeerjaNeural"
    attempts = [
        (primary_voice, rate),
        (primary_voice, "+0%"),
        (fallback_voice, "+0%"),
    ]
    last_err: Optional[Exception] = None
    for idx_a, (v_name, v_rate) in enumerate(attempts):
        try:
            if os.path.exists(out_mp3):
                os.remove(out_mp3)
            comm = edge_tts.Communicate(text=text, voice=v_name, rate=v_rate)
            await comm.save(out_mp3)
            if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 200:
                return
        except Exception as exc:
            last_err = exc
            if idx_a < len(attempts) - 1:
                await asyncio.sleep(0.5 * (idx_a + 1))
    raise RuntimeError(f"edge-tts failed after retries: {last_err}")


def synthesize_segment_audio(
    spoken_text: str,
    out_wav: str,
    idx: int,
    workdir: str,
) -> float:
    voice_cfg = FIVE_VOICE_ROSTER[idx % len(FIVE_VOICE_ROSTER)]
    raw_mp3 = os.path.join(workdir, f"seg_{idx:02d}_edge.mp3")

    asyncio.run(
        _fetch_edge_tts_with_retry(
            text=spoken_text,
            primary_voice=str(voice_cfg["voice"]),
            rate=str(voice_cfg["rate"]),
            out_mp3=raw_mp3,
        )
    )

    subprocess.run(
        ["ffmpeg", "-y", "-i", raw_mp3, "-ar", "44100", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    dur = probe_duration_seconds(out_wav)
    if dur <= 0.3:
        raise RuntimeError(f"Failed to synthesize audio for segment {idx} ({voice_cfg['id']})")
    return dur


def build_segment_video(
    base_template: Image.Image,
    photo_rgb: Image.Image,
    headline: str,
    spoken_text: str,
    workdir: str,
    idx: int,
    story_num: int,
    total_stories: int,
) -> str:
    audio_path = os.path.join(workdir, f"seg_{idx:02d}.wav")
    seg_mp4 = os.path.join(workdir, f"seg_{idx:02d}.mp4")

    audio_dur = synthesize_segment_audio(spoken_text, audio_path, idx, workdir)
    total_dur = max(2.8, audio_dur + SEGMENT_TAIL_PAD_SEC)
    num_frames = max(24, int(math.ceil(total_dur * FPS)))

    px1, py1, px2, py2 = SLOT_PHOTO
    base_photo = fit_cover(photo_rgb, px2 - px1, py2 - py1)
    headline_card = build_headline_card(headline, story_num, total_stories)
    direction = 1 if (idx % 2 == 0) else -1

    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{W}x{H}",
            "-pix_fmt", "rgb24",
            "-r", str(FPS),
            "-i", "-",
            "-i", audio_path,
            "-t", f"{total_dur:.2f}",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "26",
            "-b:v", "1100k",
            "-maxrate", "1400k",
            "-bufsize", "2200k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "1",
            "-b:a", "96k",
            seg_mp4,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    assert proc.stdin is not None
    try:
        for f_idx in range(num_frames):
            progress = f_idx / max(1, num_frames - 1)
            proc.stdin.write(render_frame_fast(base_template, base_photo, headline_card, progress, direction))
    except BrokenPipeError:
        err_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait()
        raise RuntimeError(f"FFmpeg pipe closed early on segment {idx}: {err_out[-600:]}")
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()

    err_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.stderr:
        proc.stderr.close()
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"FFmpeg failed on segment {idx} (code {ret}): {err_out[-600:]}")

    return seg_mp4


def concat_segments(segment_paths: List[str], out_mp4: str, workdir: str) -> None:
    list_file = os.path.join(workdir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p}'\n")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "26",
            "-b:v", "1100k",
            "-maxrate", "1400k",
            "-bufsize", "2200k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "1",
            "-b:a", "96k",
            "-movflags", "+faststart",
            out_mp4,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def main() -> None:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"Missing studio template: {TEMPLATE_PATH}")

    base_template = Image.open(TEMPLATE_PATH).convert("RGBA").resize((W, H), Image.Resampling.LANCZOS)
    used_fps, prior_entries = load_previously_used_fingerprints()
    items = fetch_fresh_between_bulletins(target_limit=TARGET_MAX_ITEMS, used_fps=used_fps)

    if not items:
        raise RuntimeError(
            f"No fresh un-aired news items found within {MAX_STORY_AGE_HOURS}h window."
        )

    total_stories = len(items)
    output_path = os.environ.get("OUTPUT_MP4_PATH", "top25-latest.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        segments: List[str] = []
        aired_in_this_run: List[Dict[str, str]] = []

        first_item = items[0]
        first_photo = load_story_photo(first_item["image_url"], 1)
        hook_spoken = expand_for_tts(
            f"Top news stories right now on Brahmins TV. {first_item['title']}."
        )
        segments.append(
            build_segment_video(
                base_template=base_template,
                photo_rgb=first_photo,
                headline=first_item["title"],
                spoken_text=hook_spoken,
                workdir=tmpdir,
                idx=0,
                story_num=1,
                total_stories=total_stories,
            )
        )
        aired_in_this_run.append(first_item)

        running_sec = probe_duration_seconds(segments[0])
        for i, item in enumerate(items[1:], start=1):
            if running_sec >= TARGET_MAX_SECONDS - 4.0:
                break
            story_num = i + 1
            photo = load_story_photo(item["image_url"], story_num)
            spoken = expand_for_tts(f"{item['title']}.")
            seg = build_segment_video(
                base_template=base_template,
                photo_rgb=photo,
                headline=item["title"],
                spoken_text=spoken,
                workdir=tmpdir,
                idx=i,
                story_num=story_num,
                total_stories=total_stories,
            )
            running_sec += probe_duration_seconds(seg)
            segments.append(seg)
            aired_in_this_run.append(item)

        concat_segments(segments, output_path, tmpdir)
        save_used_fingerprints(aired_in_this_run, prior_entries)
        final_dur = probe_duration_seconds(output_path)
        size_mb = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0.0
        print(f"[SUCCESS] Built {output_path} ({len(segments)}/{total_stories} stories, {final_dur:.1f}s, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
