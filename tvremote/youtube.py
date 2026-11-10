"""Find YouTube videos on the internet (not on the TV), without an API key.

The TV's YouTube app can only be driven blind: type something into its search
box and take the first result. That is only as good as what gets typed, so the
hub first settles on one concrete video here (id, title, channel, length, live
or not) and then types that video's id on the TV, which pins the first result.

search() reads the public results page. YouTube can change that page at any
time; when it does, search() raises YouTubeError and callers fall back to
typing the plain words on the TV and say so.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
DATA_MARKERS = ("var ytInitialData = ", 'window["ytInitialData"] = ', "ytInitialData = ")

# Result-page filters (the `sp` parameter): videos only, videos over 20 minutes, live now.
FILTERS = {"auto": "EgIQAQ==", "long": "EgQQARgC", "live": "EgJAAQ=="}
PREFER = tuple(FILTERS)
LONG_SECONDS = 30 * 60


class YouTubeError(Exception):
    """YouTube could not be searched (offline, blocked, or the page changed)."""


def extract_video_id(text: str) -> str | None:
    """The 11-character id from a URL or a bare id; None for anything else."""
    text = text.strip()
    if VIDEO_ID_RE.match(text) and re.search(r"[0-9_-]|[a-z][A-Z]|[A-Z][a-z]", text):
        return text
    m = URL_ID_RE.search(text)
    return m.group(1) if m else None


def _get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI"})  # skips the EU consent page
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (OSError, ValueError) as e:
        raise YouTubeError(f"YouTube did not answer: {e}") from e


def _text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    return "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict))


def to_seconds(length: str | None) -> int | None:
    """"3:02:11" -> 10931."""
    if not length or not re.fullmatch(r"\d+(:\d{1,2}){0,2}", length.strip()):
        return None
    total = 0
    for part in length.strip().split(":"):
        total = total * 60 + int(part)
    return total


def _collect(node: Any, out: list[dict[str, Any]]) -> None:
    """Every videoRenderer in the page data, in page order."""
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            flags = json.dumps(vr.get("badges", [])) + json.dumps(vr.get("thumbnailOverlays", []))
            length = _text(vr.get("lengthText")) or None
            url = json.dumps(vr.get("navigationEndpoint", {}))
            if "/shorts/" not in url:
                out.append({
                    "id": vr["videoId"],
                    "title": _text(vr.get("title")),
                    "channel": _text(vr.get("ownerText") or vr.get("longBylineText")),
                    "length": length,
                    "seconds": to_seconds(length),
                    "live": not length and "LIVE" in flags,
                    "views": _text(vr.get("viewCountText") or vr.get("shortViewCountText")) or None,
                })
            return
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out)


def parse_results(page: str, limit: int = 10) -> list[dict[str, Any]]:
    """Videos on a results page; [] if the page holds no result data."""
    for marker in DATA_MARKERS:
        at = page.find(marker)
        if at < 0:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(page, at + len(marker))
        except ValueError:
            continue
        out: list[dict[str, Any]] = []
        _collect(data, out)
        seen: set[str] = set()
        return [v for v in out if not (v["id"] in seen or seen.add(v["id"]))][:limit]
    return []


def search(query: str, prefer: str = "auto", limit: int = 10, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Top videos for a query. prefer narrows the search itself: "long" to
    videos over 20 minutes, "live" to live streams. An empty narrowed search
    falls back to the plain one. Raises YouTubeError if nothing can be read."""
    query = " ".join(query.split())[:150]
    if not query:
        raise YouTubeError("empty search")
    base = {"search_query": query, "hl": "en", "gl": "US"}
    filters = [FILTERS.get(prefer, FILTERS["auto"])]
    if filters[0] != FILTERS["auto"]:
        filters.append(FILTERS["auto"])
    readable = False
    for sp in filters:
        page = _get("https://www.youtube.com/results?" + urllib.parse.urlencode({**base, "sp": sp}), timeout)
        readable = readable or any(m in page for m in DATA_MARKERS)
        results = parse_results(page, limit)
        if results:
            return results
    if not readable:
        raise YouTubeError("YouTube's results page could not be read (it may have changed)")
    return []


def suits(video: dict[str, Any], prefer: str) -> bool:
    if prefer == "live":
        return bool(video.get("live"))
    if prefer == "long":
        return bool(video.get("live")) or (video.get("seconds") or 0) >= LONG_SECONDS
    return True


def pick(results: list[dict[str, Any]], prefer: str = "auto",
         exclude: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """The first result (YouTube's own relevance order) that fits. If nothing
    fits the preference, the first result at all, rather than nothing."""
    fresh = [v for v in results if v["id"] not in exclude] or results
    for video in fresh:
        if suits(video, prefer):
            return video
    return fresh[0] if fresh else None


def lookup(video_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
    """Title and channel of one video (public oEmbed endpoint). None if unknown."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
    try:
        data = json.loads(_get(url, timeout))
    except (YouTubeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    return {"id": video_id, "title": str(data["title"]), "channel": str(data.get("author_name") or ""),
            "length": None, "seconds": None, "live": False, "views": None}
