"""Live sockets per room, and the screens (displays) connected to each room.

A screen only counts once the host has let it in: it says hello with the host's token (the
host page's own sound screen, or a screen opened on the host's device), or with the key the
host gave it the first time. Anything else waits, showing a code, until the host lets it in.
Waiting screens get no commands, can't steer the other screens and can't save recordings.

Connected displays live in memory. A screen keeps its own settings in its browser and sends
them again when it reconnects; what the host decides (its key, whether it records) is saved.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import Callable, Dict, Optional, Set

from fastapi import WebSocket

from .store import Error, Store

LAYOUTS = ("split", "video", "queue", "splash", "confidence")
ACTIONS = ("play", "pause", "restart", "identify", "fullscreen", "speakers")
# Can the screen pick its speakers: no (browser or plain http), not yet (needs a tap), yes.
AUDIO = ("unsupported", "locked", "ready")
STATES = ("idle", "playing", "paused", "buffering", "ended", "error")
MICS = ("unsupported", "off", "on", "blocked")
DEFAULTS = {"name": "Screen", "layout": "split", "volume": 100, "muted": False, "sink": "",
            "mic": False, "mic_volume": 80, "reverb": 25, "record": False}
MAX_DISPLAYS = 12
MAX_WAITING = 12  # screens waiting for the host at once, per room
WAIT_SECONDS = 600  # a screen the host hasn't answered by then gives up its place and asks again
CODE_DIGITS = 6  # in the code a waiting screen shows
MAX_SOCKETS = 300  # ponytail: per room; a big party is well under this. Per address caps live in app.py.
MAX_OUTPUTS = 20
SEND_TIMEOUT = 5  # seconds a socket gets to take a message before it's dropped
MAX_TEXT = 200
_ID = re.compile(r"^[A-Za-z0-9-]{8,40}$")


def _name(value, length: int = 32, blank: str = "Screen") -> str:
    value = " ".join(str(value or "").split())[:length]
    return value or blank


def settings(raw: dict, current: Optional[dict] = None) -> dict:
    """Validate display settings, filling gaps from `current` (or the defaults)."""
    out = dict(current or DEFAULTS)
    if raw.get("name") is not None:
        out["name"] = _name(raw["name"])
    if raw.get("layout") is not None:
        if raw["layout"] not in LAYOUTS:
            raise Error("Unknown layout. Pick one of: " + ", ".join(LAYOUTS))
        out["layout"] = raw["layout"]
    for key, label in (("volume", "Volume"), ("mic_volume", "Mic volume"), ("reverb", "Reverb")):
        if raw.get(key) is not None:
            try:
                out[key] = max(0, min(100, int(raw[key])))
            except (TypeError, ValueError):
                raise Error(label + " must be a number from 0 to 100")
    for key in ("muted", "mic", "record"):
        if raw.get(key) is not None:
            out[key] = bool(raw[key])
    if raw.get("sink") is not None:
        # An audio output id the screen reported; "" means the device default.
        if not isinstance(raw["sink"], str) or len(raw["sink"]) > MAX_TEXT:
            raise Error("Speaker must be one of the outputs the screen reported")
        out["sink"] = raw["sink"]
    return out


def outputs(raw) -> list:
    """Validate the audio outputs a screen says it has. Blank labels get a readable name."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_OUTPUTS]:
        if not isinstance(item, dict):
            continue
        output_id, label = item.get("id"), item.get("label", "")
        if not isinstance(output_id, str) or not isinstance(label, str) or len(output_id) > MAX_TEXT:
            continue
        label = " ".join(label.split())[:MAX_TEXT] or f"Speaker {len(out) + 1}"
        out.append({"id": output_id, "label": label})
    return out


class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.rooms: Dict[str, Set[WebSocket]] = {}
        # room code -> display id -> {"ws", "settings", "status", ...}
        self.displays: Dict[str, Dict[str, dict]] = {}
        # room code -> waiting id -> {"ws", "display", "name", "code", "info"}, oldest first
        self.waiting: Dict[str, Dict[str, dict]] = {}
        # room code -> (display id, song id) pairs already saved, so each screen saves a song once
        self.uploaded: Dict[str, Set[tuple]] = {}

    async def send(self, code: str, message: dict) -> None:
        # All at once, so one slow phone doesn't hold up everyone else's update.
        sockets = self.rooms.get(code.upper(), set())
        targets = list(sockets)
        sent = await asyncio.gather(*(_try_send(ws, message) for ws in targets))
        for ws, ok in zip(targets, sent):
            if not ok:
                sockets.discard(ws)

    async def notify(self, code: str) -> None:
        await self.send(code, {"type": "update"})

    def connect(self, code: str, ws: WebSocket) -> bool:
        sockets = self.rooms.setdefault(code.upper(), set())
        if len(sockets) >= MAX_SOCKETS:
            return False
        sockets.add(ws)
        return True

    async def disconnect(self, code: str, ws: WebSocket) -> None:
        code = code.upper()
        self.rooms.get(code, set()).discard(ws)
        room = self.displays.get(code, {})
        for display_id, display in list(room.items()):
            if display["ws"] is ws:
                del room[display_id]
                await self.send(code, {"type": "displays"})
        if self._unwait(code, ws):
            await self.send(code, {"type": "waiting"})

    def _unwait(self, code: str, ws: WebSocket) -> bool:
        waiting = self.waiting.get(code, {})
        gone = [wid for wid, w in waiting.items() if w["ws"] is ws]
        for wid in gone:
            del waiting[wid]
        return bool(gone)

    async def _expire(self, code: str) -> None:
        waiting = self.waiting.get(code, {})
        old = [wid for wid, w in waiting.items() if w["since"] <= time.monotonic() - WAIT_SECONDS]
        for wid in old:
            await _try_send(waiting.pop(wid)["ws"], {"type": "error", "waiting": True, "retry": True,
                                                     "detail": "The host didn't answer in time."})

    async def _hello(self, code: str, ws: WebSocket, message: dict, address: str,
                     may_wait: Callable[[], bool], per_address: int) -> None:
        info = message.get("display") or {}
        if not isinstance(info, dict):
            return
        display_id = str(info.get("id", ""))
        if not _ID.match(display_id):
            return
        is_host = self.store.is_host(code, message.get("host") if isinstance(message.get("host"), str) else None)
        key = message.get("key") if isinstance(message.get("key"), str) else None
        if is_host or self.store.screen(code, display_id, key) is not None:
            await self._admit(code, ws, display_id, info)
            return
        # Not let in yet: wait, showing a code the host matches up before letting it in. A screen
        # saying hello again keeps its place and its code. A new one never pushes out a screen
        # that's already waiting: when there's no room, or this address already has screens
        # waiting or keeps asking, the new one is refused. Nobody holds a place forever, though.
        await self._expire(code)
        waiting = self.waiting.setdefault(code, {})
        old = next((w for w in waiting.values() if w["ws"] is ws), None)
        if old:
            old.update(display=display_id, name=_name(info.get("name")), info=info)
        else:
            mine = sum(1 for w in waiting.values() if w["address"] == address)
            if len(waiting) >= MAX_WAITING or mine >= per_address or not may_wait():
                await _try_send(ws, {"type": "error", "waiting": True, "retry": True,
                                     "detail": "Too many screens are asking to join right now."})
                return
            old = {"ws": ws, "display": display_id, "name": _name(info.get("name")),
                   "code": f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}", "info": info, "address": address,
                   "since": time.monotonic()}
            waiting[secrets.token_hex(8)] = old
        await ws.send_json({"type": "waiting", "code": old["code"]})
        await self.send(code, {"type": "waiting"})

    async def _admit(self, code: str, ws: WebSocket, display_id: str, info: dict, key: Optional[str] = None) -> None:
        room = self.displays.setdefault(code, {})
        if display_id not in room and len(room) >= MAX_DISPLAYS:
            await ws.send_json({"type": "error", "detail": "This room has too many screens connected."})
            return
        self._unwait(code, ws)
        # Whether it records is the host's call, never the screen's.
        raw = {k: v for k, v in info.items() if k != "record"}
        try:
            saved = settings(raw)
        except Error:
            saved = settings({"name": raw.get("name")})
        saved["record"] = self.store.screen_record(code, display_id)
        old = room.get(display_id)
        if old and old["ws"] is not ws:
            await _try_send(old["ws"], {"type": "error", "detail": "This screen opened somewhere else."})
        # The upload key lets this screen save recordings, and only while the host has
        # recording switched on for it. It goes to this socket only.
        upload = secrets.token_urlsafe(24)
        # A "panel" is the host page itself playing the sound, with no picture of its own.
        room[display_id] = {"ws": ws, "settings": saved, "status": {"state": "idle", "mic_state": "unsupported", "recording": False, "reason": ""},
                            "audio": "unsupported", "outputs": [], "upload": upload, "panel": info.get("panel") is True}
        welcome = {"type": "welcome", "upload": upload, "settings": saved, "sound": self.store.sound(code)}
        if key:
            welcome["key"] = key  # the screen keeps it, to come back without asking again
        await ws.send_json(welcome)
        await self.send(code, {"type": "displays"})

    def waiting_list(self, code: str) -> list:
        since = time.monotonic() - WAIT_SECONDS
        return [{"id": wid, "name": w["name"], "code": w["code"]}
                for wid, w in self.waiting.get(code.upper(), {}).items() if w["since"] > since]

    async def let_in(self, code: str, wait_id: str, allow: bool) -> None:
        """The host lets a waiting screen in, or turns it away."""
        code = code.upper()
        w = self.waiting.get(code, {}).pop(wait_id, None)
        if w is None:
            raise Error("That screen isn't waiting anymore", 404)
        if allow:
            await self._admit(code, w["ws"], w["display"], w["info"], key=self.store.pair_screen(code, w["display"]))
        else:
            await _try_send(w["ws"], {"type": "error", "waiting": True, "detail": "The host didn't let this screen in."})
        await self.send(code, {"type": "waiting"})

    async def forget(self, code: str, display_id: str) -> None:
        """The host removes a screen: its key stops working and it has to ask again."""
        code = code.upper()
        self.store.forget_screen(code, display_id)
        display = self.displays.get(code, {}).pop(display_id, None)
        if display:
            await _try_send(display["ws"], {"type": "removed"})
        await self.send(code, {"type": "displays"})

    async def send_sound(self, code: str) -> None:
        sound = self.store.sound(code)
        for display in list(self.displays.get(code.upper(), {}).values()):
            await _try_send(display["ws"], {"type": "sound", "display": sound})

    async def receive(self, code: str, ws: WebSocket, message: dict, address: str = "",
                      may_wait: Callable[[], bool] = lambda: True, per_address: int = 4) -> None:
        """Handle a message a screen sent about itself. Anything malformed is ignored. `address`
        is where it came from, `may_wait` says whether that address may ask to join again, and
        `per_address` is how many screens from one address can wait at once."""
        code = code.upper()
        kind = message.get("type")
        room = self.displays.setdefault(code, {})
        if kind == "hello":
            await self._hello(code, ws, message, address, may_wait, per_address)
        elif kind == "outputs":
            for display in room.values():
                if display["ws"] is ws:
                    audio = message.get("audio")
                    display["audio"] = audio if audio in AUDIO else "unsupported"
                    display["outputs"] = outputs(message.get("outputs"))
                    await self.send(code, {"type": "displays"})
        elif kind == "status":
            for display in room.values():
                if display["ws"] is ws:
                    state, mic = message.get("state"), message.get("mic")
                    display["status"] = {
                        "state": state if state in STATES else "idle",
                        "mic_state": mic if mic in MICS else "unsupported",
                        "recording": message.get("recording") is True,
                        "time": _seconds(message.get("time")),
                        "duration": _seconds(message.get("duration")),
                        # Why the song won't play, as the screen put it, for the host's panel.
                        "reason": _name(message.get("reason"), MAX_TEXT, "") if state == "error" else "",
                    }
                    await self.send(code, {"type": "displays"})
        elif kind == "ended":
            # A screen opened from the host's browser moves the show along itself; one the host
            # let in by its code says so here. The song id makes two screens finishing the same
            # song move on once, and a late message do nothing.
            song = message.get("song")
            if any(d["ws"] is ws for d in room.values()) and isinstance(song, int) and not isinstance(song, bool):
                await asyncio.to_thread(self.store.song_ended, code, song)
                await self.notify(code)
        elif kind == "clock":
            # The screen the sound comes from says where it is in the song; the other screens
            # follow it so the picture matches the music. Only screens hear it, and it doesn't
            # count as a status change, so the host page isn't asked to refetch every second.
            sender = next((i for i, d in room.items() if d["ws"] is ws), None)
            song = message.get("song")
            if sender is None or not (song is None or (isinstance(song, int) and not isinstance(song, bool))):
                return
            if sender != self.store.sound(code):
                return  # only the screen the host picked for the sound sets the time
            state = message.get("state")
            clock = {"type": "clock", "display": sender, "song": song,
                     "state": state if state in STATES else "idle", "time": _seconds(message.get("time"))}
            for display_id, display in list(room.items()):
                if display_id != sender:
                    await _try_send(display["ws"], clock)

    def claim_upload(self, code: str, display_id: str, upload: str, song: int) -> None:
        """Check a screen may save a recording of `song`, and hold that spot: each screen saves
        each song once. `release_upload` gives the spot back if the save fails."""
        display = self.displays.get(code.upper(), {}).get(display_id)
        if not (display and upload and display["settings"]["record"]
                and secrets.compare_digest(display["upload"], upload)):
            raise Error("This screen isn't recording", 403)
        done = self.uploaded.setdefault(code.upper(), set())
        if (display_id, song) in done:
            raise Error("This screen already saved that song", 409)
        done.add((display_id, song))

    def release_upload(self, code: str, display_id: str, song: int) -> None:
        self.uploaded.get(code.upper(), set()).discard((display_id, song))

    def list(self, code: str) -> list:
        return [_public(display_id, d) for display_id, d in self.displays.get(code.upper(), {}).items()]

    async def command(self, code: str, display_id: str, changes: dict, action: Optional[str]) -> dict:
        display = self.displays.get(code.upper(), {}).get(display_id)
        if display is None:
            raise Error("That screen isn't connected anymore", 404)
        if action is not None and action not in ACTIONS:
            raise Error("Unknown action. Pick one of: " + ", ".join(ACTIONS))
        display["settings"] = settings(changes, display["settings"])
        if changes.get("record") is not None:
            self.store.screen_record(code, display_id, display["settings"]["record"])
        await display["ws"].send_json({"type": "command", "settings": display["settings"], "action": action})
        await self.send(code, {"type": "displays"})
        return _public(display_id, display)


def _public(display_id: str, display: dict) -> dict:
    return {"id": display_id, **display["settings"], **display["status"],
            "audio": display["audio"], "outputs": display["outputs"], "panel": display["panel"]}


async def _try_send(ws: WebSocket, message: dict) -> bool:
    """Send, or hang up on a socket that's gone or has stopped reading, so it can't stall the room."""
    try:
        await asyncio.wait_for(ws.send_json(message), SEND_TIMEOUT)
        return True
    except Exception:
        try:
            await asyncio.wait_for(ws.close(), 1)
        except Exception:
            pass
        return False


def _seconds(value) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, 1) if 0 <= value < 86400 else None
