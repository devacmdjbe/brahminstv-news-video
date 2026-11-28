import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH_VERSION = "v27.0"
MP4_PATH = os.getenv("OUTPUT_MP4_PATH", "top25-latest.mp4")
USED_JSON_PATH = os.getenv("USED_JSON_PATH", "used_bulletins.json")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
RELEASE_TAG = os.getenv("RELEASE_TAG", "latest-bulletin")

BTV_BULLETIN_FOOTER = "\n".join([
    "━━━━━━━━━━━━━━━━━━━━",
    "",
    "🕉️ https://BrahminsTV.com – PART OF THE SAIDEV GROUP ECOSYSTEM (SDGC) • Media • Politics • Business • Technology • Employment • Education • Charity 🌐 Explore entire SDGC ecosystem at FundersPro.in",
    "",
    "FREE USE / RE-UPLOAD: Feel free to download & re-upload this video on any platform, with the Brahmins TV logo & credit retained.",
    "",
    "Brahmins TV – Unbowed. Unafraid.",
    "",
    "#BreakingNews #IndiaNews #TamilNadu #TopNews #BrahminsTV #NewsBulletin"
])


def ist_slot_label():
    ist_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    hr = ist_now.hour
    date_str = ist_now.strftime("%d %b %Y")
    if hr < 10:
        slot = "7 AM Morning"
    elif hr < 15:
        slot = "12 PM Afternoon"
    elif hr < 20:
        slot = "5 PM Evening"
    else:
        slot = "10 PM Night"
    return f"{slot} Bulletin ({date_str})"


def extract_aired_headlines():
    if not os.path.exists(USED_JSON_PATH):
        return []
    try:
        with open(USED_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"Warning: could not parse {USED_JSON_PATH}: {exc}")
        return []

    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("latest", "stories", "items", "articles", "used", "bulletins"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if not items:
            vals = list(data.values())
            if vals and isinstance(vals[-1], dict) and isinstance(vals[-1].get("stories"), list):
                items = vals[-1]["stories"]
            else:
                items = [v for v in vals if isinstance(v, dict)]

    titles = []
    for entry in items:
        if isinstance(entry, str):
            t = entry.strip()
        elif isinstance(entry, dict):
            t = str(entry.get("title") or entry.get("headline") or "").strip()
        else:
            t = ""
        if t and t not in titles:
            titles.append(t)
    return titles[:25]


def build_viral_copy():
    slot = ist_slot_label()
    headlines = extract_aired_headlines()

    lead = headlines[0] if len(headlines) >= 1 else "Top National, Tamil Nadu & Global Headlines"
    second = headlines[1] if len(headlines) >= 2 else ""
    third = headlines[2] if len(headlines) >= 3 else ""

    if second:
        viral_title = f"🚨 {lead} | {second} — Brahmins TV {slot}"
    else:
        viral_title = f"🚨 {lead} — Brahmins TV Top 25 {slot}"
    viral_title = viral_title[:220]

    if len(headlines) >= 3:
        lead_para = (
            f"WATCH TOP 25 NEWS SHOTS: {lead}, {second}, and {third} headline "
            f"Brahmins TV's fast-paced {slot} roundup."
        )
    else:
        lead_para = f"WATCH TOP 25 NEWS SHOTS: {lead} leads Brahmins TV's {slot} roundup."

    highlights = headlines[3:10]
    bullet_block = ""
    if highlights:
        bullet_lines = "\n".join(f"• {h}" for h in highlights)
        bullet_block = f"\n\nKey Headlines in This Bulletin:\n{bullet_lines}"

    watch_url = "https://www.brahminstv.com"
    full_caption = (
        f"{viral_title}\n\n"
        f"{lead_para}{bullet_block}\n\n"
        f"Watch live & read full news summaries: {watch_url}\n\n"
        f"{BTV_BULLETIN_FOOTER}"
    )
    return viral_title, full_caption


def http_json(url, method="GET", headers=None, payload=None, timeout=60):
    req_headers = dict(headers or {})
    body_bytes = None
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {err.code} on {url}: {raw[:400]}")


def escape_linkedin_little_text(text):
    out = text.replace("━", "-")
    for ch in ["\\", "|", "{", "}", "@", "[", "]", "(", ")", "<", ">", "*", "_", "~"]:
        out = out.replace(ch, "\\" + ch)
    return out[:2900]


def publish_facebook_video(viral_title, full_caption):
    page_id = os.getenv("FACEBOOK_PAGE_ID", "").strip()
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not token:
        print("Facebook skipped: missing FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN")
        return

    file_size = os.path.getsize(MP4_PATH)
    start_url = f"https://graph-video.facebook.com/{GRAPH_VERSION}/{page_id}/videos"
    _, start_data = http_json(start_url, method="POST", payload={
        "upload_phase": "start",
        "file_size": file_size,
        "access_token": token,
    })
    upload_session_id = start_data["upload_session_id"]
    start_offset = int(start_data["start_offset"])
    end_offset = int(start_data["end_offset"])

    with open(MP4_PATH, "rb") as f:
        while start_offset < file_size:
            f.seek(start_offset)
            chunk = f.read(end_offset - start_offset)
            boundary = f"----BTVBoundary{int(time.time() * 1000)}"
            parts = []
            for k, v in [
                ("access_token", token),
                ("upload_phase", "transfer"),
                ("upload_session_id", str(upload_session_id)),
                ("start_offset", str(start_offset)),
            ]:
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
                )
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"video_file_chunk\"; filename=\"bulletin.mp4\"\r\nContent-Type: video/mp4\r\n\r\n".encode("utf-8")
                + chunk
                + f"\r\n--{boundary}--\r\n".encode("utf-8")
            )
            body = b"".join(parts)
            req = urllib.request.Request(
                start_url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                xfer = json.loads(resp.read().decode("utf-8"))
            start_offset = int(xfer["start_offset"])
            end_offset = int(xfer["end_offset"])

    _, finish_data = http_json(start_url, method="POST", payload={
        "upload_phase": "finish",
        "upload_session_id": upload_session_id,
        "title": viral_title,
        "description": full_caption,
        "published": True,
        "access_token": token,
    })
    print("Facebook Video published:", finish_data)


def publish_instagram_reel(full_caption):
    ig_user_id = os.getenv("INSTAGRAM_USER_ID", "").strip()
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not ig_user_id or not token or not GITHUB_REPOSITORY:
        print("Instagram skipped: missing INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN, or GITHUB_REPOSITORY")
        return

    video_url = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{RELEASE_TAG}/{os.path.basename(MP4_PATH)}"
    create_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media"
    _, container = http_json(create_url, method="POST", payload={
        "media_type": "REELS",
        "video_url": video_url,
        "caption": full_caption[:2150],
        "share_to_feed": True,
        "access_token": token,
    })
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError(f"Instagram did not return creation_id: {container}")

    state = "IN_PROGRESS"
    for _ in range(30):
        time.sleep(5)
        status_url = (
            f"https://graph.facebook.com/{GRAPH_VERSION}/{creation_id}"
            f"?fields=status_code,status&access_token={urllib.parse.quote(token)}"
        )
        _, st = http_json(status_url)
        state = st.get("status_code", state)
        if state == "FINISHED":
            break
        if state in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram Reel processing failed: {st}")

    if state != "FINISHED":
        raise RuntimeError("Instagram Reel processing timed out.")

    pub_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media_publish"
    _, published = http_json(pub_url, method="POST", payload={
        "creation_id": creation_id,
        "access_token": token,
    })
    print("Instagram Reel published:", published)


def publish_linkedin_video(viral_title, full_caption):
    token = os.getenv("LINKEDIN_ACCESS_TOKEN", "").strip()
    org_urn = os.getenv("LINKEDIN_ORG_URN", "").strip()
    li_version = os.getenv("LINKEDIN_VERSION", "").strip()
    if not token or not org_urn or not li_version:
        print("LinkedIn skipped: missing LINKEDIN_ACCESS_TOKEN, LINKEDIN_ORG_URN, or LINKEDIN_VERSION")
        return

    file_size = os.path.getsize(MP4_PATH)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": li_version,
    }
    _, init_res = http_json(
        "https://api.linkedin.com/rest/videos?action=initializeUpload",
        method="POST",
        headers=headers,
        payload={
            "initializeUploadRequest": {
                "owner": org_urn,
                "fileSizeBytes": file_size,
                "uploadCaptions": False,
                "uploadThumbnail": False,
            }
        },
    )
    val = init_res.get("value", {})
    video_urn = val.get("video")
    instructions = val.get("uploadInstructions", [])
    upload_token = val.get("uploadToken", "")
    if not video_urn or not instructions:
        raise RuntimeError(f"LinkedIn initializeUpload unexpected response: {init_res}")

    etags = []
    with open(MP4_PATH, "rb") as f:
        for part in instructions:
            first_b = int(part["firstByte"])
            last_b = int(part["lastByte"])
            f.seek(first_b)
            chunk = f.read(last_b - first_b + 1)
            put_req = urllib.request.Request(
                part["uploadUrl"],
                data=chunk,
                headers={"Content-Type": "application/octet-stream"},
                method="PUT",
            )
            with urllib.request.urlopen(put_req, timeout=180) as put_resp:
                etag = put_resp.headers.get("ETag", "").strip('"')
                if etag:
                    etags.append(etag)

    http_json(
        "https://api.linkedin.com/rest/videos?action=finalizeUpload",
        method="POST",
        headers=headers,
        payload={
            "finalizeUploadRequest": {
                "video": video_urn,
                "uploadToken": upload_token,
                "uploadedPartIds": etags,
            }
        },
    )

    for _ in range(18):
        time.sleep(5)
        encoded_urn = urllib.parse.quote(video_urn, safe="")
        _, vid_status = http_json(
            f"https://api.linkedin.com/rest/videos/{encoded_urn}",
            headers=headers,
        )
        if vid_status.get("status") == "AVAILABLE":
            break
        if vid_status.get("status") == "PROCESSING_FAILED":
            raise RuntimeError(f"LinkedIn video processing failed: {vid_status}")

    _, post_res = http_json(
        "https://api.linkedin.com/rest/posts",
        method="POST",
        headers=headers,
        payload={
            "author": org_urn,
            "commentary": escape_linkedin_little_text(full_caption),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {
                "media": {
                    "title": viral_title[:200],
                    "id": video_urn,
                }
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
    )
    print("LinkedIn Video published:", post_res)


def main():
    if not os.path.exists(MP4_PATH):
        raise FileNotFoundError(f"Bulletin MP4 not found at {MP4_PATH}")

    viral_title, full_caption = build_viral_copy()
    print("=== VIRAL TITLE ===")
    print(viral_title)
    print("=== CAPTION PREVIEW ===")
    print(full_caption)

    errors = []
    for name, fn in [
        ("Facebook", lambda: publish_facebook_video(viral_title, full_caption)),
        ("Instagram", lambda: publish_instagram_reel(full_caption)),
        ("LinkedIn", lambda: publish_linkedin_video(viral_title, full_caption)),
    ]:
        try:
            fn()
        except Exception as exc:
            print(f"{name} publish failed: {exc}")
            errors.append(f"{name}: {exc}")

    if len(errors) == 3:
        raise RuntimeError("All social bulletin publishes failed: " + " | ".join(errors))


if __name__ == "__main__":
    main()
