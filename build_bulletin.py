import os
import asyncio
import subprocess
import xml.etree.ElementTree as ET
import requests
import edge_tts

FEED_URL = os.environ["NEWS_FEED_URL"]
VOICE = "en-IN-NeerjaNeural"
MAX_DURATION = 162  # Capped under 2m45s (165s)

async def generate_audio(script_text, out_mp3):
    communicate = edge_tts.Communicate(script_text, VOICE, rate="+8%")
    await communicate.save(out_mp3)

def fetch_headlines():
    resp = requests.get(FEED_URL, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    headlines = []

    if text.startswith("{") or text.startswith("["):
        data = resp.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        for idx, item in enumerate(items[:25], start=1):
            title = (item.get("title") or "").strip()
            if title:
                headlines.append(f"Number {idx}. {title}.")
    else:
        root = ET.fromstring(text)
        rss_items = root.findall(".//item") or root.findall(".//{http://purl.org/rss/1.0/}item")
        for idx, item in enumerate(rss_items[:25], start=1):
            title_el = item.find("title")
            if title_el is None:
                title_el = item.find("{http://purl.org/rss/1.0/}title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            if title:
                headlines.append(f"Number {idx}. {title}.")

    return headlines

def main():
    headlines = fetch_headlines()
    if not headlines:
        raise RuntimeError("No headlines returned from NEWS_FEED_URL")

    narration = (
        "Welcome to Brahmins TV Top 25 News Shots. "
        + " ".join(headlines)
        + " For complete stories, visit brahminstv dot com."
    )

    asyncio.run(generate_audio(narration, "voice.mp3"))

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x0b132b:s=1080x1920:r=25",
        "-i", "voice.mp3",
        "-vf", (
            "drawbox=x=0:y=0:w=1080:h=220:color=0xb91c1c@0.95:t=fill,"
            "drawtext=text='BRAHMINS TV | TOP 25':fontcolor=white:fontsize=54:x=(w-text_w)/2:y=85,"
            "drawtext=text='brahminstv.com':fontcolor=0xfacc15:fontsize=38:x=(w-text_w)/2:y=1780"
        ),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(MAX_DURATION),
        "-shortest",
        "top25-latest.mp4"
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
