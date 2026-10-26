"""YouTube helpers: pull a video id out of whatever people paste, and optional search."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Optional

from .store import Error

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def video_id(value: Optional[str]) -> Optional[str]:
    """Accept a bare id or any of the usual YouTube URL shapes. Empty in, None out."""
    value = (value or "").strip()
    if not value:
        return None
    if VIDEO_ID.match(value):
        return value
    url = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    host = (url.hostname or "").lower()
    candidate = None
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com" \
            or host.endswith(".youtube-nocookie.com"):
        if url.path == "/watch":
            candidate = urllib.parse.parse_qs(url.query).get("v", [""])[0]
        else:
            parts = url.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live", "v"):
                candidate = parts[1]
    if candidate and VIDEO_ID.match(candidate):
        return candidate
    raise Error("That doesn't look like a YouTube link")


def search(api_key: str, query: str, suffix: str = "karaoke", limit: int = 8) -> list:
    query = " ".join((query or "").split())[:100]
    if not query:
        return []
    params = urllib.parse.urlencode({
        "part": "snippet", "type": "video", "videoEmbeddable": "true", "maxResults": limit,
        "q": f"{query} {suffix}".strip(), "key": api_key,
    })
    try:
        with urllib.request.urlopen(f"{SEARCH_URL}?{params}", timeout=8) as res:
            data = json.load(res)
    except Exception:
        # Never echo the upstream error: the request URL carries the API key.
        raise Error("YouTube search isn't working right now", 502)
    return [{
        "video_id": item["id"]["videoId"],
        "title": item["snippet"]["title"],
        "channel": item["snippet"]["channelTitle"],
        "thumbnail": item["snippet"]["thumbnails"].get("default", {}).get("url"),
    } for item in data.get("items", []) if item.get("id", {}).get("videoId")]
