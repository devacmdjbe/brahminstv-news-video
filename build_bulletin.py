#!/usr/bin/env python3
"""
Brahmins TV - Top 25 News Shots Vertical (9:16) Bulletin Builder
Selects stories from Brahmins TV's own Top 50 / Archive (local files or Apps Script JSON)
instead of external RSS feeds, filtered by bulletin time window & viral/breaking priority,
with brisk multi-voice rotation (3 female + 2 male) and expanded spoken text.
"""

import csv
import datetime as dt
import io
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Canvas & Studio Template Configuration (1080x1920, 9:16)
# ---------------------------------------------------------------------------
W, H = 1080, 1920
FPS = 24
MAX_ITEMS = 25
TARGET_MAX_SECONDS = 165  # 2m 45s cap for Shorts / Reels

TEMPLATE_PATH = os.environ.get("STUDIO_TEMPLATE_PATH", "assets/studio_template_9x16.png")

# Two editable slots on 1080x1920 canvas:
#   SLOT_PHOTO: large dark rectangle under "BREAKING NEWS • TOP 25"
#   SLOT_NEWS:  dark rectangle below "BREAKING NEWS" header
SLOT_PHOTO: Tuple[int, int, int, int] = (36, 418, 1044, 1110)
SLOT_NEWS: Tuple[int, int, int, int] = (44, 1272, 1036, 1608)

# Candidate local files for Top 50 / Archive (checked in priority order)
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

# 3 Female + 2 Male brisk voice profiles (tempo & pitch shifts applied cleanly in FFmpeg)
VOICE_ROSTER: List[Dict[str, Any]] = [
    {"name": "female_1", "gender": "F", "tempo": 1.14, "pitch_semitones": 1.2, "espeak": "en-in+f3", "speed": 176},
    {"name": "male_1",   "gender": "M", "tempo": 1.15, "pitch_semitones": -1.5, "espeak": "en-in+m2", "speed": 178},
    {"name": "female_2", "gender": "F", "tempo": 1.16, "pitch_semitones": 2.0, "espeak": "en-in+f4", "speed": 180},
    {"name": "male_2",   "gender": "M", "tempo": 1.14, "pitch_semitones": -2.2, "espeak": "en-in+m3", "speed": 176},
    {"name": "female_3", "gender": "F", "tempo": 1.15, "pitch_semitones": 0.6, "espeak": "en-in+f2", "speed": 178},
]

VIRAL_KEYWORDS = [
    "breaking", "shocking", "viral", "alert", "massive", "huge", "major",
    "row", "clash", "probe", "arrest", "scam", "crisis", "warning", "surge",
    "ban", "verdict", "resigns", "sacked", "live", "urgent", "blast", "flood",
]

ABBREVIATION_MAP: Dict[str, str] = {
    "TV": "Television",
    "HC": "High Court",
    "SC": "Supreme Court",
    "CBI": "Central Bureau of Investigation",
    "ED": "Enforcement Directorate",
    "SBI": "State Bank of India",
    "RBI": "Reserve Bank of India",
    "AP": "Andhra Pradesh",
    "TN": "Tamil Nadu",
    "TS": "Telangana",
    "UP": "Uttar Pradesh",
    "MP": "Madhya Pradesh",
    "BJP": "Bharatiya Janata Party",
    "INC": "Congress",
    "DMK": "Dravida Munnetra Kazhagam",
    "AIADMK": "All India Anna Dravida Munnetra Kazhagam",
    "PM": "Prime Minister",
    "CM": "Chief Minister",
    "MLA": "Member of Legislative Assembly",
    "MPs": "Members of Parliament",
    "FIR": "First Information Report",
    "CCTV": "closed circuit camera",
    "AI": "Artificial Intelligence",
    "IST": "Indian Standard Time",
    "M/s": "Messers",
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


def expand_for_brisk_tts(text: str) -> str:
    """
    Expands abbreviations & tricky terms so brisk TTS never stumbles over short forms
    or mispronounces Brahmins Television.
    """
    s = clean_display_text(text)
    s = re.sub(r"\bBrahmans?\b", "Brahmins", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrahmins\s+TV\b", "Brahmins Television", s, flags=re.IGNORECASE)

    for abbr, expansion in ABBREVIATION_MAP.items():
        pattern = r"\b" + re.escape(abbr) + r"\b"
        s = re.sub(pattern, expansion, s)

    # Spell out remaining all-caps acronyms of 2-5 letters so TTS doesn't mangle them
    def _spell_caps(match: re.Match) -> str:
        token = match.group(0)
        if token in ("I", "A"):
            return token
        return " ".join(list(token))

    s = re.sub(r"\b[A-Z]{2,5}\b", _spell_caps, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _get_ist_now() -> dt.datetime:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    return dt.datetime.now(ist)


def _resolve_time_window() -> Tuple[int, int, str]:
    """
    Returns (start_hour, end_hour, slot_label) in 24h IST:
      - 22:00 to 05:00 -> 1st / morning bulletin
      - 05:00 to 11:00 -> 12 PM bulletin
      - 11:00 to 16:00 -> 5 PM bulletin
      - 16:00 to 21:00 -> 10 PM bulletin
    Supports explicit override via BULLETIN_SLOT or TIME_WINDOW_START / TIME_WINDOW_END.
    """
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

    s_env = os.environ.get("TIME_WINDOW_START", "").strip()
    e_env = os.environ.get("TIME_WINDOW_END", "").strip()
    if s_env.isdigit() and e_env.isdigit():
        return int(s_env) % 24, int(e_env) % 24, f"Custom ({s_env}:00 - {e_env}:00)"

    now_h = _get_ist_now().hour
    if now_h >= 22 or now_h < 5:
        return 22, 5, "1st Bulletin (10 PM - 5 AM)"
    if 5 <= now_h < 11:
        return 5, 11, "12 PM Bulletin (5 AM - 11 AM)"
    if 11 <= now_h < 16:
        return 11, 16, "5 PM Bulletin (11 AM - 4 PM)"
    return 16, 21, "10 PM Bulletin (4 PM - 9 PM)"


def _parse_item_datetime(row: Dict[str, Any]) -> Optional[dt.datetime]:
    for key in ("published_at", "publishedAt", "pubDate", "pub_date", "created_at", "createdAt", "timestamp", "date", "time"):
        val = row.get(key)
        if not val:
            continue
        if isinstance(val, (int, float)):
            try:
                ts = float(val)
                if ts > 1e12:
                    ts /= 1000.0
                return dt.datetime.fromtimestamp(ts, tz=dt.timezone(dt.timedelta(hours=5, minutes=30)))
            except Exception:
                continue
        s = str(val).strip()
        if not s:
            continue
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
            return parsed.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
            try:
                parsed = dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
                return parsed
            except Exception:
                continue
    return None


def _hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
    if start_h == end_h:
        return True
    if start_h < end_h:
        return start_h <= hour < end_h
    # Wraps across midnight (e.g. 22 to 5)
    return hour >= start_h or hour < end_h


def _viral_score(item: Dict[str, Any], start_h: int, end_h: int, order_idx: int) -> float:
    score = 0.0
    dt_obj = item.get("_dt")
    if isinstance(dt_obj, dt.datetime):
        if _hour_in_window(dt_obj.hour, start_h, end_h):
            score += 100.0
        # Newer items slightly higher within window
        age_hours = max(0.0, (_get_ist_now() - dt_obj).total_seconds() / 3600.0)
        score += max(0.0, 24.0 - min(24.0, age_hours))

    text_blob = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for kw in VIRAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", text_blob):
            score += 8.0

    # Preserve earlier top50/archive ordering tie-breaker
    score += max(0.0, 10.0 - (order_idx * 0.15))
    return score


def _extract_rows_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("top50", "top_50", "archive", "items", "records", "news", "data", "articles"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return []


def _normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = clean_display_text(
        row.get("title")
        or row.get("headline")
        or row.get("news_title")
        or row.get("subject")
        or ""
    )
    if not title or len(title) < 8:
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

    status = str(row.get("status") or row.get("bulletin_status") or "pending").strip().lower()
    if status in ("published_video", "aired", "discarded", "rejected", "duplicate"):
        return None

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


def _resolve_archive_url() -> str:
    explicit = os.environ.get("ARCHIVE_JSON_URL", "").strip() or os.environ.get("TOP50_JSON_URL", "").strip()
    if explicit:
        return explicit

    base_url = os.environ.get("APPS_SCRIPT_URL", "").strip()
    if base_url:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}view=archive"
    return ""


def fetch_from_top50_or_archive(limit: int = MAX_ITEMS) -> List[Dict[str, str]]:
    """
    Loads stories from Brahmins TV's own Top 50 / Archive (local files or Apps Script JSON),
    selects stories matching the bulletin time window and viral/breaking priority,
    and falls back to remaining pending Top 50 / Archive items if needed.
    """
    start_h, end_h, slot_label = _resolve_time_window()
    raw_rows: List[Dict[str, Any]] = []
    source_used = ""

    # 1. Explicit env file paths first (TOP50_FILE, ARCHIVE_FILE, NEWS_ITEMS_FILE)
    for env_key in ("TOP50_FILE", "ARCHIVE_FILE", "NEWS_ITEMS_FILE"):
        fpath = os.environ.get(env_key, "").strip()
        if fpath and os.path.exists(fpath):
            raw_rows = _load_records_from_file(fpath)
            source_used = fpath
            break

    # 2. Local repo Top 50 / Archive files
    if not raw_rows:
        for candidate in LOCAL_SOURCE_CANDIDATES:
            if os.path.exists(candidate):
                raw_rows = _load_records_from_file(candidate)
                source_used = candidate
                break

    # 3. Apps Script Archive / Top 50 JSON endpoint
    if not raw_rows:
        archive_url = _resolve_archive_url()
        if archive_url:
            try:
                req = urllib.request.Request(
                    archive_url,
                    headers={"User-Agent": "BrahminsTV-BulletinBuilder/2.0"},
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                raw_rows = _extract_rows_from_payload(payload)
                source_used = archive_url
            except Exception as e:
                print(f"[WARN] Failed to load Top 50 / Archive URL ({archive_url}): {e}")

    normalized: List[Dict[str, Any]] = []
    seen_titles = set()
    for r in raw_rows:
        norm = _normalize_row(r)
        if not norm:
            continue
        key = norm["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        normalized.append(norm)

    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for idx, item in enumerate(normalized):
        s = _viral_score(item, start_h, end_h, idx)
        scored.append((s, idx, item))

    scored.sort(key=lambda tup: (-tup[0], tup[1]))
    selected = [tup[2] for tup in scored[:limit]]

    while len(selected) < min(limit, 5):
        n = len(selected) + 1
        selected.append({
            "title": f"Breaking Update {n} from Brahmins Television",
            "summary": f"Latest report {n} from the Brahmins Television news archive.",
            "image_url": "",
            "_dt": None,
        })

    print(
        f"[INFO] Source: {source_used or 'built-in archive fallback'} | "
        f"Window: {slot_label} | Selected {len(selected)} stories"
    )
    return [{"title": it["title"], "summary": it["summary"], "image_url": it["image_url"]} for it in selected]


def generate_clean_fallback_visual(story_num: int, headline: str, pw: int, ph: int) -> Image.Image:
    """
    Clean borderless dark-gold studio gradient & vignette card when an archive item
    has no image URL. Zero grid lines, zero corner badges that can clip on pan.
    """
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

    # Subtle gold accent bar centered cleanly with generous margin
    bar_w = 180
    bar_x = (pw - bar_w) // 2
    bar_y = int(ph * 0.24)
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 5], fill=gold)

    # Clean centered headline summary inside safe inner box
    headline_font = load_font(42)
    lines = wrap_lines_clean(draw, clean_display_text(headline), headline_font, pw - 140, max_lines=4)
    line_h = 54
    start_y = max(bar_y + 28, (ph - len(lines) * line_h) // 2)
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        lw = bbox[2] - bbox[0]
        lx = (pw - lw) // 2
        ly = start_y + idx * line_h
        draw.text((lx + 2, ly + 2), line, font=headline_font, fill=(0, 0, 0))
        draw.text((lx, ly), line, font=headline_font, fill=(248, 250, 252))

    return card


def load_story_photo(image_url: str, story_num: int, headline: str) -> Image.Image:
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

    return generate_clean_fallback_visual(story_num, headline, pw, ph)


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
    """Pre-renders headline text with comfortable top/bottom padding inside SLOT_NEWS."""
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    box_w, box_h = nx2 - nx1, ny2 - ny1
    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    pad_x = 36
    top_margin = 48
    bottom_margin = 24
    inner_w = box_w - pad_x * 2
    inner_h = box_h - top_margin - bottom_margin

    counter_font = load_font(22)
    counter_txt = f"{story_num:02d} / {total_stories:02d}"
    cbbox = draw.textbbox((0, 0), counter_txt, font=counter_font)
    cw = cbbox[2] - cbbox[0]
    draw.text((box_w - cw - 28, 16), counter_txt, font=counter_font, fill=(245, 200, 75, 255))

    headline_clean = clean_display_text(headline)
    chosen_size = 42
    chosen_font = load_font(chosen_size)
    chosen_lines = wrap_lines_clean(draw, headline_clean, chosen_font, inner_w, max_lines=4)

    for sz in [42, 38, 34, 30]:
        f = load_font(sz)
        lines = wrap_lines_clean(draw, headline_clean, f, inner_w, max_lines=4)
        line_step = sz + 10
        if len(lines) * line_step <= inner_h:
            chosen_size = sz
            chosen_font = f
            chosen_lines = lines
            break

    line_step = chosen_size + 10
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

    # 1. Upper Photo Slot: smooth Ken Burns zoom (1.00x -> 1.11x) & gentle pan
    px1, py1, px2, py2 = SLOT_PHOTO
    pw, ph = px2 - px1, py2 - py1
    zoom = 1.0 + 0.11 * ease
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

    # 2. Lower Breaking News Slot: brisk ease-out slide-in entrance
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    enter_t = min(1.0, t / 0.14)
    slide_ease = 1.0 - (1.0 - enter_t) ** 3
    offset_x = int(round((1.0 - slide_ease) * 120))
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


def _apply_brisk_voice_shaping(in_wav: str, out_wav: str, voice_cfg: Dict[str, Any]) -> None:
    """Applies brisk tempo & pitch shift so each of the 3 female + 2 male voices sounds distinct and energetic."""
    tempo = float(voice_cfg.get("tempo", 1.15))
    semitones = float(voice_cfg.get("pitch_semitones", 0.0))
    pitch_factor = 2.0 ** (semitones / 12.0)

    # Shift pitch via sample rate adjustment + asetrate compensation, then apply brisk atempo
    shifted_rate = int(round(44100 * pitch_factor))
    comp_tempo = tempo / pitch_factor
    filters = [f"asetrate={shifted_rate}", "aresample=44100"]

    # Chain atempo filters in [0.5, 2.0] range safely
    rem = comp_tempo
    while rem > 2.0:
        filters.append("atempo=2.0")
        rem /= 2.0
    while rem < 0.5:
        filters.append("atempo=0.5")
        rem /= 0.5
    filters.append(f"atempo={rem:.4f}")

    subprocess.run(
        ["ffmpeg", "-y", "-i", in_wav, "-filter:a", ",".join(filters), "-ar", "44100", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def synthesize_brisk_voiceover(spoken_text: str, out_wav: str, idx: int) -> float:
    voice_cfg = VOICE_ROSTER[idx % len(VOICE_ROSTER)]
    base_wav = out_wav + ".base.wav"
    tmp_mp3 = out_wav + ".mp3"
    got_base = False

    try:
        from gtts import gTTS
        tts = gTTS(text=spoken_text, lang="en", tld="co.in", slow=False)
        tts.save(tmp_mp3)
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "44100", "-ac", "1", base_wav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        if probe_duration_seconds(base_wav) > 0.5:
            got_base = True
    except Exception as e:
        print(f"[WARN] gTTS fallback on seg {idx} ({voice_cfg['name']}): {e}")

    if not got_base:
        raw_espeak = out_wav + ".raw.wav"
        subprocess.run(
            [
                "espeak-ng",
                "-v", str(voice_cfg["espeak"]),
                "-s", str(voice_cfg["speed"]),
                "-w", raw_espeak,
                spoken_text,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_espeak, "-ar", "44100", "-ac", "1", base_wav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    _apply_brisk_voice_shaping(base_wav, out_wav, voice_cfg)
    dur = probe_duration_seconds(out_wav)
    if dur <= 0.3:
        raise RuntimeError(f"Failed to produce audio for segment {idx}")
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

    audio_dur = synthesize_brisk_voiceover(spoken_text, audio_path, idx)
    total_dur = max(2.4, audio_dur + 0.25)
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
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "1",
            "-b:a", "128k",
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
            "-c", "copy",
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
    items = fetch_from_top50_or_archive(limit=MAX_ITEMS)
    total_stories = len(items)

    output_path = os.environ.get("OUTPUT_MP4_PATH", "top25-latest.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        segments: List[str] = []

        # 1st news image shown as opening hook segment
        first_item = items[0]
        first_photo = load_story_photo(first_item["image_url"], 1, first_item["title"])
        hook_spoken = expand_for_brisk_tts(
            f"Top 25 news stories right now on Brahmins Television! {first_item['title']}."
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

        running_sec = probe_duration_seconds(segments[0])
        for i, item in enumerate(items[1:], start=1):
            if running_sec >= TARGET_MAX_SECONDS - 3.5:
                break
            story_num = i + 1
            photo = load_story_photo(item["image_url"], story_num, item["title"])
            spoken = expand_for_brisk_tts(f"{item['title']}.")
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

        concat_segments(segments, output_path, tmpdir)
        final_dur = probe_duration_seconds(output_path)
        print(f"[SUCCESS] Built {output_path} ({len(segments)} segments, {final_dur:.1f}s)")


if __name__ == "__main__":
    main()
