#!/usr/bin/env python3
"""
Brahmins TV - Top 25 News Shots Vertical (9:16) Bulletin Builder
Uses fixed studio template (assets/studio_template_9x16.png) and updates ONLY:
  1. Upper news image slot (smooth ease-in-out zoom/pan, no extra logo inside image)
  2. Lower flash news slot (clean wrapped headline text, zero mid-word wrap splits)
"""

import csv
import io
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

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

DEFAULT_FEEDS = [
    "https://www.news18.com/rss/india.xml",
    "https://feeds.feedburner.com/ndtvnews-top-stories",
    "https://www.timesnownews.com/feeds/gns-en-latest.xml",
    "https://www.republicworld.com/rss/india.xml",
    "https://www.indiatoday.in/rss/1206578",
    "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
]

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


def clean_display_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_for_tts(text: str) -> str:
    """
    Ensures clear Indian English pronunciation:
      - Brahmins / Brahmans -> Brah-mins
      - Brah-mins TV -> Brah-mins T V
      - TV -> T V
      - JEE Main -> J E E Main
      - N bhp -> N brake horsepower
    """
    s = clean_display_text(text)
    s = re.sub(r"\bBrahmans?\b", "Brah-mins", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrahmins\b", "Brah-mins", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrah-mins\s+TV\b", "Brah-mins T V", s, flags=re.IGNORECASE)
    s = re.sub(r"\bTV\b", "T V", s)
    s = re.sub(r"\bJEE\s+Main\b", "J E E Main", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s*bhp\b", r"\1 brake horsepower", s, flags=re.IGNORECASE)
    s = re.sub(r"\bIST\b", "I S T", s)
    return re.sub(r"\s+", " ", s).strip()


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].lower() if tag else ""


def parse_rss_entries(xml_bytes: bytes) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    root = ET.fromstring(xml_bytes)

    raw_items = []
    for elem in root.iter():
        if _local_name(elem.tag) in ("item", "entry"):
            raw_items.append(elem)

    for node in raw_items:
        title = ""
        summary = ""
        image_url = ""

        for child in node:
            cname = _local_name(child.tag)
            if cname == "title" and not title:
                title = "".join(child.itertext())
            elif cname in ("description", "summary", "encoded") and not summary:
                summary = "".join(child.itertext())
            elif cname in ("content", "thumbnail", "enclosure"):
                url_attr = child.attrib.get("url") or child.attrib.get("href") or ""
                mime = (child.attrib.get("type") or "").lower()
                if url_attr and (
                    cname == "thumbnail"
                    or "image" in mime
                    or re.search(r"\.(jpg|jpeg|png|webp)", url_attr.lower())
                ):
                    if not image_url:
                        image_url = url_attr
            elif cname == "link":
                href = child.attrib.get("href") or ""
                rel = (child.attrib.get("rel") or "").lower()
                mime = (child.attrib.get("type") or "").lower()
                if href and not image_url and (rel == "enclosure" or "image" in mime):
                    image_url = href

        if title:
            entries.append({
                "title": title,
                "summary": summary or title,
                "image_url": image_url,
            })

    return entries


def fetch_news_items(limit: int = MAX_ITEMS) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen_titles = set()

    items_file = os.environ.get("NEWS_ITEMS_FILE", "").strip()
    if items_file and os.path.exists(items_file):
        with open(items_file, "r", encoding="utf-8") as f:
            if items_file.lower().endswith(".json"):
                raw = json.load(f)
                for row in raw:
                    title = clean_display_text(row.get("title", ""))
                    if title and title.lower() not in seen_titles:
                        seen_titles.add(title.lower())
                        items.append({
                            "title": title,
                            "summary": clean_display_text(row.get("summary", title)),
                            "image_url": (row.get("image_url") or "").strip(),
                        })
            else:
                reader = csv.DictReader(f)
                for row in reader:
                    title = clean_display_text(row.get("title") or row.get("headline") or "")
                    if title and title.lower() not in seen_titles:
                        seen_titles.add(title.lower())
                        items.append({
                            "title": title,
                            "summary": clean_display_text(row.get("summary") or title),
                            "image_url": (row.get("image_url") or "").strip(),
                        })

    feeds_env = os.environ.get("RSS_FEEDS_JSON", "").strip()
    feeds = json.loads(feeds_env) if feeds_env else DEFAULT_FEEDS

    for feed_url in feeds:
        if len(items) >= limit:
            break
        try:
            req = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0 (BrahminsTV-BulletinBuilder/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read()
            for entry in parse_rss_entries(xml_data):
                if len(items) >= limit:
                    break
                title = clean_display_text(entry.get("title", ""))
                if not title or len(title) < 12:
                    continue
                key = title.lower()
                if key in seen_titles:
                    continue
                seen_titles.add(key)

                summary = clean_display_text(entry.get("summary", title))
                items.append({
                    "title": title,
                    "summary": summary[:240],
                    "image_url": (entry.get("image_url") or "").strip(),
                })
        except Exception:
            continue

    while len(items) < min(limit, 5):
        idx = len(items) + 1
        items.append({
            "title": f"Top Story {idx} on Brah-mins Television",
            "summary": f"Latest update number {idx} from Brah-mins Television news desk.",
            "image_url": "",
        })

    return items[:limit]


def load_story_photo(image_url: str, fallback_template: Image.Image) -> Image.Image:
    px1, py1, px2, py2 = SLOT_PHOTO

    if image_url:
        try:
            req = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0 (BrahminsTV-BulletinBuilder/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            if img.width >= 200 and img.height >= 120:
                return img
        except Exception:
            pass

    return fallback_template.crop((px1, py1, px2, py2)).convert("RGB")


def fit_cover(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(box_w / src_w, box_h / src_h)
    nw = max(1, int(round(src_w * scale)))
    nh = max(1, int(round(src_h * scale)))
    return img.convert("RGB").resize((nw, nh), Image.Resampling.LANCZOS)


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


def render_frame(
    base_template: Image.Image,
    photo_rgb: Image.Image,
    headline: str,
    progress: float,
) -> Image.Image:
    t = max(0.0, min(1.0, progress))
    ease = 0.5 - 0.5 * math.cos(math.pi * t)

    frame = base_template.copy()

    # 1. Upper Photo Slot: smooth zoom (1.00x -> 1.07x) & gentle pan, no extra logo
    px1, py1, px2, py2 = SLOT_PHOTO
    pw, ph = px2 - px1, py2 - py1
    base_photo = fit_cover(photo_rgb, pw, ph)
    zoom = 1.0 + 0.07 * ease
    zw = max(pw, int(round(base_photo.width * zoom)))
    zh = max(ph, int(round(base_photo.height * zoom)))
    zoomed = base_photo.resize((zw, zh), Image.Resampling.LANCZOS)

    max_dx = max(0, zw - pw)
    max_dy = max(0, zh - ph)
    crop_x = int(round(max_dx * 0.5 * ease))
    crop_y = int(round(max_dy * 0.5 * (1.0 - ease)))
    crop_box = zoomed.crop((crop_x, crop_y, crop_x + pw, crop_y + ph)).convert("RGBA")
    frame.paste(crop_box, (px1, py1))

    # 2. Lower Breaking News Slot: clean headline inside lower box only
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    pad_x, pad_y = 32, 24
    inner_w = (nx2 - nx1) - pad_x * 2
    inner_h = (ny2 - ny1) - pad_y * 2

    draw = ImageDraw.Draw(frame)
    headline_clean = clean_display_text(headline)

    chosen_size = 44
    chosen_font = load_font(chosen_size)
    chosen_lines = wrap_lines_clean(draw, headline_clean, chosen_font, inner_w, max_lines=4)

    for sz in [44, 38, 34, 30]:
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
    start_y = ny1 + pad_y + max(0, (inner_h - total_text_h) // 2)

    for idx, line in enumerate(chosen_lines):
        bbox = draw.textbbox((0, 0), line, font=chosen_font)
        lw = bbox[2] - bbox[0]
        lx = nx1 + ((nx2 - nx1) - lw) // 2
        ly = start_y + idx * line_step
        draw.text((lx + 2, ly + 2), line, font=chosen_font, fill=(0, 0, 0, 230))
        draw.text((lx, ly), line, font=chosen_font, fill=(255, 255, 255, 255))

    return frame.convert("RGB")


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


def synthesize_voiceover(spoken_text: str, out_mp3: str) -> float:
    try:
        from gtts import gTTS
        tts = gTTS(text=spoken_text, lang="en", tld="co.in", slow=False)
        tts.save(out_mp3)
        dur = probe_duration_seconds(out_mp3)
        if dur > 0.5:
            return dur
    except Exception:
        pass

    est_sec = max(2.5, min(8.0, len(spoken_text) / 14.0))
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{est_sec:.2f}", "-q:a", "9", out_mp3,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return est_sec


def build_segment_video(
    base_template: Image.Image,
    photo_rgb: Image.Image,
    headline: str,
    spoken_text: str,
    workdir: str,
    idx: int,
) -> str:
    audio_path = os.path.join(workdir, f"seg_{idx:02d}.mp3")
    seg_mp4 = os.path.join(workdir, f"seg_{idx:02d}.mp4")
    frames_dir = os.path.join(workdir, f"frames_{idx:02d}")
    os.makedirs(frames_dir, exist_ok=True)

    audio_dur = synthesize_voiceover(spoken_text, audio_path)
    total_dur = audio_dur + 0.35
    num_frames = max(12, int(math.ceil(total_dur * FPS)))

    for f_idx in range(num_frames):
        progress = f_idx / max(1, num_frames - 1)
        frame = render_frame(base_template, photo_rgb, headline, progress)
        frame.save(os.path.join(frames_dir, f"f_{f_idx:04d}.png"), format="PNG", compress_level=1)

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-framerate", str(FPS),
            "-i", os.path.join(frames_dir, "f_%04d.png"),
            "-i", audio_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            seg_mp4,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
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
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
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
    items = fetch_news_items(limit=MAX_ITEMS)

    output_path = os.environ.get("OUTPUT_MP4_PATH", "top25-latest.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        segments: List[str] = []

        first_item = items[0]
        first_photo = load_story_photo(first_item["image_url"], base_template)
        hook_spoken = clean_for_tts(
            f"Top 25 news stories right now on Brah-mins Television! {first_item['title']}."
        )
        segments.append(
            build_segment_video(
                base_template=base_template,
                photo_rgb=first_photo,
                headline=first_item["title"],
                spoken_text=hook_spoken,
                workdir=tmpdir,
                idx=0,
            )
        )

        running_sec = probe_duration_seconds(segments[0])
        for i, item in enumerate(items[1:], start=1):
            if running_sec >= TARGET_MAX_SECONDS - 4.0:
                break
            photo = load_story_photo(item["image_url"], base_template)
            spoken = clean_for_tts(f"{item['title']}. {item['summary']}")
            seg = build_segment_video(
                base_template=base_template,
                photo_rgb=photo,
                headline=item["title"],
                spoken_text=spoken,
                workdir=tmpdir,
                idx=i,
            )
            running_sec += probe_duration_seconds(seg)
            segments.append(seg)

        concat_segments(segments, output_path, tmpdir)
        print(f"Built {output_path} ({len(segments)} segments, {probe_duration_seconds(output_path):.1f}s)")


if __name__ == "__main__":
    main()
