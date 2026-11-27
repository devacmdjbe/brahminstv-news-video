import asyncio
import math
import os
import random
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import edge_tts
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FEED_URL = os.environ["NEWS_FEED_URL"]
WIDTH, HEIGHT = 1080, 1920
FPS = 25
MAX_TOTAL_SEC = 162.0  # Safely under 2m45s (165s)

# 3 Female + 2 Male Indian-accented voices
FEMALE_VOICES = [
    "en-IN-NeerjaNeural",
    "hi-IN-SwaraNeural",
    "ta-IN-PallaviNeural",
]
MALE_VOICES = [
    "en-IN-PrabhatNeural",
    "hi-IN-MadhurNeural",
]
ALL_VOICES = FEMALE_VOICES + MALE_VOICES

# Brahmins TV broadcast themes: Royal Purple, Crimson/Maroon, Deep Midnight
STUDIO_THEMES = [
    {
        "name": "Royal Purple & Gold",
        "bg_top": (24, 9, 54),
        "bg_bot": (10, 5, 24),
        "accent_top": (136, 19, 55),
        "gold": (250, 204, 21),
        "card_bg": (22, 13, 48),
        "ticker_bg": (15, 10, 34),
    },
    {
        "name": "Crimson Studio & Gold",
        "bg_top": (56, 10, 28),
        "bg_bot": (15, 6, 24),
        "accent_top": (76, 29, 149),
        "gold": (253, 224, 71),
        "card_bg": (34, 11, 28),
        "ticker_bg": (18, 9, 24),
    },
    {
        "name": "Indigo Temple Broadcast",
        "bg_top": (17, 24, 68),
        "bg_bot": (12, 8, 29),
        "accent_top": (153, 27, 27),
        "gold": (245, 158, 11),
        "card_bg": (20, 24, 58),
        "ticker_bg": (11, 15, 38),
    },
]

OPENING_LINES = [
    "Brahmins TV Top 25 News Shots. Unbowed. Unafraid. Here are the day's top headlines.",
    "Welcome to Brahmins TV Top 25 News Shots. Rapid updates across news and current affairs.",
    "You are watching Brahmins TV Top 25 News Shots. Let us dive straight into the headlines.",
]

CLOSING_LINES = [
    "Those were your Top 25 News Shots on Brahmins TV. Visit brahminstv dot com for more.",
    "Stay connected with Brahmins TV for round-the-clock updates at brahminstv dot com. Thank you for watching.",
    "That wraps this Brahmins TV Top 25 bulletin. More stories anytime at brahminstv dot com.",
]


def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


FONT_MASTHEAD = load_font(54, bold=True)
FONT_MASTHEAD_SUB = load_font(28, bold=True)
FONT_BADGE = load_font(38, bold=True)
FONT_HEADLINE = load_font(56, bold=True)
FONT_TITLE_BIG = load_font(66, bold=True)
FONT_SUB = load_font(34, bold=False)
FONT_TICKER = load_font(34, bold=True)


def get_audio_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], text=True).strip()
    return max(0.8, float(out))


async def synth_tts(text, voice, out_mp3, rate="+15%"):
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(out_mp3)


def extract_tag_url(elem):
    if elem is None:
        return ""
    for attr in ("url", "href", "src"):
        val = (elem.attrib.get(attr) or "").strip()
        if val.startswith("http"):
            return val
    txt = (elem.text or "").strip()
    if txt.startswith("http"):
        return txt
    return ""


def fetch_news_items():
    resp = requests.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    raw = resp.text.strip()
    items_out = []

    if raw.startswith("{") or raw.startswith("["):
        data = resp.json()
        entries = data.get("items", data) if isinstance(data, dict) else data
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or entry.get("headline") or "").strip()
            img = ""
            for key in ("imageUrl", "image_url", "image", "thumbnail", "photo", "cover"):
                val = entry.get(key)
                if isinstance(val, str) and val.strip().startswith("http"):
                    img = val.strip()
                    break
                if isinstance(val, dict):
                    for subk in ("url", "src", "href"):
                        if isinstance(val.get(subk), str) and val[subk].strip().startswith("http"):
                            img = val[subk].strip()
                            break
                    if img:
                        break
            if title:
                items_out.append({"title": title, "image": img})
    else:
        root = ET.fromstring(raw)
        entries = root.findall(".//item") or root.findall(".//{http://purl.org/rss/1.0/}item")
        for el in entries:
            title = ""
            for tpath in ("title", "{http://purl.org/rss/1.0/}title"):
                t_el = el.find(tpath)
                if t_el is not None and (t_el.text or "").strip():
                    title = t_el.text.strip()
                    break
            img = ""
            for ipath in (
                "enclosure",
                "{http://search.yahoo.com/mrss/}content",
                "{http://search.yahoo.com/mrss/}thumbnail",
                "image",
            ):
                found = el.find(ipath)
                img = extract_tag_url(found)
                if img:
                    break
            if title:
                items_out.append({"title": title, "image": img})

    seen = set()
    unique = []
    for it in items_out:
        norm = " ".join(it["title"].lower().split())
        if norm not in seen:
            seen.add(norm)
            unique.append(it)
        if len(unique) == 25:
            break
    return unique


def download_story_image(url, out_path):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "BrahminsTV-BulletinBot/1.0"})
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        im = Image.open(out_path).convert("RGB")
        return im
    except Exception:
        return None


def draw_studio_backdrop(draw, theme, seed_offset=0, ray_shift=0):
    w, h = WIDTH, HEIGHT
    r1, g1, b1 = theme["bg_top"]
    r2, g2, b2 = theme["bg_bot"]
    for y in range(0, h, 4):
        t = y / h
        rr = int(r1 + (r2 - r1) * t)
        gg = int(g1 + (g2 - g1) * t)
        bb = int(b1 + (b2 - b1) * t)
        draw.rectangle([0, y, w, y + 4], fill=(rr, gg, bb))

    gold = theme["gold"]
    # Subtle studio light beams
    for k in range(3):
        x_base = int(140 + k * 340 + ray_shift + (seed_offset * 37) % 120)
        poly = [
            (x_base, 180),
            (x_base + 110, 180),
            (x_base + 290, h - 140),
            (x_base + 140, h - 140),
        ]
        draw.polygon(poly, fill=(min(255, r1 + 22), min(255, g1 + 14), min(255, b1 + 36)))

    # Subtle architectural temple / gopuram silhouette motif in center-back
    cx = w // 2 + int(math.sin(seed_offset) * 35)
    sil_color = (min(255, r1 + 18), min(255, g1 + 12), min(255, b1 + 30))
    draw.rectangle([cx - 260, 520, cx + 260, 1480], fill=sil_color)
    draw.polygon([(cx - 290, 520), (cx, 330), (cx + 290, 520)], fill=sil_color)
    draw.polygon([(cx - 190, 350), (cx, 225), (cx + 190, 350)], fill=sil_color)
    for tier_y in range(580, 1380, 130):
        draw.line([(cx - 230, tier_y), (cx + 230, tier_y)], fill=(gold[0], gold[1], gold[2]), width=2)


def draw_masthead_and_footer(draw, theme, slot_label):
    gold = theme["gold"]
    # Top broadcast header
    draw.rectangle([0, 0, WIDTH, 168], fill=theme["accent_top"])
    draw.rectangle([0, 168, WIDTH, 178], fill=gold)
    draw.text((48, 34), "BRAHMINS TV", font=FONT_MASTHEAD, fill=(255, 255, 255))
    draw.text((48, 106), f"TOP 25 NEWS SHOTS  •  {slot_label}", font=FONT_MASTHEAD_SUB, fill=gold)

    # Bottom ticker bar
    draw.rectangle([0, HEIGHT - 118, WIDTH, HEIGHT], fill=theme["ticker_bg"])
    draw.rectangle([0, HEIGHT - 126, WIDTH, HEIGHT - 118], fill=gold)
    ticker = "brahminstv.com   •   UNBOWED. UNAFRAID."
    tw = draw.textlength(ticker, font=FONT_TICKER)
    draw.text(((WIDTH - tw) / 2, HEIGHT - 78), ticker, font=FONT_TICKER, fill=gold)


def render_story_frame(out_png, theme, slot_label, badge, headline, story_img=None, anim_style="slide", progress=0.5):
    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg_bot"])
    draw = ImageDraw.Draw(img)

    ray_shift = int((progress - 0.5) * 90)
    draw_studio_backdrop(draw, theme, seed_offset=len(headline), ray_shift=ray_shift)
    draw_masthead_and_footer(draw, theme, slot_label)

    gold = theme["gold"]

    # Story counter pill
    badge_w = draw.textlength(badge, font=FONT_BADGE) + 56
    badge_x = (WIDTH - badge_w) / 2
    draw.rounded_rectangle([badge_x, 212, badge_x + badge_w, 282], radius=18, fill=gold)
    draw.text((badge_x + 28, 224), badge, font=FONT_BADGE, fill=(15, 10, 30))

    # Media stage area (top-middle when image exists, or full studio showcase)
    stage_left, stage_top, stage_right, stage_bottom = 64, 312, WIDTH - 64, 1060
    if story_img is not None:
        # Blurred atmospheric background inside stage
        bg_crop = story_img.resize((stage_right - stage_left, stage_bottom - stage_top), Image.Resampling.LANCZOS)
        bg_crop = bg_crop.filter(ImageFilter.GaussianBlur(radius=14))
        img.paste(bg_crop, (stage_left, stage_top))

        # Zoom/pan fit inside stage
        zoom = 1.0 + 0.06 * progress
        sw, sh = story_img.size
        target_w = stage_right - stage_left - 24
        target_h = stage_bottom - stage_top - 24
        scale = min(target_w / sw, target_h / sh) * zoom
        rw, rh = max(1, int(sw * scale)), max(1, int(sh * scale))
        resized = story_img.resize((rw, rh), Image.Resampling.LANCZOS)
        px = (stage_left + stage_right - rw) // 2 + int((progress - 0.5) * 24)
        py = (stage_top + stage_bottom - rh) // 2
        # Clip cleanly to stage bounds
        clipped = resized.crop((
            max(0, stage_left - px),
            max(0, stage_top - py),
            min(rw, stage_right - px),
            min(rh, stage_bottom - py),
        ))
        paste_x = max(stage_left, px)
        paste_y = max(stage_top, py)
        img.paste(clipped, (paste_x, paste_y))
        draw.rectangle([stage_left, stage_top, stage_right, stage_bottom], outline=gold, width=4)

        card_top = 1100
    else:
        card_top = 420

    card_bottom = HEIGHT - 165
    lines = textwrap.wrap(headline.strip(), width=23)[:6]
    line_h = 78

    if anim_style == "slide":
        offset_y = int((1.0 - min(1.0, progress * 2.2)) * 120)
    elif anim_style == "wipe":
        offset_y = 0
    else:
        offset_y = int(math.sin(progress * math.pi) * -8)

    c_top = max(320, card_top + offset_y)
    draw.rounded_rectangle(
        [56, c_top, WIDTH - 56, card_bottom],
        radius=26,
        fill=theme["card_bg"],
        outline=gold,
        width=3,
    )

    # Gold accent bar inside card
    bar_w = int((WIDTH - 180) * (0.35 + 0.65 * min(1.0, progress * 1.8)))
    draw.rectangle([90, c_top + 28, 90 + bar_w, c_top + 38], fill=gold)

    text_y = c_top + 66
    for line in lines:
        lw = draw.textlength(line, font=FONT_HEADLINE)
        lx = (WIDTH - lw) / 2
        draw.text((lx + 3, text_y + 3), line, font=FONT_HEADLINE, fill=(0, 0, 0))
        draw.text((lx, text_y), line, font=FONT_HEADLINE, fill=(255, 255, 255))
        text_y += line_h

    img.save(out_png, "PNG")


def render_slate_card(out_png, theme, slot_label, badge, title_text, subtitle_text):
    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg_bot"])
    draw = ImageDraw.Draw(img)
    draw_studio_backdrop(draw, theme, seed_offset=7, ray_shift=0)
    draw_masthead_and_footer(draw, theme, slot_label)

    gold = theme["gold"]
    bw = draw.textlength(badge, font=FONT_BADGE) + 60
    bx = (WIDTH - bw) / 2
    draw.rounded_rectangle([bx, 420, bx + bw, 495], radius=20, fill=gold)
    draw.text((bx + 30, 434), badge, font=FONT_BADGE, fill=(15, 10, 30))

    draw.rounded_rectangle([60, 560, WIDTH - 60, 1340], radius=30, fill=theme["card_bg"], outline=gold, width=4)
    lines = textwrap.wrap(title_text, width=20)[:5]
    y = 640
    for ln in lines:
        w = draw.textlength(ln, font=FONT_TITLE_BIG)
        draw.text(((WIDTH - w) / 2, y), ln, font=FONT_TITLE_BIG, fill=(255, 255, 255))
        y += 92

    sub_lines = textwrap.wrap(subtitle_text, width=36)[:3]
    sy = y + 36
    for sl in sub_lines:
        sw = draw.textlength(sl, font=FONT_SUB)
        draw.text(((WIDTH - sw) / 2, sy), sl, font=FONT_SUB, fill=gold)
        sy += 48

    img.save(out_png, "PNG")


def generate_music_bed(total_sec, out_wav, seed):
    rng = random.Random(seed)
    sr = 22050
    n_samples = int(total_sec * sr)
    t = np.arange(n_samples, dtype=np.float64) / sr

    # Custom Brahmins TV broadcast harmonic motif (fifths + octave pulse)
    base_freq = rng.choice([110.0, 123.47, 130.81])  # A2, B2, C3
    intro_sting = np.zeros(n_samples, dtype=np.float64)
    sting_mask = t < 2.6
    st = t[sting_mask]
    # Rising 3-note news broadcast fanfare
    n1 = (st < 0.85).astype(np.float64)
    n2 = ((st >= 0.85) & (st < 1.70)).astype(np.float64)
    n3 = (st >= 1.70).astype(np.float64)
    env = np.exp(-1.4 * (st % 0.85))
    intro_sting[sting_mask] = 0.22 * env * (
        n1 * np.sin(2 * np.pi * base_freq * 2 * st)
        + n2 * np.sin(2 * np.pi * base_freq * 2.5 * st)
        + n3 * np.sin(2 * np.pi * base_freq * 3.0 * st)
    )

    # Subtle rhythmic news pulse underneath narration
    bpm = rng.choice([112, 120, 128])
    beat_period = 60.0 / bpm
    beat_phase = (t % beat_period) / beat_period
    pulse = np.exp(-6.0 * beat_phase) * np.sin(2 * np.pi * base_freq * t) * 0.065

    # Warm low drone pad
    pad = 0.04 * np.sin(2 * np.pi * base_freq * 0.5 * t) + 0.025 * np.sin(2 * np.pi * base_freq * 1.5 * t)

    # Outro resolution sting in final 2.5s
    outro = np.zeros(n_samples, dtype=np.float64)
    outro_start = max(0.0, total_sec - 2.5)
    omask = t >= outro_start
    ot = t[omask] - outro_start
    outro[omask] = 0.16 * np.exp(-1.5 * ot) * (
        np.sin(2 * np.pi * base_freq * 2 * ot) + 0.6 * np.sin(2 * np.pi * base_freq * 3 * ot)
    )

    mix = intro_sting + pulse + pad + outro
    # Fade in and fade out cleanly
    fade_in = np.minimum(1.0, t / 0.4)
    fade_out = np.minimum(1.0, np.maximum(0.0, (total_sec - t) / 0.8))
    mix = mix * fade_in * fade_out
    mix = np.clip(mix, -0.85, 0.85)

    pcm16 = (mix * 32767).astype(np.int16)
    raw_pcm = "music_bed.raw"
    with open(raw_pcm, "wb") as f:
        f.write(pcm16.tobytes())

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", raw_pcm,
        "-c:a", "pcm_s16le", out_wav
    ], check=True)


def build_voice_plan(count, rng):
    # Ensure 3 female & 2 male voices are shuffled within and across bulletins
    pool = []
    while len(pool) < count:
        batch = FEMALE_VOICES + MALE_VOICES
        rng.shuffle(batch)
        # Avoid back-to-back identical voice when possible
        if pool and batch[0] == pool[-1]:
            batch.reverse()
        pool.extend(batch)
    return pool[:count]


def main():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    slot_label = ist_now.strftime("%d %b • %I:%M %p IST")
    seed = int(ist_now.strftime("%Y%m%d%H"))
    rng = random.Random(seed)

    theme = rng.choice(STUDIO_THEMES)
    news_items = fetch_news_items()
    if not news_items:
        raise RuntimeError("No news items returned by NEWS_FEED_URL")

    voices = build_voice_plan(len(news_items) + 2, rng)
    animations = ["slide", "wipe", "zoom"]

    segments = []
    segments.append({
        "kind": "open",
        "badge": "OPENING BULLETIN",
        "title": "BRAHMINS TV TOP 25 NEWS SHOTS",
        "sub": f"{slot_label}  •  Unbowed. Unafraid.",
        "spoken": rng.choice(OPENING_LINES),
        "voice": voices[0],
        "rate": "+12%",
        "image": "",
        "anim": "zoom",
    })

    for idx, item in enumerate(news_items, start=1):
        segments.append({
            "kind": "news",
            "badge": f"NEWS {idx:02d} / {len(news_items):02d}",
            "title": item["title"],
            "sub": "",
            "spoken": f"Story {idx}. {item['title']}.",
            "voice": voices[idx],
            "rate": "+17%",
            "image": item.get("image", ""),
            "anim": animations[(idx - 1 + rng.randint(0, 2)) % len(animations)],
        })

    closing_seg = {
        "kind": "close",
        "badge": "BULLETIN SIGN-OFF",
        "title": "THANK YOU FOR WATCHING BRAHMINS TV",
        "sub": "brahminstv.com  •  7 AM • 12 PM • 5 PM • 10 PM IST",
        "spoken": rng.choice(CLOSING_LINES),
        "voice": voices[-1],
        "rate": "+12%",
        "image": "",
        "anim": "zoom",
    }

    reserve_closing = 7.0
    voice_clips = []
    running = 0.0

    for i, seg in enumerate(segments):
        if running + reserve_closing >= MAX_TOTAL_SEC:
            break
        mp3_path = f"voice_{i}.mp3"
        asyncio.run(synth_tts(seg["spoken"], seg["voice"], mp3_path, rate=seg["rate"]))
        dur = get_audio_duration(mp3_path)
        if running + dur + reserve_closing > MAX_TOTAL_SEC:
            break
        voice_clips.append((seg, mp3_path, dur))
        running += dur

    c_mp3 = "voice_close.mp3"
    asyncio.run(synth_tts(closing_seg["spoken"], closing_seg["voice"], c_mp3, rate=closing_seg["rate"]))
    c_dur = min(get_audio_duration(c_mp3), max(2.5, MAX_TOTAL_SEC - running))
    voice_clips.append((closing_seg, c_mp3, c_dur))
    running += c_dur

    # Render each segment's video clip
    segment_videos = []
    for idx, (seg, mp3_path, dur) in enumerate(voice_clips):
        seg_mp4 = f"seg_{idx:02d}.mp4"
        if seg["kind"] == "news":
            raw_img = f"raw_{idx:02d}.img"
            story_img = download_story_image(seg["image"], raw_img)
            # Render 3 keyframes (start, mid, end) for smooth Ken Burns & headline reveal
            k0, k1, k2 = f"k0_{idx:02d}.png", f"k1_{idx:02d}.png", f"k2_{idx:02d}.png"
            render_story_frame(k0, theme, slot_label, seg["badge"], seg["title"], story_img, seg["anim"], 0.15)
            render_story_frame(k1, theme, slot_label, seg["badge"], seg["title"], story_img, seg["anim"], 0.55)
            render_story_frame(k2, theme, slot_label, seg["badge"], seg["title"], story_img, seg["anim"], 0.95)
            # Crossfade across the 3 frames to match duration smoothly
            t1 = max(0.3, dur * 0.35)
            t2 = max(0.3, dur * 0.70)
            subprocess.run([
                "ffmpeg", "-y",
                "-loop", "1", "-t", f"{t1 + 0.35:.2f}", "-i", k0,
                "-loop", "1", "-t", f"{t2 - t1 + 0.35:.2f}", "-i", k1,
                "-loop", "1", "-t", f"{max(0.5, dur - t2 + 0.35):.2f}", "-i", k2,
                "-i", mp3_path,
                "-filter_complex",
                f"[0:v]fps={FPS},format=yuv420p[v0];"
                f"[1:v]fps={FPS},format=yuv420p[v1];"
                f"[2:v]fps={FPS},format=yuv420p[v2];"
                f"[v0][v1]xfade=transition=fade:duration=0.25:offset={max(0.1, t1 - 0.1):.2f}[x1];"
                f"[x1][v2]xfade=transition=fade:duration=0.25:offset={max(0.2, t2 - 0.1):.2f},trim=duration={dur:.2f},setpts=PTS-STARTPTS[vout]",
                "-map", "[vout]", "-map", "3:a",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                seg_mp4
            ], check=True)
        else:
            slate_png = f"slate_{idx:02d}.png"
            render_slate_card(slate_png, theme, slot_label, seg["badge"], seg["title"], seg["sub"])
            subprocess.run([
                "ffmpeg", "-y",
                "-loop", "1", "-i", slate_png,
                "-i", mp3_path,
                "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast",
                "-r", str(FPS), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-t", f"{dur:.2f}",
                "-shortest",
                seg_mp4
            ], check=True)
        segment_videos.append(seg_mp4)

    with open("list.txt", "w", encoding="utf-8") as f:
        for vid in segment_videos:
            f.write(f"file '{vid}'\n")

    voice_only_mp4 = "voice_only.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", "list.txt",
        "-c", "copy",
        voice_only_mp4
    ], check=True)

    total_dur = get_audio_duration(voice_only_mp4)
    music_wav = "music_bed.wav"
    generate_music_bed(total_dur, music_wav, seed)

    # Mix ducked Brahmins TV music bed under voice narration
    subprocess.run([
        "ffmpeg", "-y",
        "-i", voice_only_mp4,
        "-i", music_wav,
        "-filter_complex",
        "[1:a]volume=0.45[mus];[0:a][mus]amix=inputs=2:duration=first:weights=1.0 0.45[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "top25-latest.mp4"
    ], check=True)


if __name__ == "__main__":
    main()
