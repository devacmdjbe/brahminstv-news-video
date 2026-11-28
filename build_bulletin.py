#!/usr/bin/env python3
"""
Brahmins TV - Top 25 News Shots Vertical (9:16) Bulletin Builder
Targets up to 25 fresh stories between bulletins (non-rigid: airs fewer if
fewer fresh stories exist), avoids cross-bulletin duplication, rotates across
5 voices (3 Female + 2 Male) at natural pace, renders at 720x1280 (720p 9:16)
up to 2m 45s (165s), and encodes at a lean bitrate for consistent streaming.
"""

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

from PIL import Image, ImageDraw, ImageFont

# 720p vertical (9:16): 720x1280
W, H = 720, 1280
FPS = 24
TARGET_MAX_ITEMS = 25          # Soft target; fewer than 25 is allowed when fewer are fresh
TARGET_MAX_SECONDS = 165       # 2m 45s cap for Shorts / Reels
DEDUP_LOOKBACK_HOURS = 36
MAX_STORY_AGE_HOURS = float(os.environ.get("MAX_STORY_AGE_HOURS", "5"))

TEMPLATE_PATH = os.environ.get("STUDIO_TEMPLATE_PATH", "assets/studio_template_9x16.png")

# Scaled proportionally from 1080x1920 template slots
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

# 5 distinct voices: 3 Female (F1, F2, F3) + 2 Male (M1, M2) at normal, unhurried pace
FIVE_VOICE_ROSTER: List[Dict[str, Any]] = [
    {"id": "F1", "gender": "F", "espeak": "en-in+f3", "speed": 134, "pitch": 52, "tempo": 0.88, "semitones": 0.8},
    {"id": "M1", "gender": "M", "espeak": "en-in+m1", "speed": 132, "pitch": 40, "tempo": 0.86, "semitones": -1.0},
    {"id": "F2", "gender": "F", "espeak": "en-in+f4", "speed": 136, "pitch": 56, "tempo": 0.90, "semitones": 1.2},
    {"id": "M2", "gender": "M", "espeak": "en-in+m4", "speed": 130, "pitch": 36, "tempo": 0.85, "semitones": -1.4},
    {"id": "F3", "gender": "F", "espeak": "en-in+f2", "speed": 134, "pitch": 48, "tempo": 0.88, "semitones": 0.4},
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


def expand_for_tts(text: str) -> str:
    s = clean_display_text(text)
    s = re.sub(r"\bBrahmans?\b", "Brahmins", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrahmins\s+TV\b", "Brahmins Television", s, flags=re.IGNORECASE)

    for abbr, expansion in ABBREVIATION_MAP.items():
        pattern = r"\b" + re.escape(abbr) + r"\b"
        s = re.sub(pattern, expansion, s)

    def _spell_caps(match: re.Match) -> str:
        token = match.group(0)
        if token in ("I", "A"):
            return token
        return " ".join(list(token))

    s = re.sub(r"\b[A-Z]{2,5}\b", _spell_caps, s)
    return re.sub(r"\s+", " ", s).strip()


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
                    "Accept": "application/vnd.github+json
