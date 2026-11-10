"""Reading YouTube's pages (no network: the pages are built here)."""

import json

import pytest

from tvremote import youtube as yt


def renderer(vid, title, length=None, live=False, shorts=False, channel="Chan"):
    r = {"videoId": vid, "title": {"runs": [{"text": title}]}, "ownerText": {"runs": [{"text": channel}]},
         "viewCountText": {"simpleText": "1,234 views"},
         "navigationEndpoint": {"commandMetadata": {"webCommandMetadata": {"url": f"/shorts/{vid}" if shorts else f"/watch?v={vid}"}}}}
    if length:
        r["lengthText"] = {"simpleText": length}
    if live:
        r["badges"] = [{"metadataBadgeRenderer": {"label": "LIVE"}}]
    return {"videoRenderer": r}


def page(*renderers, marker="var ytInitialData = "):
    data = {"contents": {"sections": [{"items": list(renderers)}, {"ad": {"promoted": True}}]}}
    return f"<html><script>{marker}{json.dumps(data)};</script><script>var other = {{}};</script></html>"


def test_parse_results_reads_videos_in_page_order():
    html = page(renderer("aaaaaaaaaa1", "Jazz Mix", "3:02:11"), renderer("bbbbbbbbbb2", "Short", "0:30", shorts=True),
                renderer("ccccccccc_3", "Jazz Radio", live=True), renderer("aaaaaaaaaa1", "Jazz Mix again", "3:02:11"))
    out = yt.parse_results(html)
    assert [v["id"] for v in out] == ["aaaaaaaaaa1", "ccccccccc_3"]  # no Shorts, no repeats
    assert out[0] == {"id": "aaaaaaaaaa1", "title": "Jazz Mix", "channel": "Chan", "length": "3:02:11",
                      "seconds": 10931, "live": False, "views": "1,234 views"}
    assert out[1]["live"] is True and out[1]["length"] is None
    assert yt.parse_results(page(marker='window["ytInitialData"] = ')) == []
    assert yt.parse_results("<html>consent wall</html>") == []
    assert yt.parse_results("var ytInitialData = {broken") == []


def test_search_narrows_then_falls_back(monkeypatch):
    asked = []

    def get(url, timeout, cookies=True):
        asked.append(url)
        return page() if "sp=EgQQARgC" in url else page(renderer("aaaaaaaaaa1", "Any length", "2:00"))
    monkeypatch.setattr(yt, "_get", get)
    out = yt.search("  jazz   music ", "long")
    assert [v["id"] for v in out] == ["aaaaaaaaaa1"] and len(asked) == 2
    assert "search_query=jazz+music" in asked[0]


def test_search_tells_unreadable_from_empty(monkeypatch):
    monkeypatch.setattr(yt, "_get", lambda url, timeout, cookies=True: "<html>something else entirely</html>")
    with pytest.raises(yt.YouTubeError):
        yt.search("jazz")
    monkeypatch.setattr(yt, "_get", lambda url, timeout, cookies=True: page())
    assert yt.search("zzzz") == []
    with pytest.raises(yt.YouTubeError):
        yt.search("   ")


def test_pick_follows_the_preference_and_never_returns_nothing_needlessly():
    results = [{"id": "a", "seconds": 60, "live": False}, {"id": "b", "seconds": 7200, "live": False},
               {"id": "c", "seconds": None, "live": True}]
    assert yt.pick(results)["id"] == "a"
    assert yt.pick(results, "long")["id"] == "b"
    assert yt.pick(results, "live")["id"] == "c"
    assert yt.pick(results, "long", exclude={"b"})["id"] == "c"       # live counts as long-running
    assert yt.pick(results[:1], "live")["id"] == "a"                  # nothing live: best there is
    assert yt.pick(results[:1], "auto", exclude={"a"})["id"] == "a"   # everything excluded: still something
    assert yt.pick([]) is None


@pytest.mark.parametrize("raw,want", [
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/jfKfPfyJRdk", "jfKfPfyJRdk"),
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("Rachmaninov", None),      # eleven letters, but a word
    ("jazz music", None),
])
def test_extract_video_id(raw, want):
    assert yt.extract_video_id(raw) == want


def watch(details):
    return "<script>var ytInitialPlayerResponse = " + json.dumps({"videoDetails": details}) + ";var meta = 1;</script>"


def test_lookup_reads_the_watch_page_then_oembed(monkeypatch):
    pages = {"watch": watch({"videoId": "jfKfPfyJRdk", "title": "lofi radio", "author": "Lofi Girl",
                             "lengthSeconds": "0", "isLive": True})}

    def get(url, timeout, cookies=True):
        if "/watch" in url:
            return pages["watch"]
        assert cookies is False
        return json.dumps({"title": "From oEmbed", "author_name": "Someone"})
    monkeypatch.setattr(yt, "_get", get)
    assert yt.lookup("jfKfPfyJRdk") == {"id": "jfKfPfyJRdk", "title": "lofi radio", "channel": "Lofi Girl",
                                        "length": None, "seconds": None, "live": True, "views": None}
    pages["watch"] = watch({"videoId": "jfKfPfyJRdk", "title": "A film", "author": "Studio", "lengthSeconds": "3723"})
    assert yt.lookup("jfKfPfyJRdk")["length"] == "1:02:03"
    pages["watch"] = watch({"videoId": "otherVIDEO0", "title": "Not the one asked for"})
    assert yt.lookup("jfKfPfyJRdk")["title"] == "From oEmbed"
    assert yt.lookup("not an id") is None


def test_lookup_of_a_video_that_does_not_exist(monkeypatch):
    def get(url, timeout, cookies=True):
        if "/watch" in url:
            return "<html>Video unavailable</html>"
        raise yt.YouTubeError("404")
    monkeypatch.setattr(yt, "_get", get)
    assert yt.lookup("aaaaaaaaaa1") is None


def test_times():
    assert yt.to_seconds("3:02:11") == 10931 and yt.to_seconds("0:59") == 59 and yt.to_seconds("LIVE") is None
    assert yt.from_seconds(10931) == "3:02:11" and yt.from_seconds(59) == "0:59" and yt.from_seconds(None) is None
