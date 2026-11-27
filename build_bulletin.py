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

LOCAL_BANNER_PATHS = [
    "assets/assetsstudio-banner.png",
    "assets/studio-banner.png",
]
REMOTE_BANNER_URL = (
    "https://raw.githubusercontent.com/devacmdjbe/brahmins-tv-website/main/assets/assetsstudio-banner.png"
)

# 3 Female + 2 Male Indian English voice slots (shuffled within & between bulletins)
VOICE_SLOTS = [
    {"voice": "en-IN-NeerjaNeural", "pitch": "+0Hz", "rate_delta": 0},
    {"voice": "en-IN-NeerjaNeural", "pitch": "+12Hz", "rate_delta": 2},
    {"voice": "en-IN-NeerjaNeural", "pitch": "-8Hz", "rate_delta": -1},
    {"voice": "en-IN-PrabhatNeural", "pitch": "+0Hz", "rate_delta": 0},
    {"voice": "en-IN-PrabhatNeural", "pitch": "-10Hz", "rate_delta": 1},
]

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

SKIP_PHRASES = [
    "testing multiple digital tv options",
    "part of the trial",
    "digital tv trial",
    "brahmins tv trial",
]

TITLE_CORRECTIONS = [
    ("chief minister mk stalin", "former chief minister mk stalin"),
    ("chief minister m. k. stalin", "former chief minister m. k. stalin"),
    ("chief minister m k stalin", "former chief minister m. k. stalin"),
    ("tn chief minister stalin", "former tn chief minister mk stalin"),
    ("tamil nadu chief minister stalin", "former tamil nadu chief minister mk stalin"),
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


FONT_MASTHEAD = load_font(48, bold=True)
FONT_MASTHEAD_SUB = load_font(26, bold=True)
FONT_BADGE = load_font(36, bold=True)
FONT_HOOK_BIG = load_font(74, bold=True)
FONT_HEADLINE = load_font(54, bold=True)
FONT_TITLE_BIG = load_font(62, bold=True)
FONT_SUB = load_font(34, bold=False)
FONT_TICKER = load_font(32, bold=True)


def apply_title_corrections(text):
    out = text.strip()
    for wrong, right in TITLE_CORRECTIONS:
        idx = out.lower().find(wrong)
        while idx != -1:
            matched = out[idx : idx + len(wrong)]
            replacement = right.title() if matched.isupper() else (
                right.capitalize() if matched and matched[0].isupper() else right
            )
            # Preserve clean capitalization for MK Stalin / M. K. Stalin
            replacement = (
                replacement.replace("Mk Stalin", "M. K. Stalin")
                .replace("M. K. Stalin", "M. K. Stalin")
                .replace("Tn ", "TN ")
            )
            out = out[:idx] + replacement + out[idx + len(wrong) :]
            idx = out.lower().find(wrong, idx + len(replacement))
    return out


def is_trial_item(title):
    low = " ".join(title.lower().split())
    return any(phrase in low for phrase in SKIP_PHRASES)


def load_banner_assets():
    banner_img = None
    for p in LOCAL_BANNER_PATHS:
        if os.path.exists(p):
            try:
                banner_img = Image.open(p).convert("RGB")
                break
            except Exception:
                pass

    if banner_img is None:
        try:
            resp = requests.get(REMOTE_BANNER_URL, timeout=15)
            resp.raise_for_status()
            tmp_path = "fetched_banner.png"
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            banner_img = Image.open(tmp_path).convert("RGB")
        except Exception:
            banner_img = None

    if banner_img is None:
        return {"emblem": None, "left_bg": None, "right_bg": None}

    bw, bh = banner_img.size
    # Center emblem box from the 16:9 banner
    emblem_crop = banner_img.crop((int(bw * 0.24), int(bh * 0.21), int(bw * 0.77), int(bh * 0.65)))

    # Tall 9:16 vertical crops from left (temple/lamp/scriptures) and right (Parliament/globe)
    target_w = int(bh * (9.0 / 16.0))
    target_w = min(bw, max(1, target_w))
    left_crop = banner_img.crop((0, 0, target_w, bh)).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    right_crop = banner_img.crop((max(0, bw - target_w), 0, bw, bh)).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    return {"emblem": emblem_crop, "left_bg": left_crop, "right_bg": right_crop}


def get_audio_duration(path):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return max(0.8, float(out))


async def synth_tts(text, slot, out_mp3, base_rate=16):
    rate_val = max(-10, min(35, base_rate + slot["rate_delta"]))
    rate_str = f"{rate_val:+d}%"
    comm = edge_tts.Communicate(text, slot["voice"], rate=rate_str, pitch=slot["pitch"])
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
        corrected = apply_title_corrections(it["title"])
        if is_trial_item(corrected):
            continue
        norm = " ".join(corrected.lower().split())
        if norm not in seen:
            seen.add(norm)
            unique.append({"title": corrected, "image": it.get("image", "")})
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
        return Image.open(out_path).convert("RGB")
    except Exception:
        return None


def draw_studio_backdrop(draw, img, theme, banner_assets, use_right=False, seed_offset=0, ray_shift=0):
    bg_asset = banner_assets["right_bg"] if use_right else banner_assets["left_bg"]
    if bg_asset is not None:
        img.paste(bg_asset, (0, 0))
        # Darken slightly so text stays super crisp and legible
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (12, 6, 26, 105))
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw = ImageDraw.Draw(img)
    else:
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
    for k in range(2):
        x_base = int(120 + k * 420 + ray_shift + (seed_offset * 41) % 140)
        poly = [(x_base, 190), (x_base + 90, 190), (x_base + 240, HEIGHT - 150), (x_base + 110, HEIGHT - 150)]
        draw.polygon(poly, fill=(45, 22, 78))

    return ImageDraw.Draw(img)


def paste_emblem_badge(img, emblem_crop, box_rect, gold):
    if emblem_crop is None:
        return
    x1, y1, x2, y2 = box_rect
    tw, th = x2 - x1, y2 - y1
    ew, eh = emblem_crop.size
    scale = min(tw / ew, th / eh)
    rw, rh = max(1, int(ew * scale)), max(1, int(eh * scale))
    resized = emblem_crop.resize((rw, rh), Image.Resampling.LANCZOS)
    px = x1 + (tw - rw) // 2
    py = y1 + (th - rh) // 2
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([px - 4, py - 4, px + rw + 4, py + rh + 4], radius=16, fill=gold)
    img.paste(resized, (px, py))


def draw_masthead_and_footer(draw, img, theme, banner_assets, slot_label):
    gold = theme["gold"]
    draw.rectangle([0, 0, WIDTH, 164], fill=theme["accent_top"])
    draw.rectangle([0, 164, WIDTH, 174], fill=gold)
    draw.text((40, 32), "BRAHMINS TV", font=FONT_MASTHEAD, fill=(255, 255, 255))
    draw.text((40, 98), f"TOP 25 NEWS SHOTS  •  {slot_label}", font=FONT_MASTHEAD_SUB, fill=gold)

    # Embed Brahmins TV emblem in top-right header
    paste_emblem_badge(img, banner_assets.get("emblem"), (WIDTH - 245, 14, WIDTH - 24, 152), gold)

    draw.rectangle([0, HEIGHT - 114, WIDTH, HEIGHT], fill=theme["ticker_bg"])
    draw.rectangle([0, HEIGHT - 122, WIDTH, HEIGHT - 114], fill=gold)
    ticker = "brahminstv.com   •   UNBOWED. UNAFRAID."
    tw = draw.textlength(ticker, font=FONT_TICKER)
    draw.text(((WIDTH - tw) / 2, HEIGHT - 76), ticker, font=FONT_TICKER, fill=gold)


def render_opening_hook(out_png, theme, banner_assets, slot_label, lead_title, preview_imgs):
    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg_bot"])
    draw = draw_studio_backdrop(draw=None, img=img, theme=theme, banner_assets=banner_assets, use_right=False, seed_offset=1)
    gold = theme["gold"]

    # Top emblem banner
    draw.rectangle([0, 0, WIDTH, 250], fill=(22, 9, 48))
    draw.rectangle([0, 250, WIDTH, 260], fill=gold)
    paste_emblem_badge(img, banner_assets.get("emblem"), (290, 18, WIDTH - 290, 236), gold)

    # Top-middle photo strip (up to 3 top story images)
    strip_top, strip_bot = 285, 760
    valid_imgs = [im for im in preview_imgs if im is not None][:3]
    if valid_imgs:
        cols = len(valid_imgs)
        gap = 16
        total_w = WIDTH - 96 - gap * (cols - 1)
        cw = total_w // cols
        for idx, sim in enumerate(valid_imgs):
            cx1 = 48 + idx * (cw + gap)
            cx2 = cx1 + cw
            sw, sh = sim.size
            scale = max(cw / sw, (strip_bot - strip_top) / sh)
            rw, rh = int(sw * scale), int(sh * scale)
            rs = sim.resize((rw, rh), Image.Resampling.LANCZOS)
            crop_box = ((rw - cw) // 2, max(0, (rh - (strip_bot - strip_top)) // 3), (rw - cw) // 2 + cw, max(0, (rh - (strip_bot - strip_top)) // 3) + (strip_bot - strip_top))
            img.paste(rs.crop(crop_box), (cx1, strip_top))
            draw.rectangle([cx1, strip_top, cx2, strip_bot], outline=gold, width=3)
    else:
        draw.rounded_rectangle([48, strip_top, WIDTH - 48, strip_bot], radius=24, fill=theme["card_bg"], outline=gold, width=3)

    # Bold stacked hook banner (inspired by 9:16 mobile hook layout)
    hook_lines = ["TOP 25", "NEWS SHOTS"]
    y = 795
    for hl in hook_lines:
        hw = draw.textlength(hl, font=FONT_HOOK_BIG)
        hx = (WIDTH - hw) / 2
        draw.rounded_rectangle([hx - 28, y - 8, hx + hw + 28, y + 88], radius=16, fill=(153, 27, 27), outline=gold, width=3)
        draw.text((hx, y), hl, font=FONT_HOOK_BIG, fill=gold)
        y += 108

    # Lead story hook card below
    lead_lines = textwrap.wrap(lead_title, width=22)[:3]
    draw.rounded_rectangle([48, y + 10, WIDTH - 48, HEIGHT - 150], radius=24, fill=theme["card_bg"], outline=gold, width=3)
    ly = y + 36
    for ln in lead_lines:
        lw = draw.textlength(ln, font=FONT_HEADLINE)
        draw.text(((WIDTH - lw) / 2, ly), ln, font=FONT_HEADLINE, fill=(255, 255, 255))
        ly += 68
    sub = f"{slot_label}  •  BRAHMINS TV"
    sw = draw.textlength(sub, font=FONT_MASTHEAD_SUB)
    draw.text(((WIDTH - sw) / 2, HEIGHT - 205), sub, font=FONT_MASTHEAD_SUB, fill=gold)

    draw.rectangle([0, HEIGHT - 114, WIDTH, HEIGHT], fill=theme["ticker_bg"])
    draw.rectangle([0, HEIGHT - 122, WIDTH, HEIGHT - 114], fill=gold)
    ticker = "brahminstv.com   •   UNBOWED. UNAFRAID."
    tw = draw.textlength(ticker, font=FONT_TICKER)
    draw.text(((WIDTH - tw) / 2, HEIGHT - 76), ticker, font=FONT_TICKER, fill=gold)

    img.save(out_png, "PNG")


def render_story_frame(out_png, theme, banner_assets, slot_label, badge, headline, story_img=None, use_right=False, progress=0.5):
    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg_bot"])
    draw = draw_studio_backdrop(
        draw=None,
        img=img,
        theme=theme,
        banner_assets=banner_assets,
        use_right=use_right,
        seed_offset=len(headline),
        ray_shift=int((progress - 0.5) * 60),
    )
    draw_masthead_and_footer(draw, img, theme, banner_assets, slot_label)

    gold = theme["gold"]

    badge_w = draw.textlength(badge, font=FONT_BADGE) + 56
    badge_x = (WIDTH - badge_w) / 2
    draw.rounded_rectangle([badge_x, 204, badge_x + badge_w, 272], radius=18, fill=gold)
    draw.text((badge_x + 28, 216), badge, font=FONT_BADGE, fill=(15, 10, 30))

    stage_left, stage_top, stage_right, stage_bottom = 56, 302, WIDTH - 56, 1055
    if story_img is not None:
        bg_crop = story_img.resize((stage_right - stage_left, stage_bottom - stage_top), Image.Resampling.LANCZOS)
        bg_crop = bg_crop.filter(ImageFilter.GaussianBlur(radius=14))
        img.paste(bg_crop, (stage_left, stage_top))

        zoom = 1.0 + 0.05 * progress
        sw, sh = story_img.size
        target_w = stage_right - stage_left - 20
        target_h = stage_bottom - stage_top - 20
        scale = min(target_w / sw, target_h / sh) * zoom
        rw, rh = max(1, int(sw * scale)), max(1, int(sh * scale))
        resized = story_img.resize((rw, rh), Image.Resampling.LANCZOS)
        px = (stage_left + stage_right - rw) // 2 + int((progress - 0.5) * 18)
        py = (stage_top + stage_bottom - rh) // 2
        clipped = resized.crop((
            max(0, stage_left - px),
            max(0, stage_top - py),
            min(rw, stage_right - px),
            min(rh, stage_bottom - py),
        ))
        img.paste(clipped, (max(stage_left, px), max(stage_top, py)))
        draw.rectangle([stage_left, stage_top, stage_right, stage_bottom], outline=gold, width=4)

        # Stamp Brahmins TV emblem in lower-right corner of the story photo stage
        paste_emblem_badge(
            img,
            banner_assets.get("emblem"),
            (stage_right - 210, stage_bottom - 142, stage_right - 12, stage_bottom - 12),
            gold,
        )
        card_top = 1092
    else:
        card_top = 380

    card_bottom = HEIGHT - 156
    lines = textwrap.wrap(headline.strip(), width=23)[:6]
    line_h = 76

    draw.rounded_rectangle(
        [52, card_top, WIDTH - 52, card_bottom],
        radius=26,
        fill=theme["card_bg"],
        outline=gold,
        width=3,
    )

    bar_w = int((WIDTH - 176) * (0.4 + 0.6 * min(1.0, progress * 1.6)))
    draw.rectangle([88, card_top + 26, 88 + bar_w, card_top + 36], fill=gold)

    text_y = card_top + 62
    for line in lines:
        lw = draw.textlength(line, font=FONT_HEADLINE)
        lx = (WIDTH - lw) / 2
        draw.text((lx + 3, text_y + 3), line, font=FONT_HEADLINE, fill=(0, 0, 0))
        draw.text((lx, text_y), line, font=FONT_HEADLINE, fill=(255, 255, 255))
        text_y += line_h

    img.save(out_png, "PNG")


def render_closing_slate(out_png, theme, banner_assets, slot_label, badge, title_text, subtitle_text):
    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg_bot"])
    draw_studio_backdrop(draw=None, img=img, theme=theme, banner_assets=banner_assets, use_right=True, seed_offset=11)
    draw = ImageDraw.Draw(img)
    draw_masthead_and_footer(draw, img, theme, banner_assets, slot_label)

    gold = theme["gold"]
    paste_emblem_badge(img, banner_assets.get("emblem"), (260, 225, WIDTH - 260, 525), gold)

    bw = draw.textlength(badge, font=FONT_BADGE) + 60
    bx = (WIDTH - bw) / 2
    draw.rounded_rectangle([bx, 565, bx + bw, 635], radius=18, fill=gold)
    draw.text((bx + 30, 578), badge, font=FONT_BADGE, fill=(15, 10, 30))

    draw.rounded_rectangle([56, 675, WIDTH - 56, 1380], radius=28, fill=theme["card_bg"], outline=gold, width=4)
    lines = textwrap.wrap(title_text, width=20)[:4]
    y = 745
    for ln in lines:
        w = draw.textlength(ln, font=FONT_TITLE_BIG)
        draw.text(((WIDTH - w) / 2, y), ln, font=FONT_TITLE_BIG, fill=(255, 255, 255))
        y += 88

    sub_lines = textwrap.wrap(subtitle_text, width=36)[:3]
    sy = y + 32
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

    base_freq = rng.choice([110.0, 123.47, 130.81])
    intro_sting = np.zeros(n_samples, dtype=np.float64)
    sting_mask = t < 2.4
    st = t[sting_mask]
    n1 = (st < 0.80).astype(np.float64)
    n2 = ((st >= 0.80) & (st < 1.60)).astype(np.float64)
    n3 = (st >= 1.60).astype(np.float64)
    env = np.exp(-1.5 * (st % 0.80))
    intro_sting[sting_mask] = 0.20 * env * (
        n1 * np.sin(2 * np.pi * base_freq * 2 * st)
        + n2 * np.sin(2 * np.pi * base_freq * 2.5 * st)
        + n3 * np.sin(2 * np.pi * base_freq * 3.0 * st)
    )

    bpm = rng.choice([114, 120, 126])
    beat_period = 60.0 / bpm
    beat_phase = (t % beat_period) / beat_period
    pulse = np.exp(-6.2 * beat_phase) * np.sin(2 * np.pi * base_freq * t) * 0.055

    pad = 0.035 * np.sin(2 * np.pi * base_freq * 0.5 * t) + 0.02 * np.sin(2 * np.pi * base_freq * 1.5 * t)

    outro = np.zeros(n_samples, dtype=np.float64)
    outro_start = max(0.0, total_sec - 2.4)
    omask = t >= outro_start
    ot = t[omask] - outro_start
    outro[omask] = 0.15 * np.exp(-1.5 * ot) * (
        np.sin(2 * np.pi * base_freq * 2 * ot) + 0.6 * np.sin(2 * np.pi * base_freq * 3 * ot)
    )

    mix = intro_sting + pulse + pad + outro
    fade_in = np.minimum(1.0, t / 0.4)
    fade_out = np.minimum(1.0, np.maximum(0.0, (total_sec - t) / 0.8))
    mix = np.clip(mix * fade_in * fade_out, -0.85, 0.85)

    raw_pcm = "music_bed.raw"
    with open(raw_pcm, "wb") as f:
        f.write((mix * 32767).astype(np.int16).tobytes())

    subprocess.run(
        ["ffmpeg", "-y", "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", raw_pcm, "-c:a", "pcm_s16le", out_wav],
        check=True,
    )


def build_voice_plan(count, rng):
    plan = []
    while len(plan) < count:
        batch = VOICE_SLOTS.copy()
        rng.shuffle(batch)
        if plan and batch[0] == plan[-1]:
            batch.reverse()
        plan.extend(batch)
    return plan[:count]


def main():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    slot_label = ist_now.strftime("%d %b • %I:%M %p IST")
    seed = int(ist_now.strftime("%Y%m%d%H%M"))
    rng = random.Random(seed)

    theme = rng.choice(STUDIO_THEMES)
    banner_assets = load_banner_assets()
    news_items = fetch_news_items()
    if not news_items:
        raise RuntimeError("No valid news items returned by NEWS_FEED_URL")

    # Pre-download story images so 0s hook and individual story cards use real photos
    downloaded_imgs = []
    for idx, it in enumerate(news_items):
        im = download_story_image(it.get("image", ""), f"story_img_{idx:02d}.bin")
        downloaded_imgs.append(im)

    slots = build_voice_plan(len(news_items) + 2, rng)

    open_seg = {
        "kind": "open",
        "spoken": rng.choice(OPENING_LINES),
        "slot": slots[0],
        "base_rate": 13,
    }

    news_segs = []
    for idx, item in enumerate(news_items):
        news_segs.append({
            "kind": "news",
            "index": idx + 1,
            "total": len(news_items),
            "badge": f"NEWS {idx + 1:02d} / {len(news_items):02d}",
            "title": item["title"],
            "spoken": item["title"],
            "slot": slots[idx + 1],
            "base_rate": 18,
            "story_img": downloaded_imgs[idx],
            "use_right": (idx % 2 == 1),
        })

    close_seg = {
        "kind": "close",
        "badge": "BULLETIN SIGN-OFF",
        "title": "THANK YOU FOR WATCHING BRAHMINS TV",
        "sub": "brahminstv.com  •  7 AM • 12 PM • 5 PM • 10 PM IST",
        "spoken": rng.choice(CLOSING_LINES),
        "slot": slots[-1],
        "base_rate": 13,
    }

    reserve_closing = 7.5
    voice_clips = []
    running = 0.0

    # Opening hook clip
    o_mp3 = "voice_open.mp3"
    asyncio.run(synth_tts(open_seg["spoken"], open_seg["slot"], o_mp3, base_rate=open_seg["base_rate"]))
    o_dur = get_audio_duration(o_mp3)
    voice_clips.append((open_seg, o_mp3, o_dur))
    running += o_dur

    for i, seg in enumerate(news_segs):
        if running + reserve_closing >= MAX_TOTAL_SEC:
            break
        mp3_path = f"voice_{i:02d}.mp3"
        asyncio.run(synth_tts(seg["spoken"], seg["slot"], mp3_path, base_rate=seg["base_rate"]))
        dur = get_audio_duration(mp3_path)
        if running + dur + reserve_closing > MAX_TOTAL_SEC:
            break
        voice_clips.append((seg, mp3_path, dur))
        running += dur

    c_mp3 = "voice_close.mp3"
    asyncio.run(synth_tts(close_seg["spoken"], close_seg["slot"], c_mp3, base_rate=close_seg["base_rate"]))
    c_dur = min(get_audio_duration(c_mp3), max(2.5, MAX_TOTAL_SEC - running))
    voice_clips.append((close_seg, c_mp3, c_dur))

    segment_videos = []
    for idx, (seg, mp3_path, dur) in enumerate(voice_clips):
        seg_mp4 = f"seg_{idx:02d}.mp4"
        if seg["kind"] == "open":
            hook_png = "hook_00.png"
            preview_pool = [im for im in downloaded_imgs if im is not None]
            render_opening_hook(hook_png, theme, banner_assets, slot_label, news_items[0]["title"], preview_pool)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    hook_png,
                    "-i",
                    mp3_path,
                    "-c:v",
                    "libx264",
                    "-tune",
                    "stillimage",
                    "-preset",
                    "veryfast",
                    "-r",
                    str(FPS),
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-t",
                    f"{dur:.2f}",
                    "-shortest",
                    seg_mp4,
                ],
                check=True,
            )
        elif seg["kind"] == "news":
            # Render clean start and end frames (no overlapping multi-frame text jumps)
            k0, k1 = f"k0_{idx:02d}.png", f"k1_{idx:02d}.png"
            render_story_frame(
                k0,
                theme,
                banner_assets,
                slot_label,
                seg["badge"],
                seg["title"],
                seg["story_img"],
                seg["use_right"],
                0.0,
            )
            render_story_frame(
                k1,
                theme,
                banner_assets,
                slot_label,
                seg["badge"],
                seg["title"],
                seg["story_img"],
                seg["use_right"],
                1.0,
            )
            half = max(0.35, dur / 2.0)
            xf_offset = max(0.1, half - 0.15)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-t",
                    f"{half + 0.25:.2f}",
                    "-i",
                    k0,
                    "-loop",
                    "1",
                    "-t",
                    f"{half + 0.25:.2f}",
                    "-i",
                    k1,
                    "-i",
                    mp3_path,
                    "-filter_complex",
                    f"[0:v]fps={FPS},format=yuv420p[v0];"
                    f"[1:v]fps={FPS},format=yuv420p[v1];"
                    f"[v0][v1]xfade=transition=fade:duration=0.25:offset={xf_offset:.2f},trim=duration={dur:.2f},setpts=PTS-STARTPTS[vout]",
                    "-map",
                    "[vout]",
                    "-map",
                    "2:a",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-shortest",
                    seg_mp4,
                ],
                check=True,
            )
        else:
            close_png = "close_slate.png"
            render_closing_slate(close_png, theme, banner_assets, slot_label, seg["badge"], seg["title"], seg["sub"])
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    close_png,
                    "-i",
                    mp3_path,
                    "-c:v",
                    "libx264",
                    "-tune",
                    "stillimage",
                    "-preset",
                    "veryfast",
                    "-r",
                    str(FPS),
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-t",
                    f"{dur:.2f}",
                    "-shortest",
                    seg_mp4,
                ],
                check=True,
            )
        segment_videos.append(seg_mp4)

    with open("list.txt", "w", encoding="utf-8") as f:
        for vid in segment_videos:
            f.write(f"file '{vid}'\n")

    voice_only_mp4 = "voice_only.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", voice_only_mp4],
        check=True,
    )

    total_dur = get_audio_duration(voice_only_mp4)
    music_wav = "music_bed.wav"
    generate_music_bed(total_dur, music_wav, seed)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            voice_only_mp4,
            "-i",
            music_wav,
            "-filter_complex",
            "[1:a]volume=0.45[mus];[0:a][mus]amix=inputs=2:duration=first:weights=1.0 0.45[aout]",
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "top25-latest.mp4",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
