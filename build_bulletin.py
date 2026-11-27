#!/usr/bin/env python3
"""
Brahmins TV - Top 25 News Shots Vertical (9:16) Bulletin Builder
Guaranteed fixes:
  1. Images: Extracts image URLs from RSS media tags, enclosures, AND HTML <img> tags inside description/content;
     generates a clean, branded dynamic news visual card if a story has no valid web image (never blank).
  2. Audio: Uses reliable gTTS + offline espeak-ng fallback (installed in workflow) so every segment ALWAYS
     has spoken Indian-English audio with "Brah-mins Television / T V" pronunciation (never silent).
  3. Smooth Animation: Per-frame smooth Ken Burns zoom/pan + horizontal breaking-news ticker/entrance slide.
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
    "https://www.thehindu.com/news/national/feeder/default.rss",
    "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
    "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "https://www.indiatoday.in/rss/1206578",
    "https://feeds.feedburner.com/ndtvnews-top-stories",
    "https://www.news18.com/rss/india.xml",
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
        .replace("&nbsp;", " ")
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


def _extract_img_from_html(html_text: str) -> str:
    if not html_text:
        return ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, flags=re.IGNORECASE)
    if m:
        url = m.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and "pixel" not in url.lower() and "1x1" not in url.lower():
            return url
    return ""


def parse_rss_entries(xml_bytes: bytes) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    root = ET.fromstring(xml_bytes)
    raw_items = [elem for elem in root.iter() if _local_name(elem.tag) in ("item", "entry")]

    for node in raw_items:
        title = ""
        summary_raw = ""
        image_url = ""

        for child in node:
            cname = _local_name(child.tag)
            if cname == "title" and not title:
                title = "".join(child.itertext())
            elif cname in ("description", "summary", "encoded", "content") and not summary_raw:
                # Preserve inner XML/HTML so <img src="..."> can be extracted
                inner_parts = [child.text or ""]
                for sub in child:
                    inner_parts.append(ET.tostring(sub, encoding="unicode"))
                summary_raw = "".join(inner_parts)
            elif cname in ("thumbnail", "enclosure") or (cname == "content" and child.attrib.get("url")):
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

        if not image_url and summary_raw:
            image_url = _extract_img_from_html(summary_raw)

        if title:
            entries.append({
                "title": title,
                "summary": summary_raw or title,
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
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
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
                    "summary": summary[:180],
                    "image_url": (entry.get("image_url") or "").strip(),
                })
        except Exception as e:
            print(f"[WARN] Feed failed {feed_url}: {e}")
            continue

    while len(items) < min(limit, 5):
        idx = len(items) + 1
        items.append({
            "title": f"Top Story {idx} on Brah-mins Television",
            "summary": f"Latest update number {idx} from Brah-mins Television news desk.",
            "image_url": "",
        })

    print(f"[INFO] Fetched {len(items)} news items ({sum(1 for x in items if x['image_url'])} with image URLs)")
    return items[:limit]


def generate_fallback_news_visual(story_num: int, headline: str, pw: int, ph: int) -> Image.Image:
    """
    Generates a crisp, high-contrast broadcast visual card when an RSS item has no valid web image
    so the upper photo slot is NEVER blank dark navy. Contains NO extra logo inside the image.
    """
    palettes = [
        ((18, 36, 84), (178, 24, 38), (245, 196, 62)),
        ((42, 18, 78), (196, 36, 68), (255, 210, 75)),
        ((14, 52, 74), (204, 68, 24), (255, 224, 102)),
        ((58, 16, 48), (188, 28, 44), (248, 200, 70)),
    ]
    c1, c2, accent = palettes[(story_num - 1) % len(palettes)]

    card = Image.new("RGB", (pw, ph), c1)
    draw = ImageDraw.Draw(card)

    # Diagonal broadcast backdrop stripes
    for y in range(0, ph, 6):
        ratio = y / max(1, ph)
        r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
        g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
        b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
        draw.line([(0, y), (pw, y)], fill=(r, g, b), width=6)

    # Subtle geometric broadcast grid lines
    for gx in range(0, pw, 90):
        draw.line([(gx, 0), (gx, ph)], fill=(255, 255, 255, 25), width=1)
    for gy in range(0, ph, 90):
        draw.line([(0, gy), (pw, gy)], fill=(255, 255, 255, 25), width=1)

    # Top-left story badge: "NEWS 01 / 25"
    badge_font = load_font(34)
    badge_text = f"NEWS {story_num:02d} / {MAX_ITEMS:02d}"
    bx1, by1, bx2, by2 = 36, 32, 310, 92
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=10, fill=accent)
    draw.text((bx1 + 22, by1 + 12), badge_text, font=badge_font, fill=(15, 15, 25))

    # Central summary card inside visual frame
    keywords = [w.upper() for w in re.findall(r"[A-Za-z0-9']+", headline) if len(w) > 2][:6]
    banner_text = " • ".join(keywords[:3]) if keywords else "TOP HEADLINE"
    sub_text = " ".join(keywords[3:6]) if len(keywords) > 3 else "INDIA DESK UPDATE"

    big_font = load_font(54)
    sub_font = load_font(36)
    lines = wrap_lines_clean(draw, banner_text, big_font, pw - 120, max_lines=3)
    y_cursor = ph // 2 - (len(lines) * 66) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=big_font)
        lw = bbox[2] - bbox[0]
        lx = (pw - lw) // 2
        draw.text((lx + 3, y_cursor + 3), line, font=big_font, fill=(0, 0, 0))
        draw.text((lx, y_cursor), line, font=big_font, fill=(255, 255, 255))
        y_cursor += 68

    sbbox = draw.textbbox((0, 0), sub_text, font=sub_font)
    slw = sbbox[2] - sbbox[0]
    draw.text(((pw - slw) // 2, y_cursor + 16), sub_text, font=sub_font, fill=accent)

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
            print(f"[WARN] Image download failed for story {story_num} ({image_url}): {e}")

    return generate_fallback_news_visual(story_num, headline, pw, ph)


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
    """Pre-renders the headline text image once per story for crisp, smooth slide-in animation."""
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    box_w, box_h = nx2 - nx1, ny2 - ny1
    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    pad_x, pad_y = 32, 20
    inner_w = box_w - pad_x * 2
    inner_h = box_h - pad_y * 2 - 36

    # Small story counter pill at top-right inside lower box
    counter_font = load_font(24)
    counter_txt = f"{story_num:02d}/{total_stories:02d}"
    cbbox = draw.textbbox((0, 0), counter_txt, font=counter_font)
    cw = cbbox[2] - cbbox[0]
    draw.text((box_w - cw - 28, 12), counter_txt, font=counter_font, fill=(255, 215, 80, 255))

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
    start_y = 34 + pad_y + max(0, (inner_h - total_text_h) // 2)

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
    """
    Renders one 1080x1920 RGB frame:
      1) Upper slot: smooth Ken Burns zoom (1.00x -> 1.12x) + directional pan
      2) Lower slot: smooth ease-out slide-in of the breaking-news headline during first 0.45s
    """
    t = max(0.0, min(1.0, progress))
    ease = 0.5 - 0.5 * math.cos(math.pi * t)

    frame = base_template.copy()

    # 1. Upper Photo Slot: smooth zoom (1.00x -> 1.12x) & pan
    px1, py1, px2, py2 = SLOT_PHOTO
    pw, ph = px2 - px1, py2 - py1
    zoom = 1.0 + 0.12 * ease
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

    # 2. Lower Breaking News Slot: smooth horizontal slide-in entrance
    nx1, ny1, nx2, ny2 = SLOT_NEWS
    enter_t = min(1.0, t / 0.15)  # smooth slide over initial ~15% of segment
    slide_ease = 1.0 - (1.0 - enter_t) ** 3
    offset_x = int(round((1.0 - slide_ease) * 140))
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


def synthesize_voiceover(spoken_text: str, out_wav: str, idx: int) -> float:
    """
    Produces guaranteed audible speech WAV for every segment:
      1) Primary: gTTS (Indian English co.in) -> decoded to 44.1kHz mono WAV
      2) Offline Backup: espeak-ng -> 44.1kHz mono WAV (never silent!)
    """
    tmp_mp3 = out_wav + ".mp3"
    try:
        from gtts import gTTS
        tts = gTTS(text=spoken_text, lang="en", tld="co.in", slow=False)
        tts.save(tmp_mp3)
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "44100", "-ac", "1", out_wav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        dur = probe_duration_seconds(out_wav)
        if dur > 0.6:
            return dur
    except Exception as e:
        print(f"[WARN] gTTS failed on segment {idx}, switching to offline espeak-ng: {e}")

    # Offline TTS fallback so audio is ALWAYS audible
    raw_espeak = out_wav + ".raw.wav"
    subprocess.run(
        ["espeak-ng", "-v", "en-in", "-s", "155", "-w", raw_espeak, spoken_text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", raw_espeak, "-ar", "44100", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    dur = probe_duration_seconds(out_wav)
    if dur <= 0.3:
        raise RuntimeError(f"Both gTTS and espeak-ng failed to produce audio for segment {idx}")
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

    audio_dur = synthesize_voiceover(spoken_text, audio_path, idx)
    total_dur = max(2.8, audio_dur + 0.35)
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
    items = fetch_news_items(limit=MAX_ITEMS)
    total_stories = len(items)

    output_path = os.environ.get("OUTPUT_MP4_PATH", "top25-latest.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        segments: List[str] = []

        first_item = items[0]
        first_photo = load_story_photo(first_item["image_url"], 1, first_item["title"])
        hook_spoken = clean_for_tts(
            f"Top 25 news stories right now on Brah-mins Television! Number 1: {first_item['title']}."
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
            if running_sec >= TARGET_MAX_SECONDS - 4.0:
                break
            story_num = i + 1
            photo = load_story_photo(item["image_url"], story_num, item["title"])
            spoken = clean_for_tts(f"Number {story_num}: {item['title']}.")
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
