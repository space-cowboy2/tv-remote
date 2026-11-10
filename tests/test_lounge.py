"""The read-only YouTube link's wire handling, against a stand-in server.

The stand-in is written from the same understanding of the link as the client,
so these tests show that the client is consistent, bounded and read-only: what
it sends, that it leaves, that it gives up in time. They cannot show that
YouTube's real link answers the same way; that was only ever tried on a real
TV with its owner watching (see docs/CONVERSATION.md)."""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tvremote import lounge


def frame(events, start=0):
    """Events in the link's framing: a length line, then a JSON list spread over lines."""
    body = "[" + "\n,".join(json.dumps([start + i, e]) for i, e in enumerate(events)) + "]\n"
    return f"{len(body)}\n{body}"


def test_chunks_spread_over_lines_and_several_in_a_row():
    text = frame([["c", "SID1", "", 8], ["S", "GS1"]]) + frame([["noop"]], 2) + "junk\n" + frame([["nowPlaying", {"videoId": "v"}]], 3)
    chunks = list(lounge.parse_chunks(iter(text.splitlines())))
    assert [len(c) for c in chunks] == [2, 1, 1]
    assert chunks[0][1] == [1, ["S", "GS1"]] and chunks[2][0][1][0] == "nowPlaying"
    with pytest.raises(lounge.LoungeError):
        list(lounge.parse_chunks(iter(["5"] + ["not json at all"] * 20_000)))


def test_events_fold_into_one_picture():
    st = lounge.Status()
    st.take([[0, ["c", "SID1", "", 8]], [1, ["S", "GS1"]], [2, ["loungeStatus", {"devices": "[]"}]]])
    assert (st.sid, st.gsession, st.last_id, st.heard) == ("SID1", "GS1", 2, False)
    st.take([[3, ["nowPlaying", {"videoId": "abc", "state": "3", "currentTime": "0", "duration": "100.5"}]]])
    assert st.as_dict() == {"video": "abc", "state": "loading", "position": 0.0, "duration": 100.5, "heard": True}
    assert not st.settled("abc")
    st.take([[4, ["onStateChange", {"state": "1", "currentTime": "4.26"}]]])
    assert st.state == "playing" and st.position == 4.3 and st.settled("abc") and st.settled(None) and not st.settled("other")
    st.take([[5, ["onStateChange", {"state": "2"}]]])
    assert st.state == "paused" and not st.settled(None)
    st.take([[6, ["onAdStateChange", {"adState": "1", "contentVideoId": "abc"}]]])
    assert st.state == "ad" and st.settled("abc")
    st.take([[7, ["onStateChange", {"state": "77"}]]])
    assert st.state == "unknown (77)"
    st.take([[8, ["nowPlaying", {}]]])                       # nothing loaded
    assert st.video is None and st.state == "idle"
    st.take([[9, ["loungeScreenDisconnected"]], "garbage", [10, []], [11, ["onStateChange", "not a dict"]]])
    assert st.gone


class Link:
    """A stand-in for YouTube's link. `script` is what each listen (GET) answers:
    a list of (seconds to wait, events) pairs, or "hold" to stay silent."""

    def __init__(self, script, token="TOKEN-1", bind_events=()):
        self.script, self.token, self.bind_events = list(script), token, list(bind_events)
        self.seen: list[dict] = []
        link = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _note(self, form):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                link.seen.append({"method": self.command, "path": urllib.parse.urlsplit(self.path).path, "query": q,
                                  "form": form, "token": self.headers.get("X-YouTube-LoungeId-Token")})
                return q

            def _send(self, code, text=""):
                body = text.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
                q = self._note(form)
                if self.path.startswith("/pairing/get_lounge_token_batch"):
                    if link.token is None:
                        return self._send(200, json.dumps({"screens": []}))
                    if link.token == "refuse":
                        return self._send(401)
                    return self._send(200, json.dumps({"screens": [{"screenId": form.get("screen_ids"), "loungeToken": link.token}]}))
                if "SID" not in q:   # the join
                    return self._send(200, frame([["c", "SID1", "", 8], ["S", "GS1"], *link.bind_events]))
                self._send(200, frame([["noop"]]))

            def do_GET(self):  # noqa: N802
                self._note({})
                step = link.script.pop(0) if link.script else "hold"
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(text):
                    data = text.encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                try:
                    if step == "hold":
                        time.sleep(3)
                    else:
                        n = 10
                        for wait, events in step:
                            time.sleep(wait)
                            chunk(frame(events, n))
                            n += len(events)
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def commands(self):
        return [r["form"].get("req0__sc") or r["form"].get("TYPE") for r in self.seen if r["method"] == "POST" and "SID" in r["query"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def link(monkeypatch):
    made = []

    def make(script, **kw):
        ln = Link(script, **kw)
        made.append(ln)
        monkeypatch.setattr(lounge, "API", ln.url)
        return ln
    yield make
    for ln in made:
        ln.close()


PLAYING = [["nowPlaying", {"videoId": "Dx5qFachd3A", "state": "3", "currentTime": "0"}],
           ["onStateChange", {"state": "1", "currentTime": "2.0"}]]


def test_joins_listens_and_leaves_without_sending_a_control(link):
    ln = link([[(0.05, PLAYING)]])
    t0 = time.monotonic()
    now = lounge.watch_blocking("screen-1", "Dx5qFachd3A", seconds=5)
    assert now == {"video": "Dx5qFachd3A", "state": "playing", "position": 2.0, "duration": None, "heard": True}
    assert time.monotonic() - t0 < 2                      # back as soon as the answer was in
    token, join, listen, leave = ln.seen
    assert token["path"] == "/pairing/get_lounge_token_batch" and token["form"] == {"screen_ids": "screen-1"}
    assert join["form"]["device"] == "REMOTE_CONTROL" and join["form"]["loungeIdToken"] == "TOKEN-1"
    assert "method" not in join["form"] and not any(k.startswith("req") or k in ("videoId", "listId") for k in join["form"])
    assert listen["method"] == "GET" and listen["query"]["SID"] == "SID1" and listen["query"]["gsessionid"] == "GS1"
    assert leave["form"]["TYPE"] == "terminate" and leave["token"] == "TOKEN-1"
    assert ln.commands() == ["terminate"]                 # nothing else was ever sent to the app


def test_an_app_that_says_nothing_is_asked_once_to_repeat_its_state(link):
    ln = link([[], [(0.05, PLAYING)]])
    now = lounge.watch_blocking("screen-1", None, seconds=6)
    assert now["state"] == "playing" and now["video"] == "Dx5qFachd3A"
    assert ln.commands() == ["getNowPlaying", "terminate"]
    rids = [int(r["query"]["RID"]) for r in ln.seen if r["method"] == "POST" and "RID" in r["query"]]
    assert rids == sorted(rids) and len(set(rids)) == len(rids)


def test_waits_for_the_wanted_video_and_reports_the_last_state_at_the_deadline(link):
    other = [["nowPlaying", {"videoId": "OTHERvideo1", "state": "1", "currentTime": "50"}]]
    link([[(0.05, other), (0.3, PLAYING)]])
    assert lounge.watch_blocking("s", "Dx5qFachd3A", seconds=5)["video"] == "Dx5qFachd3A"
    ln = link([[(0.05, other)], "hold"])
    t0 = time.monotonic()
    now = lounge.watch_blocking("s", "Dx5qFachd3A", seconds=1.5)
    assert now["video"] == "OTHERvideo1" and now["state"] == "playing"
    assert 1.0 < time.monotonic() - t0 < 4 and ln.commands()[-1] == "terminate"


def test_silence_ends_at_the_deadline_and_still_leaves(link):
    ln = link(["hold", "hold", "hold"])
    t0 = time.monotonic()
    now = lounge.watch_blocking("s", None, seconds=1.2)
    assert now["heard"] is False and now["state"] == "unknown" and time.monotonic() - t0 < 4
    assert ln.commands()[-1] == "terminate"


def test_state_announced_at_the_join_is_used(link):
    ln = link([], bind_events=PLAYING)
    assert lounge.watch_blocking("s", "Dx5qFachd3A", seconds=3)["state"] == "playing"
    assert [r["method"] for r in ln.seen] == ["POST", "POST", "POST"]   # token, join, leave: no listen was needed


def test_a_link_that_refuses_is_an_error_not_a_guess(link):
    link([], token=None)
    with pytest.raises(lounge.LoungeError, match="no token"):
        lounge.watch_blocking("s", None, seconds=2)
    link([], token="refuse")
    with pytest.raises(lounge.LoungeError, match="answered 401"):
        lounge.watch_blocking("s", None, seconds=2)


def test_an_unreachable_link_is_an_error(monkeypatch):
    monkeypatch.setattr(lounge, "API", "http://127.0.0.1:9")
    with pytest.raises(lounge.LoungeError, match="did not answer"):
        lounge.watch_blocking("s", None, seconds=2)
