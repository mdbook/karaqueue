"""HTTP API, WebSocket push and the static pages."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote, urlsplit

import segno
from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import youtube
from .displays import Hub
from .downloads import Downloads, Saver, readable
from .downloads import search as ytdlp_search
from .guards import Logins, Protect
from .limits import SOCKETS, Limiter
from .media import Library, lookup, naming_rule
from .settings import RULES_KEY, Settings, _scrypt, env_name, hash_password, password_matches
from .store import CLAIM_SECONDS, Error, NoRoom, Store

log = logging.getLogger("karaqueue")
STATIC = Path(__file__).parent / "static"
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a"}
FREE_DISK_FLOOR = 1024 ** 3  # ponytail: recordings never take the disk's last gigabyte; make it a setting if anyone asks


class RoomIn(BaseModel):
    name: str


class JoinIn(BaseModel):
    name: str = ""
    guest: Optional[str] = None  # a guest account's session, to join under it or find their old spot


class GuestIn(BaseModel):
    name: str
    password: str


class GuestChange(BaseModel):
    disabled: Optional[bool] = None
    password: Optional[str] = None


class ClaimIn(BaseModel):
    claim: str


class SongIn(BaseModel):
    title: str
    artist: str = ""  # optional: plenty of people only know the song's name
    singer_id: Optional[int] = None
    video: Optional[str] = None
    media: Optional[str] = None


class OrderIn(BaseModel):
    order: List[int]


class FlagIn(BaseModel):
    value: bool


class NextIn(BaseModel):
    # The song the caller thinks is playing. If it already moved on, nothing happens,
    # so two screens finishing the same video can't skip a singer.
    after: Optional[int] = None


class DisplayIn(BaseModel):
    name: Optional[str] = None
    layout: Optional[str] = None
    volume: Optional[int] = None
    muted: Optional[bool] = None
    sink: Optional[str] = None
    mic: Optional[bool] = None
    mic_volume: Optional[int] = None
    reverb: Optional[int] = None
    record: Optional[bool] = None
    action: Optional[str] = None


class OptionsIn(BaseModel):
    songs_per_singer: Optional[int] = None
    youtube: Optional[bool] = None
    library: Optional[bool] = None
    sound: Optional[str] = None


class SaveIn(BaseModel):
    video: str  # a YouTube link or id
    title: str
    artist: Optional[str] = ""


class SongFix(BaseModel):
    id: str
    title: Optional[str] = None  # None puts back what the file name says
    artist: Optional[str] = None


class SongsFix(BaseModel):
    ids: List[str] = Field(max_length=1000)
    action: str  # "artist" sets one artist on them all, "swap" trades title and artist, "undo" drops fixes
    artist: str = ""


class RuleIn(BaseModel):
    folder: str
    pattern: str = Field("", max_length=200)  # empty: go back to guessing from the file name


class ValueIn(BaseModel):
    value: Union[bool, int, float, str]


class PasswordIn(BaseModel):
    password: str
    setup: Optional[str] = None
    name: Optional[str] = None  # an account; no name means the built-in admin password


class InviteIn(BaseModel):
    role: Optional[str] = None  # a link to make a new account with this role,
    user: Optional[int] = None  # or to pick a new password for this account


class LinkIn(BaseModel):
    link: str = Field(max_length=200)


class AcceptIn(LinkIn):
    password: str
    name: str = ""  # for a new account


class UserIn(BaseModel):
    name: str
    role: str
    password: str


class UserChange(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None
    password: Optional[str] = None


def make_seekable(path: Path) -> None:
    """Browsers record webm without a length or seek index, so players can't skip around in it.
    Copying it through ffmpeg (no re-encoding) writes both. Without ffmpeg, or if it fails,
    the recording is kept as it came."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    fixed = path.with_name(path.stem + ".fixed" + path.suffix)
    try:
        result = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(path), "-c", "copy", str(fixed)],
                                capture_output=True, timeout=120)
        if result.returncode == 0 and fixed.stat().st_size > 0:
            os.replace(fixed, path)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        fixed.unlink(missing_ok=True)


def device(user_agent: str) -> str:
    """A short name for where someone signed in, like "Safari on iPhone", so admins can tell sessions apart."""
    ua = user_agent or ""
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("CrOS", "ChromeOS"), ("Mac OS X", "Mac"), ("Windows", "Windows"),
                                          ("Linux", "Linux")) if key in ua), "")
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                           ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "")
    return " on ".join(filter(None, (browser, system))) or "Unknown browser"


def client_address(host: str) -> str:
    """Who a request counts as for the limits. One IPv6 home or phone usually gets a whole /64,
    so that's what counts as one address."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def create_app(store: Optional[Store] = None) -> FastAPI:
    setup_file = None
    if store is None:
        db = os.environ.get("KARAQUEUE_DB", "karaqueue.db")
        store = Store(path=db)
        setup_file = Path(db).resolve().parent / "admin-setup-link.txt"
    cfg = Settings(store, setup_file=setup_file)
    app = FastAPI(title="KaraQueue")
    app.add_middleware(Protect, static=STATIC)
    app.state.settings = cfg
    hub = Hub(store)

    # Settings can change while the server runs, so everything reads them when it needs them.
    # The library and downloader are rebuilt when a setting they were made from changes.
    built: dict = {}

    def cached(name: str, args: tuple, make):
        old = built.get(name)
        if old and old[0] == args:
            return old[1]
        if old and hasattr(old[1], "pool"):
            old[1].pool.shutdown(wait=False)
        built[name] = (args, make(*args) if args[0] else None)
        return built[name][1]

    def library() -> Optional[Library]:
        roots = [f for f in cfg.get("media_dir").split(os.pathsep) if f]
        save = cfg.get("save_dir")
        # The saved-songs folder is part of the library, unless it's already inside a library folder.
        if save and not any(Path(save).resolve().is_relative_to(Path(r).resolve()) for r in roots):
            roots.append(save)
        return cached("library", (os.pathsep.join(roots),), lambda roots: Library(
            roots, edits=store.library_edits(), rules=store.load_settings().get(RULES_KEY, {})))

    def save_folder() -> Optional[Path]:
        if cfg.get("save_dir"):
            return Path(cfg.get("save_dir"))
        lib = library()
        return lib.root / "YouTube" if lib else None

    def saver() -> Optional[Saver]:
        """Saving YouTube songs into the library, if there's a folder for them and yt-dlp is installed."""
        if save_folder() is None or not shutil.which(cfg.get("ytdlp")):
            return None
        return cached("saver", (cfg.get("ytdlp"), cfg.get("ytdlp_format"), FREE_DISK_FLOOR,
                                cfg.get("save_max_mb") * 1024 ** 2), Saver)

    def downloads() -> Optional[Downloads]:
        return cached("downloads", (cfg.get("downloads_dir"), cfg.get("ytdlp"), cfg.get("ytdlp_format"),
                                    int(cfg.get("downloads_max_gb") * 1024 ** 3)), Downloads)

    def recordings_dir() -> Optional[Path]:
        return Path(cfg.get("recordings_dir")) if cfg.get("recordings_dir") else None

    def sync_store() -> None:
        for key in ("name_limit", "songs_per_singer", "code_length", "singers_per_room"):
            setattr(store, key, cfg.get(key))

    def sweep() -> None:
        # ponytail: runs at startup and whenever a room is made, not on a timer. A server
        # nobody makes rooms on isn't growing, so that's enough.
        days = cfg.get("room_days")
        if days <= 0:
            return
        busy = {code for code, sockets in hub.rooms.items() if sockets}
        for code in store.expire(time.time() - days * 86400, keep=busy):
            if recordings_dir() is not None:
                shutil.rmtree(recordings_dir() / code, ignore_errors=True)

    sync_store()
    sweep()
    for key in cfg.ignored():
        log.warning("Ignoring %s saved on the admin page (%s). Set %s in the environment instead.",
                    key, cfg.saved[key], env_name(key))
    if cfg.setup_token:
        # Never the link itself: logs get shipped and read by more people than the server's owner.
        where = f"is in {setup_file} on the server" if setup_file and setup_file.exists() else "couldn't be saved"
        log.warning("No admin password yet. The one-time setup link %s. Or set KARAQUEUE_ADMIN_PASSWORD.", where)

    def guest_may(code: str, tok: Optional[str], what: str) -> None:
        # Searching is for members only, so the server isn't an open search proxy, and only
        # for what the room allows.
        role, _ = store.auth(code, tok)
        if role != "host" and not store.state(code)["options"][what]:
            raise Error("The host has turned that off in this room", 403)

    @app.exception_handler(Error)
    async def store_error(request: Request, exc: Error):
        if isinstance(exc, NoRoom):
            request.state.no_room = True  # counted by rate_limits below
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # Per-address limits. Behind a reverse proxy, KARAQUEUE_TRUSTED_PROXIES has to name it, or
    # every guest looks like the proxy and they all share one set of limits.
    limiter = Limiter()
    app.state.limiter = limiter
    sockets_from: dict = {}  # address -> open sockets

    def address(conn) -> str:
        return client_address(conn.client.host if conn.client else "")

    async def signed_in(code: str, request: Request) -> bool:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        try:
            await asyncio.to_thread(store.auth, code, value)
            return True
        except Error:
            return False

    logins = Logins()
    # Guests sign in on a party's shared wifi, so their wrong passwords have their own, roomier
    # counts: a few typos at the venue never lock the admin out, or every other guest. Ten wrong
    # tries lock a name only for the phone making them, so a prankster can't lock someone else out.
    guest_logins = Logins(per_address=100, per_name=50, per_pair=10)

    def too_many(detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=429)

    @app.middleware("http")
    async def rate_limits(request: Request, call_next):
        scale, who, method = cfg.get("rate_limit"), address(request), request.method
        parts = request.url.path.strip("/").split("/")
        in_room = parts[:2] == ["api", "rooms"] and len(parts) > 2
        # Past the cap, only people already signed in to the room get through, so a party sharing
        # one address isn't locked out by a few guests' typos (each wrong code counts once).
        if in_room and limiter.full("miss", who, scale) and not await signed_in(parts[2], request):
            return too_many("Too many wrong room codes from here. Try again in a few minutes.")
        bucket = ("create" if method == "POST" and parts == ["api", "rooms"] else
                  "join" if method == "POST" and in_room and parts[3:] == ["singers"] else
                  "account" if method == "POST" and parts == ["api", "guests"] else
                  "link" if method == "POST" and in_room and parts[3:4] == ["singers"] and parts[5:] == ["link"] else
                  "search" if method == "GET" and in_room and parts[3:] == ["search"] else None)
        if bucket and not limiter.take(bucket, who, scale):
            return too_many("That's a lot of requests from here. Try again in a few minutes.")
        response = await call_next(request)
        if getattr(request.state, "no_room", False):
            limiter.add("miss", who, item=parts[2].upper())
        return response

    def token(authorization: str = Header("")) -> Optional[str]:
        scheme, _, value = authorization.partition(" ")
        return value if scheme.lower() == "bearer" and value else None

    # Store calls are blocking SQLite work, so endpoints that only touch the store are
    # plain `def` (FastAPI runs them in a thread). Ones that also notify sockets are async.

    @app.post("/api/rooms")
    def create_room(body: RoomIn, tok: Optional[str] = Depends(token)):
        if cfg.get("new_rooms") == "accounts":
            cfg.require(tok, role="host")
        sweep()
        return store.create_room(body.name)

    @app.get("/api/config")
    def config():
        return {"youtube_search": bool(cfg.get("youtube_api_key") or downloads() or saver()), "library": library() is not None,
                "recordings": recordings_dir() is not None, "downloads": downloads() is not None,
                "saving": saver() is not None and cfg.get("who_saves") == "hosts",
                "accounts_only": cfg.get("new_rooms") == "accounts"}

    # Every search runs yt-dlp or spends API quota, and party guests search the same songs, so
    # answers are kept a while. ponytail: two searches at once for the whole server, and the
    # 500 newest answers in memory. Enough for a few parties; make them settings if it isn't.
    searching = threading.BoundedSemaphore(2)
    answers: dict = {}  # (where, query) -> (when, hits)
    answers_lock = threading.Lock()

    def remembered_search(where: str, q: str, run) -> list:
        key = (where, " ".join(q.lower().split())[:100])
        with answers_lock:
            if key in answers and time.time() - answers[key][0] < 900:
                return answers[key][1]
        if not searching.acquire(timeout=5):
            raise Error("Search is busy. Try again in a moment.", 503)
        try:
            hits = run(q)
        finally:
            searching.release()
        with answers_lock:
            answers[key] = (time.time(), hits)
            while len(answers) > 500:
                answers.pop(next(iter(answers)))
        return hits

    def search_youtube(q: str) -> list:
        if cfg.get("youtube_api_key"):
            return remembered_search("api", q, lambda q: youtube.search(cfg.get("youtube_api_key"), q))
        if downloads() or saver():
            return remembered_search("yt-dlp", q, lambda q: ytdlp_search(cfg.get("ytdlp"), q))
        raise Error("Search isn't set up on this server. Paste a YouTube link instead.", 404)

    @app.get("/api/rooms/{code}/search")
    def search(code: str, q: str = "", tok: Optional[str] = Depends(token)):
        guest_may(code, tok, "youtube")
        return search_youtube(q)

    @app.get("/api/rooms/{code}/library")
    def search_library(code: str, q: str = "", tok: Optional[str] = Depends(token)):
        guest_may(code, tok, "library")
        if library() is None:
            raise Error("This server doesn't have a song library set up.", 404)
        return library().search(q)

    # Video tags can't send a bearer token, so the room's state hands screens a signed link to
    # the song that's playing. A link names its room and runs out, so knowing a room code
    # doesn't open the whole library, and an old link stops working. Links last 6 to 7 hours,
    # in whole hours so the state doesn't change on every fetch: well past the end of any song,
    # even one the host pauses for a while, since the screen keeps reading from the same link.
    def sign(kind: str, name: str, room: str, until: int) -> str:
        message = f"{kind}\n{name}\n{room}\n{until}".encode()
        return hmac.new(cfg.link_key, message, hashlib.sha256).hexdigest()[:32]

    def song_link(kind: str, name: str, room: str) -> str:
        until = (int(time.time()) // 3600 + 7) * 3600
        return f"/{kind}/{quote(name)}?room={room}&until={until}&sig={sign(kind, name, room, until)}"

    app.state.song_link = song_link

    def check_link(kind: str, name: str, room: str, until: int, sig: str) -> None:
        if until < time.time() or not hmac.compare_digest(sig, sign(kind, name, room, until)):
            raise Error("This link has run out. Reload the screen to get a new one.", 403)
        store.state(room)  # a deleted room's links die with it

    # Old video formats get converted while they play, a whole CPU core or more each.
    # A stream leaves this set as soon as its response ends, however it ends.
    converting = weakref.WeakSet()
    converting_lock = threading.Lock()

    class Conversion(StreamingResponse):
        # Starlette only drops a sync iterator when the screen goes away, so without this
        # ffmpeg would run on, holding its slot, until a full garbage collection.
        def __init__(self, stream):
            super().__init__(stream, media_type="video/mp4")
            self.stream = stream

        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                self.stream.close()  # stops ffmpeg
                with converting_lock:
                    converting.discard(self.stream)

    @app.get("/media/{rel:path}")
    def media_file(rel: str, room: str = "", until: int = 0, sig: str = "", part: str = "audio"):
        # Only files the library listed are served. `part=cdg` is the lyrics half of an MP3+CDG song.
        check_link("media", rel, room, until, sig)
        lib = library()
        entry = lib.get(rel) if lib else None
        if entry is None:
            raise Error("File not found", 404)
        if entry["kind"] == "zip":
            data, kind = lib.zip_member(rel, part)
            return Response(data, media_type=kind)
        if entry["kind"] == "convert":
            with converting_lock:
                if len(converting) >= cfg.get("conversions"):
                    raise Error("The server is busy converting other videos. Try again in a minute.", 503)
                stream = lib.convert(rel)
                converting.add(stream)
            return Conversion(stream)
        path = lib.cdg_path(rel) if part == "cdg" else lib.path(rel)
        if path is None:
            raise Error("File not found", 404)
        return FileResponse(path)

    @app.get("/downloads/{video_id}")
    def downloaded_file(video_id: str, room: str = "", until: int = 0, sig: str = ""):
        check_link("downloads", video_id, room, until, sig)
        dl = downloads()
        path = dl.ready(video_id) if dl else None
        if path is None:
            raise Error("File not found", 404)
        dl.touch(path)
        return FileResponse(path)

    # Each room can only make the server fetch so many new videos an hour, so one room can't
    # churn the downloads folder or get the server's address banned by YouTube. Songs past that
    # still go in the queue; they download later, or play from YouTube's player when their turn comes.
    # ponytail: kept in memory, so a restart starts every room's hour over.
    fetches: dict = {}  # (limit, room code) -> when it started each new download or save this past hour
    fetches_lock = threading.Lock()

    def one_more(room: str, always: bool = False, limit: str = "downloads_per_hour") -> bool:
        """Count a new download (or save, with limit "saves_per_hour") for the room, if it has any
        left this hour (or `always`)."""
        now, key = time.time(), (limit, room)
        with fetches_lock:
            for gone in [k for k, times in fetches.items() if not times or now - times[-1] >= 3600]:
                del fetches[gone]
            recent = [t for t in fetches.get(key, []) if now - t < 3600]
            if not always and len(recent) >= cfg.get(limit):
                fetches[key] = recent
                return False
            fetches[key] = recent + [now]
        return True

    def fetch(room: str, video: Optional[str], playing: bool = False) -> None:
        dl = downloads()
        if not dl or not video or dl.status(video) is not None:
            return  # nothing to do, or already here, on its way or just failed
        # The song that's on always comes down: that's one per song sung.
        if one_more(room, always=playing):
            dl.fetch(video)

    @app.get("/api/rooms/{code}")
    def room_state(code: str):
        state = store.state(code)
        np = state["now_playing"]
        dl, lib = downloads(), library()
        # With downloads on, YouTube songs only ever play from disk, like library songs: lots of
        # karaoke videos refuse to play embedded. Fetching here too catches songs queued before
        # downloads were turned on, or before a restart. Already-fetched ones are a quick no-op.
        if dl and np and np.get("video_id") and not np.get("media"):
            fetch(state["code"], np["video_id"], playing=True)
            for video in store.queued_videos(code):
                fetch(state["code"], video)
            status = dl.status(np["video_id"])
            if status == "ready":
                np["file"] = song_link("downloads", np["video_id"], state["code"])
            else:
                np["download"] = status or "pending"  # "failed": the screen tries YouTube's player
        entry = lib.get(np.get("media")) if lib and np and np.get("media") else None
        if entry:
            np["file"] = song_link("media", np["media"], state["code"])
            if entry["kind"] in ("cdg", "zip"):
                np["cdg"] = True  # the screen draws the lyrics itself
        return state

    @app.get("/api/rooms/{code}/join-info")
    def join_info(code: str, request: Request):
        room = store.state(code)["code"]
        # Behind a reverse proxy the request URL can be wrong, so let the operator pin it.
        home = cfg.get("public_url") or str(request.base_url).rstrip("/")
        link = f"{home}/r/{room}"
        # Opened at localhost, the QR code points guests' phones at themselves. The pages say so.
        host = urlsplit(home).hostname or ""
        local = host == "localhost" or host.endswith(".localhost") or host == "::1" or host.startswith("127.")
        return {"home": home, "link": link, "local": local,
                "qr_svg": segno.make(link).svg_inline(border=2, omitsize=True)}

    @app.get("/api/rooms/{code}/host")
    def host_state(code: str, tok: Optional[str] = Depends(token)):
        return store.host_state(code, tok)

    @app.get("/api/rooms/{code}/me")
    def me(code: str, tok: Optional[str] = Depends(token)):
        return store.me(code, tok)

    async def changed(code: str, result=None):
        store.touch(code)
        await hub.notify(code)
        return result if result is not None else {"ok": True}

    @app.post("/api/rooms/{code}/singers")
    async def join(code: str, body: JoinIn, tok: Optional[str] = Depends(token)):
        # The host adding someone by hand signs in as the room's host; a guest doesn't.
        by_host = store.is_host(code, tok)
        guest = None
        if body.guest and not by_host:
            guest = store.guest_session(body.guest)
            if guest is None:
                raise Error("You've been signed out of your account. Sign in again.", 401)
        joined = store.join(code, body.name, guest, by_host, token=tok)
        if by_host:
            joined.pop("token")  # the host shows them a "this is me" code to sign their phone in
        return await changed(code, joined)

    @app.post("/api/rooms/{code}/singers/{singer_id}/link")
    def singer_link(code: str, singer_id: int, request: Request, tok: Optional[str] = Depends(token)):
        """The host, or the singer themselves, gets a one-time link to sign another phone in as them.
        The code rides after the #, so it never reaches a proxy's logs."""
        claim = store.claim_link(code, tok, singer_id)
        home = cfg.get("public_url") or str(request.base_url).rstrip("/")
        link = f"{home}/r/{code.upper()}#claim={claim}"
        return {"claim": claim, "link": link, "minutes": CLAIM_SECONDS // 60,
                "qr_svg": segno.make(link).svg_inline(border=2, omitsize=True)}

    @app.post("/api/rooms/{code}/claim")
    def claim(code: str, body: ClaimIn):
        return store.claim(code, body.claim)

    # Guest accounts: optional. Signed in, a guest gets back to their spot in any room from any phone.
    # ponytail: name and password only, no email, so a forgotten password means an admin resets it.

    @app.post("/api/guests")
    def guest_sign_up(body: GuestIn, request: Request):
        guest_id = store.add_guest(body.name, hash_password(body.password))
        tok = store.start_guest_session(guest_id, device(request.headers.get("user-agent", "")))
        return {"token": tok, **store.guest_session_info(guest_id)}

    @app.post("/api/guests/login")
    def guest_sign_in(body: GuestIn, request: Request):
        here, name = address(request), " ".join(body.name.split()).lower()
        keys = [("address", here), ("name", name), ("pair", (here, name))]
        guest_logins.check(keys, scale=cfg.get("rate_limit"))
        guest = store.guest(name=body.name)
        if guest is None:
            _scrypt(body.password, bytes(16))  # as slow as a real check, so timing can't tell which names exist
        if guest is None or not password_matches(body.password, guest["password"]):
            guest_logins.failed(keys)
            raise Error("Wrong name or password", 401)
        if guest["disabled"]:
            raise Error("This account is turned off", 403)
        guest_logins.passed(*keys[1:])
        tok = store.start_guest_session(guest["id"], device(request.headers.get("user-agent", "")))
        return {"token": tok, **store.guest_session_info(guest["id"])}

    @app.get("/api/guests/me")
    def guest_me(tok: Optional[str] = Depends(token)):
        guest = store.guest_session(tok)
        if guest is None:
            raise Error("Not signed in", 401)
        return store.guest_session_info(guest["id"])

    @app.delete("/api/guests/me")
    def guest_sign_out(tok: Optional[str] = Depends(token)):
        store.end_guest_session(tok)
        return {"ok": True}

    @app.post("/api/rooms/{code}/songs")
    async def add_song(code: str, body: SongIn, tok: Optional[str] = Depends(token)):
        # The library lookup can rescan its folders and the fetch checks the downloads folder,
        # either of which can be a slow network share: off the event loop.
        def add():
            if body.media and (library() is None or library().get(body.media) is None):
                raise Error("That song isn't in the library anymore", 400)
            video = youtube.video_id(body.video)
            song = store.add_song(code, tok, body.title, body.artist, body.singer_id, video, body.media or None)
            fetch(code.upper(), video)
            return song
        return await changed(code, await asyncio.to_thread(add))

    @app.delete("/api/rooms/{code}/songs/{song_id}")
    async def remove_song(code: str, song_id: int, tok: Optional[str] = Depends(token)):
        store.remove_song(code, tok, song_id)
        return await changed(code)

    @app.put("/api/rooms/{code}/singers/{singer_id}/songs")
    async def reorder_songs(code: str, singer_id: int, body: OrderIn, tok: Optional[str] = Depends(token)):
        store.reorder_songs(code, tok, singer_id, body.order)
        return await changed(code)

    @app.put("/api/rooms/{code}/rotation")
    async def reorder_rotation(code: str, body: OrderIn, tok: Optional[str] = Depends(token)):
        store.reorder_rotation(code, tok, body.order)
        return await changed(code)

    @app.put("/api/rooms/{code}/singers/{singer_id}/hidden")
    async def hide(code: str, singer_id: int, body: FlagIn, tok: Optional[str] = Depends(token)):
        store.set_hidden(code, tok, singer_id, body.value)
        return await changed(code)

    @app.delete("/api/rooms/{code}/singers/{singer_id}")
    async def remove_singer(code: str, singer_id: int, tok: Optional[str] = Depends(token)):
        for name in store.remove_singer(code, tok, singer_id):
            drop_recording(name)
        return await changed(code)

    @app.put("/api/rooms/{code}/paused")
    async def pause(code: str, body: FlagIn, tok: Optional[str] = Depends(token)):
        store.set_paused(code, tok, body.value)
        return await changed(code)

    @app.put("/api/rooms/{code}/options")
    async def room_options(code: str, body: OptionsIn, tok: Optional[str] = Depends(token)):
        changes = body.model_dump(exclude_unset=True)
        store.set_options(code, tok, changes)
        if "sound" in changes:
            await hub.send_sound(code)
        return await changed(code)

    @app.post("/api/rooms/{code}/next")
    async def next_song(code: str, body: Optional[NextIn] = None, tok: Optional[str] = Depends(token)):
        store.next(code, tok, after=body.after if body else None)
        return await changed(code)

    @app.get("/api/rooms/{code}/displays")
    def displays(code: str, tok: Optional[str] = Depends(token)):
        store.require_host(code, tok)
        return hub.list(code)

    @app.patch("/api/rooms/{code}/displays/{display_id}")
    async def control_display(code: str, display_id: str, body: DisplayIn, tok: Optional[str] = Depends(token)):
        store.require_host(code, tok)
        return await hub.command(code, display_id, body.model_dump(exclude={"action"}), body.action)

    @app.delete("/api/rooms/{code}/displays/{display_id}")
    async def forget_display(code: str, display_id: str, tok: Optional[str] = Depends(token)):
        store.require_host(code, tok)
        await hub.forget(code, display_id)
        return {"ok": True}

    # Screens that opened the room but aren't let in yet. Each shows a code the host matches.
    @app.get("/api/rooms/{code}/waiting")
    def waiting_displays(code: str, tok: Optional[str] = Depends(token)):
        store.require_host(code, tok)
        return hub.waiting_list(code)

    @app.post("/api/rooms/{code}/waiting/{wait_id}")
    async def let_display_in(code: str, wait_id: str, body: FlagIn, tok: Optional[str] = Depends(token)):
        store.require_host(code, tok)
        await hub.let_in(code, wait_id, body.value)
        return {"ok": True}

    # Recordings: a screen with recording switched on uploads each performance when the song
    # ends, using the upload key it got over its socket. Singers fetch their own afterwards.
    def recordings_on() -> Path:
        if recordings_dir() is None:
            raise Error("Recording isn't set up on this server.", 404)
        return recordings_dir()

    @app.post("/api/rooms/{code}/recordings")
    async def upload_recording(code: str, request: Request, display: str, song: int,
                               x_upload: str = Header("")):
        folder = recordings_on()
        kind = request.headers.get("content-type", "").split(";")[0].strip()
        ext = AUDIO_TYPES.get(kind)
        if ext is None:
            raise Error("Recordings must be audio/webm, audio/ogg or audio/mp4", 415)
        store.recordable_song(code, song)
        room = store.state(code)["code"]
        # How much this one may take: the most for one take, what's left of the room's share and
        # the server's, and never the last of the disk.
        mb = 1024 * 1024
        room_left = cfg.get("recordings_room_mb") * mb - store.recordings_bytes(room)
        server_left = int(cfg.get("recordings_max_gb") * 1024 * mb) - store.recordings_bytes()
        folder.mkdir(parents=True, exist_ok=True)
        disk_left = shutil.disk_usage(folder).free - FREE_DISK_FLOOR
        limits = [(cfg.get("recording_max_mb") * mb, "That recording is too big for this server"),
                  (room_left, "This room's recordings are full. The host can delete some to make room."),
                  (server_left, "The server's recordings are full."),
                  (disk_left, "The server is almost out of space.")]
        limit, too_big = min(limits, key=lambda item: item[0])
        if limit <= 0:
            raise Error(too_big, 507)
        hub.claim_upload(code, display, x_upload, song)
        (folder / room).mkdir(parents=True, exist_ok=True)
        name = f"{room}/{song}-{secrets.token_hex(6)}{ext}"
        size = 0
        try:
            with open(folder / name, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise Error(too_big, 413)
                    out.write(chunk)
            if size == 0:
                raise Error("The recording was empty")
            if ext == ".webm":
                await asyncio.to_thread(make_seekable, folder / name)
                size = (folder / name).stat().st_size
            recording = store.add_recording(code, song, name, size)
        except BaseException:
            (folder / name).unlink(missing_ok=True)
            hub.release_upload(code, display, song)
            raise
        return await changed(code, {"id": recording})

    @app.get("/api/rooms/{code}/recordings")
    def list_recordings(code: str, tok: Optional[str] = Depends(token)):
        recordings_on()
        return store.recordings(code, tok)

    @app.get("/api/rooms/{code}/recordings/{recording_id}/file")
    def recording_file(code: str, recording_id: int, tok: Optional[str] = Depends(token)):
        folder = recordings_on()
        name = store.recording_file(code, tok, recording_id)
        return FileResponse(folder / name, filename="KaraQueue " + Path(name).name)

    @app.delete("/api/rooms/{code}/recordings/{recording_id}")
    async def remove_recording(code: str, recording_id: int, tok: Optional[str] = Depends(token)):
        recordings_on()
        drop_recording(store.remove_recording(code, tok, recording_id))
        return await changed(code)

    def drop_recording(name: str) -> None:
        """Delete a recording's file. Names come from the database (the server made them up), and
        anything that would land outside the recordings folder is left alone."""
        folder = recordings_dir()
        if folder is None:
            return
        path = folder / name
        # One stuck file shouldn't fail the request or keep the rest. Before Python 3.13 a
        # symlink loop makes resolve() raise RuntimeError, not OSError.
        try:
            path = path.resolve()
            if path.is_relative_to(folder.resolve()) and path != folder.resolve():
                path.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            log.warning("Couldn't delete recording %s: %s", path, exc)

    # -- the admin page: settings for the whole server ---------------------------

    def admin(tok: Optional[str] = Depends(token)) -> dict:
        return cfg.require(tok)

    @app.get("/api/admin/status")
    def admin_status():
        return {"setup": cfg.setup_token is not None, "password_env": cfg.password_from_env()}

    @app.post("/api/admin/login")
    def admin_login(body: PasswordIn, request: Request):
        """Admins sign in here for the admin page, and hosts to start rooms when that needs an account."""
        keys = [("address", address(request)),
                ("name", (body.name or "").strip().lower())]
        logins.check(keys)
        if body.setup is not None:
            # First run: the one-time link from admin-setup-link.txt lets you pick a password.
            if not cfg.setup_token or not secrets.compare_digest(body.setup, cfg.setup_token):
                raise Error("That setup link has already been used or is out of date", 403)
            cfg.set_password(body.password)
            body.name = None
        tok = cfg.log_in(body.name, body.password, device(request.headers.get("user-agent", "")))
        if tok is None:
            logins.failed(keys)
            raise Error("Wrong name or password" if body.name else "Wrong password", 401)
        logins.passed(keys[1])
        return {"token": tok, **cfg.whoami(tok)}

    @app.post("/api/admin/logout")
    def admin_logout(tok: Optional[str] = Depends(token)):
        if tok:
            store.end_staff_sessions(token=tok)
        return {"ok": True}

    # Links an admin sends someone: to make an account (no password passed around), or to pick a
    # new one for a forgotten password. Each works once, and only for a while.

    # The link's secret rides in the body, never the URL, so access logs never hold a usable invite.

    @app.post("/api/invites/look")
    def invite_info(body: LinkIn):
        found = store.invite(body.link)
        if found is None:
            raise Error("That link has already been used or is out of date. Ask an admin for a new one.", 404)
        return {"role": found["role"], "name": found["name"]}

    @app.post("/api/invites/accept")
    def accept_invite(body: AcceptIn, request: Request):
        tok = cfg.accept_invite(body.link, body.password, body.name, device(request.headers.get("user-agent", "")))
        return {"token": tok, **cfg.whoami(tok)}

    @app.get("/api/admin/me")
    def admin_me(tok: Optional[str] = Depends(token)):
        return cfg.require(tok, role="host")

    def admin_state() -> dict:
        open_rooms = {c: len(s) for c, s in hub.rooms.items()}
        rooms = [{**r, "open": open_rooms.get(r["code"], 0), "screens": len(hub.displays.get(r["code"], {}))}
                 for r in store.rooms_overview()]
        lib, dl = library(), downloads()
        return {"settings": cfg.describe(), "startup": cfg.startup(), "rooms": rooms,
                "password_env": cfg.password_from_env(), "users": store.users(), "guests": store.guests(),
                "sessions": store.sessions(), "invites": store.invites(),
                "library": lib.counts() if lib else None,
                "downloads": dl.usage() if dl else None}

    @app.get("/api/admin")
    def admin_get(who: dict = Depends(admin)):
        return {**admin_state(), "me": who}

    @app.post("/api/admin/users", dependencies=[Depends(admin)])
    def admin_add_user(body: UserIn):
        cfg.add_user(body.name, body.role, body.password)
        return admin_state()

    @app.patch("/api/admin/users/{user_id}")
    def admin_change_user(user_id: int, body: UserChange, who: dict = Depends(admin)):
        cfg.update_user(who, user_id, body.name, body.role, body.disabled, body.password)
        return admin_state()

    @app.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: int, who: dict = Depends(admin)):
        cfg.delete_user(who, user_id)
        return admin_state()

    @app.post("/api/admin/invites")
    def admin_invite(body: InviteIn, who: dict = Depends(admin)):
        if (body.role is None) == (body.user is None):
            raise Error("Pick a role for a new account, or an account for a new password")
        if body.role is not None and body.role not in ("admin", "host"):
            raise Error("Role must be admin or host")
        return {"link": "/admin#invite=" + store.add_invite(who["name"], who["id"], body.role, body.user), **admin_state()}

    @app.delete("/api/admin/invites/{invite_id}", dependencies=[Depends(admin)])
    def admin_cancel_invite(invite_id: str):
        store.cancel_invite(invite_id)
        return admin_state()

    @app.delete("/api/admin/sessions/{kind}/{session_id}", dependencies=[Depends(admin)])
    def admin_sign_out(kind: str, session_id: str):
        """Sign out one phone or computer."""
        if kind == "guest":
            store.end_guest_session(session=session_id)
        elif kind == "staff":
            store.end_staff_sessions(session=session_id)
        else:
            raise Error("No such kind of sign-in", 404)
        return admin_state()

    @app.delete("/api/admin/users/{user_id}/sessions")
    def admin_sign_out_user(user_id: int, who: dict = Depends(admin)):
        """Sign an account out everywhere (but not the admin doing it, on this device)."""
        store.end_staff_sessions(user_id, keep=who["session"] if who["id"] == user_id else None)
        return admin_state()

    @app.delete("/api/admin/guests/{guest_id}/sessions", dependencies=[Depends(admin)])
    def admin_sign_out_guest(guest_id: int):
        store.end_guest_sessions(guest_id)
        return admin_state()

    @app.get("/api/admin/guests", dependencies=[Depends(admin)])
    def admin_guests():
        return store.guests()

    @app.patch("/api/admin/guests/{guest_id}", dependencies=[Depends(admin)])
    def admin_change_guest(guest_id: int, body: GuestChange):
        fields = {}
        if body.disabled is not None:
            fields["disabled"] = int(body.disabled)
        if body.password is not None:
            fields["password"] = hash_password(body.password)
        store.update_guest(guest_id, **fields)
        return admin_state()

    @app.delete("/api/admin/guests/{guest_id}", dependencies=[Depends(admin)])
    def admin_delete_guest(guest_id: int):
        store.delete_guest(guest_id)
        return admin_state()

    @app.put("/api/admin/settings/{key}", dependencies=[Depends(admin)])
    def admin_set(key: str, body: ValueIn):
        cfg.set(key, body.value)
        sync_store()
        return admin_state()

    @app.delete("/api/admin/settings/{key}", dependencies=[Depends(admin)])
    def admin_reset(key: str):
        cfg.reset(key)
        sync_store()
        return admin_state()

    @app.put("/api/admin/password")
    def admin_password(body: PasswordIn, who: dict = Depends(admin)):
        cfg.set_password(body.password, who)
        return {"ok": True}

    @app.delete("/api/admin/rooms/{code}", dependencies=[Depends(admin)])
    async def admin_delete_room(code: str):
        room = store.delete_room(code)
        if recordings_dir() is not None:
            shutil.rmtree(recordings_dir() / room, ignore_errors=True)
        await hub.notify(room)  # open pages refetch, find the room gone and say so
        return admin_state()

    @app.post("/api/admin/library/rescan", dependencies=[Depends(admin)])
    def admin_rescan():
        if library() is None:
            raise Error("The song library is off", 404)
        library().counts(rescan=True)
        return admin_state()

    @app.delete("/api/admin/downloads", dependencies=[Depends(admin)])
    def admin_clear_downloads():
        if downloads() is None:
            raise Error("Downloading is off", 404)
        downloads().clear()
        return admin_state()

    # ---- The Library tab: folders, every song, downloaded songs, and disk space ----

    def folder_size(path: Optional[Path]) -> int:
        if path is None or not path.is_dir():
            return 0
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    def library_state() -> dict:
        lib, dl, rec = library(), downloads(), recordings_dir()
        places = [{"label": f["key"], "kind": "library", "path": f["path"], "bytes": f["bytes"], "songs": f["songs"],
                   "exists": f["exists"], "rule": f["rule"]} for f in (lib.folder_stats() if lib else [])]
        if dl:
            usage = dl.usage()
            places.append({"label": "Downloaded songs", "kind": "downloads", "path": str(dl.folder),
                           "bytes": usage["bytes"], "songs": usage["files"], "max_bytes": usage["max_bytes"], "exists": True})
        if rec:
            places.append({"label": "Recordings", "kind": "recordings", "path": str(rec), "bytes": folder_size(rec),
                           "exists": rec.is_dir()})
        # Free space once per disk, however many of the folders above share it.
        disks = {}
        for place in places:
            try:
                device = os.stat(place["path"]).st_dev
                usage = shutil.disk_usage(place["path"])
            except OSError:
                continue
            disk = disks.setdefault(device, {"paths": [], "free": usage.free, "total": usage.total})
            disk["paths"].append(place["label"])
        return {"places": places, "disks": list(disks.values()), "library": lib is not None, "downloads": dl is not None,
                "saving": saver() is not None, "lookup": cfg.get("song_lookup") != "off", "save_folder": str(save_folder()) if save_folder() else None,
                "search": bool(cfg.get("youtube_api_key") or dl or saver())}

    @app.get("/api/admin/library", dependencies=[Depends(admin)])
    def admin_library():
        return library_state()

    def set_folders(folders: list) -> None:
        cfg.set("media_dir", os.pathsep.join(folders))  # none left: the library is off

    @app.post("/api/admin/library/folders", dependencies=[Depends(admin)])
    def admin_add_folder(body: ValueIn):
        folder = str(body.value or "").strip()
        if not folder or os.pathsep in folder:
            raise Error("Type the full path of one folder on the server")
        current = [f for f in cfg.get("media_dir").split(os.pathsep) if f]
        set_folders(current + [folder])
        return library_state()

    @app.delete("/api/admin/library/folders", dependencies=[Depends(admin)])
    def admin_remove_folder(path: str):
        current = [f for f in cfg.get("media_dir").split(os.pathsep) if f]
        lib = library()
        keep = [f for f in current if f != path and (lib is None or str(Path(f).resolve()) != path)]
        if len(keep) == len(current):
            raise Error("That folder isn't in the library", 404)
        set_folders(keep)  # only stops using it: nothing on disk is touched
        return library_state()

    def the_library() -> Library:
        if library() is None:
            raise Error("The song library is off", 404)
        return library()

    @app.get("/api/admin/library/songs", dependencies=[Depends(admin)])
    def admin_library_songs(q: str = "", offset: int = 0):
        return the_library().songs(q[:200], max(0, offset))

    @app.patch("/api/admin/library/songs", dependencies=[Depends(admin)])
    def admin_fix_song(body: SongFix):
        lib = the_library()
        title = None if body.title is None else " ".join(body.title.split())[:200]
        if title == "":
            raise Error("A song needs a title")
        artist, entry = " ".join((body.artist or "").split())[:200], lib.get(body.id)
        if entry is None:
            raise Error("That song isn't in the library anymore", 404)
        store.save_library_edit(entry["id"], title, artist)  # saved first, so a fix shown is a fix kept
        try:
            entry = lib.rename(entry["id"], title, artist)
        except KeyError:  # deleted a moment ago
            raise Error("That song isn't in the library anymore", 404)
        return {"id": entry["id"], "title": entry["title"], "artist": entry["artist"], "edited": title is not None}

    @app.post("/api/admin/library/songs/fix", dependencies=[Depends(admin)])
    def admin_fix_songs(body: SongsFix):
        """One fix for many songs at once: everything by one artist, titles and artists the wrong way
        round, or back to what the file names say."""
        lib = the_library()
        artist = " ".join(body.artist.split())[:200]
        if body.action not in ("artist", "swap", "undo"):
            raise Error("Pick set artist, swap or undo")
        if body.action == "artist" and not artist:
            raise Error("Type the artist to set")
        plan = []
        for song in dict.fromkeys(body.ids):
            entry = lib.get(song)
            if entry is None:
                continue
            title, new_artist = entry["title"], artist
            if body.action == "swap":
                title, new_artist = entry["artist"], entry["title"]
                if not title:
                    continue  # nothing to swap in as the title
            if body.action == "undo":
                title = None
            plan.append((entry["id"], title, new_artist))
        store.save_library_edits(plan)  # saved first, so fixes shown are fixes kept
        fixed = []
        for song, title, artist in plan:
            try:
                entry = lib.rename(song, title, artist)
            except KeyError:
                continue  # deleted a moment ago
            fixed.append({"id": entry["id"], "title": entry["title"], "artist": entry["artist"], "edited": title is not None})
        return {"songs": fixed}

    @app.get("/api/admin/library/lookup", dependencies=[Depends(admin)])
    def admin_lookup(title: str, artist: str = ""):
        if cfg.get("song_lookup") == "off":
            raise Error("Song lookup is turned off in Settings", 404)
        return {"matches": lookup(title, artist)}

    def library_folder(path: str) -> str:
        lib = the_library()
        if path not in [str(f["root"]) for f in lib.folders]:
            raise Error("That folder isn't in the library", 404)
        return path

    @app.post("/api/admin/library/rules/preview", dependencies=[Depends(admin)])
    def admin_preview_rule(body: RuleIn):
        try:
            return the_library().preview(library_folder(body.folder), body.pattern)
        except ValueError as e:
            raise Error(str(e))

    @app.put("/api/admin/library/rules", dependencies=[Depends(admin)])
    def admin_save_rule(body: RuleIn):
        """A folder's naming rule. Songs an admin fixed by hand keep their fix."""
        folder, pattern = library_folder(body.folder), " ".join(body.pattern.split())
        rules = store.load_settings().get(RULES_KEY, {})
        rules = {p: r for p, r in rules.items() if str(Path(p).resolve()) != folder}
        if pattern:
            try:
                naming_rule(pattern)
            except ValueError as e:
                raise Error(str(e))
            rules[folder] = pattern
        store.save_setting(RULES_KEY, rules or None)
        the_library().set_rules(rules)
        return library_state()

    @app.delete("/api/admin/library/songs", dependencies=[Depends(admin)])
    def admin_delete_song(id: str):
        try:
            song = the_library().delete(id)
        except OSError:
            raise Error("The server can't delete that file. Is the folder read-only?", 409)
        if song is None:
            raise Error("That song isn't in the library anymore", 404)
        store.save_library_edit(song, None)
        return {"ok": True}

    @app.get("/api/admin/downloads", dependencies=[Depends(admin)])
    def admin_list_downloads():
        if downloads() is None:
            raise Error("Downloading is off", 404)
        files = downloads().songs()
        names = store.video_names([f["video_id"] for f in files])
        return [{**f, **names.get(f["video_id"], {"title": "", "artist": ""})} for f in files]

    @app.delete("/api/admin/downloads/{video_id}", dependencies=[Depends(admin)])
    def admin_delete_download(video_id: str):
        if downloads() is None or not downloads().delete(video_id):
            raise Error("That song isn't downloaded", 404)
        return {"ok": True}

    # ---- Saving YouTube songs into the library for good ----

    def start_save(body: SaveIn, room: Optional[str] = None) -> dict:
        save = saver()
        if save is None:
            raise Error("Saving songs needs a library folder and yt-dlp on the server", 404)
        video = youtube.video_id(body.video)
        if not video:
            raise Error("That isn't a YouTube link")
        if not readable(body.title):  # before it can use up one of the room's saves
            raise Error("Give the song a title")
        folder = save_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("Can't save into %s", folder)
            raise Error("The folder for saved songs isn't usable. The server log says why.", 500)
        # Only a new save counts against the room's hour: not one that's saved, or on its way.
        on_its_way = (save.jobs.get(video) or {}).get("state") in ("waiting", "saving")
        if room and not on_its_way and not save.saved(folder, video):
            save.check_room()  # a full queue turns it away without using up one of the room's saves
            if not one_more(room, limit="saves_per_hour"):
                raise Error("This room has saved a lot this hour. Try again later.", 429)
        dl = downloads()

        def done() -> None:
            lib = library()
            if lib:
                lib.scanned = 0.0  # the next search looks again and finds it
        return save.save(folder, video, body.title[:200], (body.artist or "")[:200], room=room,
                         cached=dl.ready(video) if dl else None, done=done)

    def host_saves(code: str, tok: Optional[str]) -> None:
        store.require_host(code, tok)
        if cfg.get("who_saves") != "hosts":
            raise Error("Only admins can save songs on this server", 403)

    @app.post("/api/rooms/{code}/saves")
    def room_save(code: str, body: SaveIn, tok: Optional[str] = Depends(token)):
        host_saves(code, tok)
        return start_save(body, room=code.upper())

    @app.get("/api/rooms/{code}/saves")
    def room_saves(code: str, tok: Optional[str] = Depends(token)):
        host_saves(code, tok)
        return saver().list(code.upper()) if saver() else []

    @app.get("/api/admin/youtube", dependencies=[Depends(admin)])
    def admin_search(q: str = ""):
        return search_youtube(q)

    @app.post("/api/admin/saves", dependencies=[Depends(admin)])
    def admin_save(body: SaveIn):
        return start_save(body)

    @app.get("/api/admin/saves", dependencies=[Depends(admin)])
    def admin_saves():
        return saver().list() if saver() else []

    @app.websocket("/ws/{code}")
    async def socket(ws: WebSocket, code: str):
        # The server pushes "something changed" and clients refetch what they're allowed to see.
        # The only thing a client can say back is a screen describing itself.
        # Accepting before closing lets the page see why (closing first reaches it as 1006).
        scale, who = cfg.get("rate_limit"), address(ws)
        await ws.accept()
        if scale > 0 and sockets_from.get(who, 0) >= max(1, round(SOCKETS * scale)):
            await ws.close(code=4429)
            return
        try:
            store.state(code)
        except NoRoom:
            # Only wrong codes count against the guess limit, so a prankster guessing codes on
            # the venue's wifi can't lock the real room's phones and screens out with them.
            full = limiter.full("miss", who, scale)
            if not full:
                limiter.add("miss", who, item=code.upper())
            await ws.close(code=4429 if full else 4404)
            return
        if not hub.connect(code, ws):
            await ws.close(code=1013)  # "try again later": the room has too many open connections
            return
        sockets_from[who] = sockets_from.get(who, 0) + 1
        try:
            while True:
                text = await ws.receive_text()
                if len(text) > 10000:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await hub.receive(code, ws, message, who, lambda: limiter.take("pair", who, scale),
                                      cfg.get("screens_per_address"))
        except WebSocketDisconnect:
            pass
        finally:
            sockets_from[who] -= 1
            if not sockets_from[who]:
                del sockets_from[who]
            await hub.disconnect(code, ws)

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC / "admin.html")

    @app.get("/r/{code}")
    def singer_page(code: str):
        return FileResponse(STATIC / "room.html")

    @app.get("/r/{code}/host")
    def host_page(code: str):
        return FileResponse(STATIC / "host.html")

    @app.get("/r/{code}/screen")
    def screen_page(code: str):
        return FileResponse(STATIC / "screen.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    import uvicorn

    # Client addresses come from X-Forwarded-For only when the request came from one of these
    # (comma-separated addresses or networks, or * for any). The rate limits depend on it.
    # Clients only ever send the server short JSON over the socket.
    uvicorn.run("karaqueue.app:create_app", factory=True, ws_max_size=16 * 1024,
                host=os.environ.get("KARAQUEUE_HOST", "0.0.0.0"),
                port=int(os.environ.get("KARAQUEUE_PORT", "8000")),
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("KARAQUEUE_TRUSTED_PROXIES") or "127.0.0.1",
                # A TV mid-song holds a stream open; don't let that stall a restart.
                timeout_graceful_shutdown=5)
