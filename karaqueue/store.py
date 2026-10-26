"""Rooms, singers, songs and the singer rotation, backed by SQLite."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Optional

TAKE_WINDOW = 15 * 60  # seconds after a song ends that a screen can still save its recording
SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    host_hash TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    current_song_id INTEGER,
    last_singer_id INTEGER,
    created_at REAL NOT NULL,
    touched_at REAL
);
CREATE TABLE IF NOT EXISTS singers (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    position INTEGER NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room_id, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    singer_id INTEGER NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    video_id TEXT,
    media TEXT,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    file TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('admin', 'host')),
    password TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    seen_at REAL
);
CREATE TABLE IF NOT EXISTS staff_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL: signed in with the admin password
    device TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    role TEXT CHECK (role IN ('admin', 'host')),  -- a link to make a new account with this role,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- or to pick a new password for this one
    made_by TEXT NOT NULL,
    made_by_id INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL: the built-in admin
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library_edits (
    song TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    seen_at REAL
);
CREATE TABLE IF NOT EXISTS guest_sessions (
    token_hash TEXT PRIMARY KEY,
    guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS singer_tokens (
    token_hash TEXT PRIMARY KEY,
    singer_id INTEGER NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS screens (
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    display_id TEXT NOT NULL,
    key_hash TEXT,
    record INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room_id, display_id)
);
"""
# What a room allows guests to do, unless the host changes it.
# `sound` is the id of the one screen the music, mic and recording happen on; "" means each screen plays its own.
ROOM_OPTIONS = {"songs_per_singer": None, "youtube": True, "library": True, "sound": ""}

DEVICES = 5  # phones one singer can be signed in on at once; the oldest is signed out past that
GUEST_SESSIONS = 10  # the same, for a guest account
CLAIM_SECONDS = 600  # how long a "this is me" link works
GUEST_SESSION_DAYS = 180  # a guest account stays signed in on a phone this long
STAFF_SESSIONS = 10  # phones and computers one admin or host account can be signed in on at once
STAFF_SESSION_DAYS = 30  # an admin or host stays signed in this long after they last used the page
STAFF_SESSION_MAX_DAYS = 90  # and never longer than this after signing in
# What admins see as a sign-in's or link's id: the start of its token's hash. Never reused, unlike a
# rowid, so a stale list can't sign out the wrong phone, and no use for signing in.
PUBLIC_ID = "substr(token_hash, 1, 16)"
INVITE_DAYS = 7  # how long a link to make an account works
RESET_DAYS = 1  # how long a link to pick a new password works

# No 0/O or 1/I, so codes survive being read aloud or off a screen.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Error(Exception):
    """A request the caller got wrong. `status` maps onto an HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NoRoom(Error):
    """No room has that code. Counted per address, so nobody can guess codes until one works."""


def _hash(token: str) -> str:
    # Tokens are 32 random bytes, so a plain digest is enough; no need for a slow KDF.
    return hashlib.sha256(token.encode()).hexdigest()


def _clean(value: str, field: str, limit: int, optional: bool = False) -> str:
    value = " ".join((value or "").split())
    if not value and not optional:
        raise Error(f"{field} is required")
    if len(value) > limit:
        raise Error(f"{field} must be {limit} characters or fewer")
    return value


class Store:
    def __init__(self, path: str = ":memory:", name_limit: int = 32, text_limit: int = 120,
                 songs_per_singer: int = 10, code_length: int = 6, singers_per_room: int = 100):
        # ponytail: one shared connection behind one lock. Plenty for a party; swap for a
        # connection pool if a single instance ever hosts hundreds of busy rooms.
        if path != ":memory:":
            # It holds the key that signs song links and any saved API key: the server's user only.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
            try:
                os.chmod(path, 0o600)  # databases made before this
            except OSError:
                pass
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        # Databases from before the media library don't have this column yet.
        if "media" not in [c[1] for c in self.db.execute("PRAGMA table_info(songs)")]:
            self.db.execute("ALTER TABLE songs ADD COLUMN media TEXT")
        # Or this one, from before room cleanup.
        if "touched_at" not in [c[1] for c in self.db.execute("PRAGMA table_info(rooms)")]:
            self.db.execute("ALTER TABLE rooms ADD COLUMN touched_at REAL")
        # Or this one, from before per-room options.
        if "options" not in [c[1] for c in self.db.execute("PRAGMA table_info(rooms)")]:
            self.db.execute("ALTER TABLE rooms ADD COLUMN options TEXT")
        # Or this one, from before recordings were tied to the song that just played.
        if "previous_song_id" not in [c[1] for c in self.db.execute("PRAGMA table_info(rooms)")]:
            self.db.execute("ALTER TABLE rooms ADD COLUMN previous_song_id INTEGER")
        # Or this one, from before a take could be saved for a while after its song ended.
        if "ended_at" not in [c[1] for c in self.db.execute("PRAGMA table_info(songs)")]:
            self.db.execute("ALTER TABLE songs ADD COLUMN ended_at REAL")
        # Or this one, from before guests could have accounts.
        if "guest_id" not in [c[1] for c in self.db.execute("PRAGMA table_info(singers)")]:
            self.db.execute("ALTER TABLE singers ADD COLUMN guest_id INTEGER REFERENCES guests(id) ON DELETE SET NULL")
        # Or these, from before admins could see where guests are signed in.
        columns = [c[1] for c in self.db.execute("PRAGMA table_info(guest_sessions)")]
        if "device" not in columns:
            self.db.execute("ALTER TABLE guest_sessions ADD COLUMN device TEXT NOT NULL DEFAULT ''")
        if "seen_at" not in columns:  # checked on its own, so a crash between the two can't strand it
            self.db.execute("ALTER TABLE guest_sessions ADD COLUMN seen_at REAL")
        # Or this, from before a link died with its maker's access.
        if "made_by_id" not in [c[1] for c in self.db.execute("PRAGMA table_info(invites)")]:
            self.db.execute("ALTER TABLE invites ADD COLUMN made_by_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        self.lock = threading.RLock()
        self.name_limit = name_limit
        self.text_limit = text_limit
        self.songs_per_singer = songs_per_singer
        self.code_length = code_length
        self.singers_per_room = singers_per_room
        # "This is me" links: claim hash -> (room id, singer id, good until). In memory: they
        # only last minutes, and a restart just means asking for a new one.
        self.claims: dict = {}

    def _drop_claims(self, room_id: Optional[int] = None, singer_ids=()) -> None:
        """Forget the links for these singers, or for a whole room, and any that ran out."""
        now, singer_ids = time.time(), set(singer_ids)
        for key, (room, singer, until) in list(self.claims.items()):
            if until < now or room == room_id or singer in singer_ids:
                del self.claims[key]

    # -- helpers -----------------------------------------------------------------

    def _one(self, sql: str, *args) -> Optional[sqlite3.Row]:
        return self.db.execute(sql, args).fetchone()

    def _room(self, code: str) -> sqlite3.Row:
        room = self._one("SELECT * FROM rooms WHERE code = ?", (code or "").upper())
        if room is None:
            raise NoRoom("Room not found", 404)
        return room

    def _singer(self, room: sqlite3.Row, singer_id: int) -> sqlite3.Row:
        singer = self._one("SELECT * FROM singers WHERE id = ? AND room_id = ?", singer_id, room["id"])
        if singer is None:
            raise Error("Singer not found", 404)
        return singer

    def _rotation(self, room_id: int) -> list:
        return self.db.execute(
            "SELECT * FROM singers WHERE room_id = ? ORDER BY position", (room_id,)).fetchall()

    def _queued(self, singer_id: int) -> list:
        return self.db.execute(
            "SELECT * FROM songs WHERE singer_id = ? AND status = 'queued' ORDER BY position",
            (singer_id,)).fetchall()

    def _pick_next(self, room: sqlite3.Row) -> Optional[sqlite3.Row]:
        """First queued song of the next visible singer after whoever sang last."""
        singers = self._rotation(room["id"])
        ids = [s["id"] for s in singers]
        start = ids.index(room["last_singer_id"]) + 1 if room["last_singer_id"] in ids else 0
        for singer in singers[start:] + singers[:start]:
            if singer["hidden"]:
                continue
            queued = self._queued(singer["id"])
            if queued:
                return queued[0]
        return None

    @staticmethod
    def _options(room: sqlite3.Row) -> dict:
        return {**ROOM_OPTIONS, **json.loads(room["options"] or "{}")}

    @staticmethod
    def _song(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "title": row["title"], "artist": row["artist"],
                "video_id": row["video_id"], "media": row["media"]}

    # -- auth --------------------------------------------------------------------

    def auth(self, code: str, token: Optional[str]) -> tuple:
        """Return ("host", None), ("singer", singer_id) or raise 401."""
        with self.lock:
            room = self._room(code)
            if token:
                digest = _hash(token)
                if secrets.compare_digest(digest, room["host_hash"]):
                    return "host", None
                singer = self._one("SELECT id FROM singers WHERE room_id = ? AND token_hash = ?",
                                   room["id"], digest) or self._one(
                    "SELECT s.id FROM singer_tokens t JOIN singers s ON s.id = t.singer_id "
                    "WHERE s.room_id = ? AND t.token_hash = ?", room["id"], digest)
                if singer:
                    return "singer", singer["id"]
        raise Error("Not signed in to this room", 401)

    def is_host(self, code: str, token: Optional[str]) -> bool:
        with self.lock:
            try:
                return self.auth(code, token)[0] == "host"
            except Error:
                return False

    def require_host(self, code: str, token: Optional[str]) -> None:
        if self.auth(code, token)[0] != "host":
            raise Error("Only the host can do that", 403)

    def _may_edit(self, code: str, token: Optional[str], singer_id: int) -> None:
        role, me = self.auth(code, token)
        if role != "host" and me != singer_id:
            raise Error("That isn't your song", 403)

    # -- rooms -------------------------------------------------------------------

    def create_room(self, name: str) -> dict:
        name = _clean(name, "Room name", self.text_limit)
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            for _ in range(20):
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(self.code_length))
                if not self._one("SELECT 1 FROM rooms WHERE code = ?", code):
                    break
            else:
                raise Error("Couldn't find a free room code, try again", 503)
            self.db.execute("INSERT INTO rooms (code, name, host_hash, created_at) VALUES (?, ?, ?, ?)",
                            (code, name, _hash(token), time.time()))
        return {"code": code, "host_token": token}

    def touch(self, code: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE rooms SET touched_at = ? WHERE code = ?", (time.time(), (code or "").upper()))

    def expire(self, before: float, keep=()) -> list:
        """Delete rooms nobody has changed since `before`, except the codes in `keep`.
        Returns the deleted codes so the caller can clear out their files."""
        with self.lock, self.db:
            codes = [r["code"] for r in self.db.execute(
                "SELECT code FROM rooms WHERE COALESCE(touched_at, created_at) < ?", (before,))
                if r["code"] not in keep]
            for code in codes:
                self._drop_claims(room_id=self._room(code)["id"])
            self.db.executemany("DELETE FROM rooms WHERE code = ?", [(c,) for c in codes])
        return codes

    def state(self, code: str) -> dict:
        """What anyone in the room can see: who's up, who's next, the singer order."""
        with self.lock:
            room = self._room(code)
            current = None
            if room["current_song_id"]:
                row = self._one("SELECT songs.*, singers.name AS singer FROM songs JOIN singers "
                                "ON singers.id = songs.singer_id WHERE songs.id = ?",
                                room["current_song_id"])
                if row:
                    current = {**self._song(row), "singer": row["singer"]}
            upcoming = self._pick_next(room)
            up_next = None
            if upcoming:
                singer = self._one("SELECT name FROM singers WHERE id = ?", upcoming["singer_id"])
                up_next = {"singer": singer["name"]}
            rotation = [{"id": s["id"], "name": s["name"], "queued": len(self._queued(s["id"]))}
                        for s in self._rotation(room["id"]) if not s["hidden"]]
            options = self._options(room)
            # `limit` is the one in effect: the room's own, or the server's.
            options["limit"] = options["songs_per_singer"] or self.songs_per_singer
            # Which screen the sound comes from is for the host and the screens only; screens
            # hear it over their socket.
            del options["sound"]
            return {"code": room["code"], "name": room["name"], "paused": bool(room["paused"]),
                    "now_playing": current, "up_next": up_next, "rotation": rotation, "options": options}

    def queued_videos(self, code: str) -> list:
        """YouTube ids of every song still waiting in the room, so the server can fetch them ahead."""
        with self.lock:
            room = self._room(code)
            return [r["video_id"] for r in self.db.execute(
                "SELECT video_id FROM songs WHERE room_id = ? AND status = 'queued' AND video_id IS NOT NULL",
                (room["id"],))]

    def host_state(self, code: str, token: Optional[str]) -> dict:
        """Everything, including every singer's full list and hidden singers."""
        with self.lock:
            self.require_host(code, token)
            room = self._room(code)
            data = self.state(code)
            data["options"]["sound"] = self._options(room)["sound"]
            data["singers"] = [
                {"id": s["id"], "name": s["name"], "hidden": bool(s["hidden"]),
                 "songs": [self._song(r) for r in self._queued(s["id"])]}
                for s in self._rotation(room["id"])]
            return data

    def set_paused(self, code: str, token: Optional[str], paused: bool) -> None:
        with self.lock, self.db:
            self.require_host(code, token)
            self.db.execute("UPDATE rooms SET paused = ? WHERE code = ?", (int(paused), code.upper()))

    def set_options(self, code: str, token: Optional[str], changes: dict) -> None:
        """Host changes what guests may do. `songs_per_singer` None means the server's limit."""
        with self.lock, self.db:
            self.require_host(code, token)
            room = self._room(code)
            options = self._options(room)
            for key, value in changes.items():
                if key not in ROOM_OPTIONS:
                    continue
                if key == "songs_per_singer":
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                              or not 1 <= value <= 1000):
                        raise Error("Songs per guest must be between 1 and 1000")
                elif key == "sound":
                    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,40}|", value):
                        raise Error("Sound must come from one of the room's screens")
                elif not isinstance(value, bool):
                    raise Error(f"{key} must be true or false")
                options[key] = value
            self.db.execute("UPDATE rooms SET options = ? WHERE id = ?", (json.dumps(options), room["id"]))

    def sound(self, code: str) -> str:
        with self.lock:
            return self._options(self._room(code))["sound"]

    # -- screens: a screen counts once the host has let it in ---------------------

    def screen(self, code: str, display_id: str, key: Optional[str]) -> Optional[dict]:
        """The saved screen, if `key` is the one the host gave it."""
        with self.lock:
            row = self._one("SELECT * FROM screens WHERE room_id = ? AND display_id = ?",
                            self._room(code)["id"], display_id)
        if row is None or not key or not row["key_hash"] or not secrets.compare_digest(_hash(key), row["key_hash"]):
            return None
        return {"record": bool(row["record"])}

    def pair_screen(self, code: str, display_id: str) -> str:
        """Let a screen in with a fresh key, which replaces any key that id had before."""
        key = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("INSERT INTO screens (room_id, display_id, key_hash) VALUES (?, ?, ?) "
                            "ON CONFLICT (room_id, display_id) DO UPDATE SET key_hash = excluded.key_hash, record = 0",
                            (self._room(code)["id"], display_id, _hash(key)))
        return key

    def screen_record(self, code: str, display_id: str, value: Optional[bool] = None) -> bool:
        """Whether the host has recording on for a screen; with `value`, change it."""
        with self.lock, self.db:
            room_id = self._room(code)["id"]
            if value is not None:
                self.db.execute("INSERT INTO screens (room_id, display_id, record) VALUES (?, ?, ?) "
                                "ON CONFLICT (room_id, display_id) DO UPDATE SET record = excluded.record",
                                (room_id, display_id, int(value)))
            row = self._one("SELECT record FROM screens WHERE room_id = ? AND display_id = ?", room_id, display_id)
        return bool(row and row["record"])

    def forget_screen(self, code: str, display_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM screens WHERE room_id = ? AND display_id = ?",
                            (self._room(code)["id"], display_id))

    # -- the whole server, for the admin page ------------------------------------

    def rooms_overview(self) -> list:
        with self.lock:
            rows = self.db.execute(
                "SELECT code, name, created_at, COALESCE(touched_at, created_at) AS touched_at, "
                "(SELECT COUNT(*) FROM singers WHERE room_id = rooms.id) AS singers, "
                "(SELECT COUNT(*) FROM songs WHERE room_id = rooms.id AND status = 'queued') AS queued, "
                "(SELECT COUNT(*) FROM recordings WHERE room_id = rooms.id) AS recordings "
                "FROM rooms ORDER BY touched_at DESC").fetchall()
            return [dict(r) for r in rows]

    def delete_room(self, code: str) -> str:
        with self.lock, self.db:
            room = self._room(code)
            self._drop_claims(room_id=room["id"])
            self.db.execute("DELETE FROM rooms WHERE id = ?", (room["id"],))
            return room["code"]

    def load_settings(self) -> dict:
        with self.lock:
            return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}

    def save_setting(self, key: str, value) -> None:
        """Save a setting, or forget it with None."""
        with self.lock, self.db:
            if value is None:
                self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) "
                                "DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # -- library title fixes, and names for downloaded songs -----------------------

    def library_edits(self) -> dict:
        """Library song id -> (title, artist) an admin typed in, over what the file name says."""
        with self.lock:
            return {r["song"]: (r["title"], r["artist"]) for r in self.db.execute("SELECT * FROM library_edits")}

    def save_library_edit(self, song: str, title: Optional[str], artist: Optional[str] = None) -> None:
        """Save a fix, or forget it with title None."""
        self.save_library_edits([(song, title, artist)])

    def save_library_edits(self, edits: list) -> None:
        """Many (song, title, artist) fixes in one go; title None forgets that song's fix."""
        with self.lock, self.db:
            for song, title, artist in edits:
                if title is None:
                    self.db.execute("DELETE FROM library_edits WHERE song = ?", (song,))
                else:
                    self.db.execute("INSERT INTO library_edits (song, title, artist) VALUES (?, ?, ?) ON CONFLICT (song) "
                                    "DO UPDATE SET title = excluded.title, artist = excluded.artist", (song, title, artist or ""))

    def video_names(self, video_ids: list) -> dict:
        """What guests called each YouTube video when they queued it, newest name first."""
        with self.lock:
            names = {}
            for i in range(0, len(video_ids), 500):
                chunk = video_ids[i:i + 500]
                for r in self.db.execute(
                        f"SELECT video_id, title, artist FROM songs WHERE video_id IN ({','.join('?' * len(chunk))}) "
                        "ORDER BY created_at", chunk):
                    names[r["video_id"]] = {"title": r["title"], "artist": r["artist"]}
            return names

    # -- user accounts (passwords arrive already hashed) ---------------------------

    def users(self) -> list:
        with self.lock:
            rows = self.db.execute("SELECT id, name, role, disabled, created_at, seen_at FROM users "
                                   "ORDER BY name COLLATE NOCASE").fetchall()
            return [{**dict(r), "disabled": bool(r["disabled"])} for r in rows]

    def user(self, user_id: Optional[int] = None, name: Optional[str] = None) -> Optional[dict]:
        with self.lock:
            row = (self._one("SELECT * FROM users WHERE id = ?", user_id) if user_id is not None
                   else self._one("SELECT * FROM users WHERE name = ? COLLATE NOCASE", name))
            return dict(row) if row else None

    def add_user(self, name: str, role: str, password: str) -> int:
        name = _clean(name, "Name", self.name_limit)
        with self.lock, self.db:
            if self._one("SELECT 1 FROM users WHERE name = ? COLLATE NOCASE", name):
                raise Error("There's already an account with that name", 409)
            return self.db.execute("INSERT INTO users (name, role, password, created_at) VALUES (?, ?, ?, ?)",
                                   (name, role, password, time.time())).lastrowid

    def update_user(self, user_id: int, **fields) -> None:
        """Change any of name, role, password, disabled, seen_at."""
        assert set(fields) <= {"name", "role", "password", "disabled", "seen_at"}, fields
        if "name" in fields:
            fields["name"] = _clean(fields["name"], "Name", self.name_limit)
        with self.lock, self.db:
            if not self._one("SELECT 1 FROM users WHERE id = ?", user_id):
                raise Error("No such account", 404)
            if "name" in fields and self._one("SELECT 1 FROM users WHERE name = ? COLLATE NOCASE AND id != ?",
                                              fields["name"], user_id):
                raise Error("There's already an account with that name", 409)
            for key, value in fields.items():
                self.db.execute(f"UPDATE users SET {key} = ? WHERE id = ?", (value, user_id))

    def delete_user(self, user_id: int) -> None:
        with self.lock, self.db:
            if not self.db.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount:
                raise Error("No such account", 404)

    # -- where admins and hosts are signed in; user_id None is the built-in admin password --

    def start_staff_session(self, user_id: Optional[int], device: str = "") -> str:
        token, now = secrets.token_urlsafe(32), time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO staff_sessions (token_hash, user_id, device, created_at, seen_at) "
                            "VALUES (?, ?, ?, ?, ?)", (_hash(token), user_id, device[:80], now, now))
            self.db.execute("DELETE FROM staff_sessions WHERE user_id IS ? AND token_hash NOT IN "
                            "(SELECT token_hash FROM staff_sessions WHERE user_id IS ? ORDER BY seen_at DESC, rowid DESC LIMIT ?)",
                            (user_id, user_id, STAFF_SESSIONS))
            if user_id is not None:
                self.db.execute("UPDATE users SET seen_at = ? WHERE id = ?", (now, user_id))
        return token

    def staff_session(self, token: Optional[str]) -> Optional[dict]:
        """{"session": id, "user_id": id or None} for a live sign-in, or None."""
        if not token:
            return None
        now = time.time()
        with self.lock, self.db:
            row = self._one(f"SELECT {PUBLIC_ID} AS id, user_id, seen_at FROM staff_sessions "
                            "WHERE token_hash = ? AND seen_at > ? AND created_at > ?",
                            _hash(token), now - STAFF_SESSION_DAYS * 86400, now - STAFF_SESSION_MAX_DAYS * 86400)
            if row is None:
                return None
            if now - row["seen_at"] > 60:  # once a minute is plenty for "last used"
                self.db.execute("UPDATE staff_sessions SET seen_at = ? WHERE token_hash = ?", (now, _hash(token)))
                if row["user_id"] is not None:
                    self.db.execute("UPDATE users SET seen_at = ? WHERE id = ?", (now, row["user_id"]))
            return {"session": row["id"], "user_id": row["user_id"]}

    def end_staff_sessions(self, user_id: Optional[int] = None, session: Optional[str] = None,
                           token: Optional[str] = None, keep: Optional[str] = None) -> None:
        """Sign out one session (by id or token), or every one of an account but `keep`."""
        with self.lock, self.db:
            if token is not None:
                self.db.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (_hash(token),))
            elif session is not None:
                self.db.execute(f"DELETE FROM staff_sessions WHERE {PUBLIC_ID} = ?", (session,))
            else:
                self.db.execute(f"DELETE FROM staff_sessions WHERE user_id IS ? AND {PUBLIC_ID} IS NOT ?", (user_id, keep))

    def sessions(self) -> list:
        """Every live sign-in, staff and guest, newest use first."""
        now = time.time()
        with self.lock, self.db:
            self.db.execute("DELETE FROM staff_sessions WHERE seen_at <= ? OR created_at <= ?",
                            (now - STAFF_SESSION_DAYS * 86400, now - STAFF_SESSION_MAX_DAYS * 86400))
            self.db.execute("DELETE FROM guest_sessions WHERE created_at <= ?", (now - GUEST_SESSION_DAYS * 86400,))
            staff = self.db.execute(f"SELECT {PUBLIC_ID} AS id, 'staff' AS kind, user_id AS account, device, created_at, "
                                    "seen_at FROM staff_sessions")
            guests = self.db.execute(f"SELECT {PUBLIC_ID} AS id, 'guest' AS kind, guest_id AS account, device, created_at, "
                                     "COALESCE(seen_at, created_at) AS seen_at FROM guest_sessions")
            return sorted([dict(r) for r in [*staff, *guests]], key=lambda s: -s["seen_at"])

    # -- links that make an account, or pick a new password for one, without the admin knowing it --

    def add_invite(self, made_by: str, made_by_id: Optional[int], role: Optional[str] = None,
                   user_id: Optional[int] = None) -> str:
        """made_by_id None is the built-in admin."""
        assert (role is None) != (user_id is None)
        token, now = secrets.token_urlsafe(32), time.time()
        with self.lock, self.db:
            if user_id is not None:
                if not self._one("SELECT 1 FROM users WHERE id = ?", user_id):
                    raise Error("No such account", 404)
                self.db.execute("DELETE FROM invites WHERE user_id = ?", (user_id,))  # a new link replaces the last
            days = INVITE_DAYS if role else RESET_DAYS
            self.db.execute("INSERT INTO invites (token_hash, role, user_id, made_by, made_by_id, created_at, expires_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_hash(token), role, user_id, made_by, made_by_id, now, now + days * 86400))
        return token

    def invites(self) -> list:
        with self.lock, self.db:
            self.db.execute("DELETE FROM invites WHERE expires_at <= ?", (time.time(),))
            return [dict(r) for r in self.db.execute(
                "SELECT substr(i.token_hash, 1, 16) AS id, i.role, i.user_id, u.name, i.made_by, i.created_at, i.expires_at "
                "FROM invites i LEFT JOIN users u ON u.id = i.user_id ORDER BY i.created_at DESC")]

    def cancel_invite(self, invite_id: str) -> None:
        with self.lock, self.db:
            self.db.execute(f"DELETE FROM invites WHERE {PUBLIC_ID} = ?", (invite_id,))

    def drop_invites_by(self, user_id: Optional[int]) -> None:
        """Cancel every link someone made, when their password or access changes (None: the built-in admin)."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM invites WHERE made_by_id IS ?", (user_id,))

    def invite(self, token: str) -> Optional[dict]:
        """What a link is for: {"role"} for a new account, {"user_id", "name"} for a new password."""
        with self.lock:
            row = self._one("SELECT i.role, i.user_id, u.name FROM invites i LEFT JOIN users u ON u.id = i.user_id "
                            "WHERE i.token_hash = ? AND i.expires_at > ?", _hash(token or ""), time.time())
            return dict(row) if row else None

    def use_invite(self, token: str, password: str, name: str = "") -> int:
        """Make the account, or set its new password and sign it out everywhere. The link then stops working."""
        with self.lock, self.db:
            found = self.invite(token)
            if found is None:
                raise Error("That link has already been used or is out of date. Ask an admin for a new one.", 404)
            if found["role"]:
                user_id = self.add_user(name, found["role"], password)
            else:
                user_id = found["user_id"]
                self.update_user(user_id, password=password)
                self.db.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
                self.drop_invites_by(user_id)  # a new password ends the links they made, as it does anywhere else
            self.db.execute("DELETE FROM invites WHERE token_hash = ?", (_hash(token),))
            return user_id

    # -- guest accounts: optional, so a guest can get back to their spot from any phone, in any room --

    def add_guest(self, name: str, password: str) -> int:
        name = _clean(name, "Name", self.name_limit)
        with self.lock, self.db:
            if self._one("SELECT 1 FROM guests WHERE name = ? COLLATE NOCASE", name):
                raise Error("That name is taken. Sign in if it's yours, or pick another.", 409)
            return self.db.execute("INSERT INTO guests (name, password, created_at) VALUES (?, ?, ?)",
                                   (name, password, time.time())).lastrowid

    def guest(self, guest_id: Optional[int] = None, name: Optional[str] = None) -> Optional[dict]:
        row = self._one("SELECT * FROM guests WHERE id = ?", guest_id) if guest_id is not None else \
            self._one("SELECT * FROM guests WHERE name = ? COLLATE NOCASE", " ".join((name or "").split()))
        return dict(row) if row else None

    def guests(self) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT g.id, g.name, g.disabled, g.created_at, g.seen_at, "
                "(SELECT COUNT(*) FROM singers s WHERE s.guest_id = g.id) AS rooms FROM guests g ORDER BY g.name")]

    def update_guest(self, guest_id: int, **fields) -> None:
        """Change any of password, disabled. Turning an account off or changing its password signs it out everywhere."""
        assert set(fields) <= {"password", "disabled"}, fields
        with self.lock, self.db:
            if not self._one("SELECT 1 FROM guests WHERE id = ?", guest_id):
                raise Error("No such account", 404)
            for key, value in fields.items():
                self.db.execute(f"UPDATE guests SET {key} = ? WHERE id = ?", (value, guest_id))
            if fields.get("disabled") or "password" in fields:
                self.end_guest_sessions(guest_id)

    def end_guest_sessions(self, guest_id: int) -> None:
        """Signed out everywhere: the account, and every phone signed in as one of its singers."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM guest_sessions WHERE guest_id = ?", (guest_id,))
            singers = [r["id"] for r in self.db.execute("SELECT id FROM singers WHERE guest_id = ?", (guest_id,))]
            self.db.executemany("DELETE FROM singer_tokens WHERE singer_id = ?", [(i,) for i in singers])
            self.db.executemany("UPDATE singers SET token_hash = ? WHERE id = ?",
                                [(_hash(secrets.token_urlsafe(32)), i) for i in singers])
            self._drop_claims(singer_ids=singers)

    def delete_guest(self, guest_id: int) -> None:
        """The account goes; the singers it was in rooms as stay, just without it."""
        with self.lock, self.db:
            if not self.db.execute("DELETE FROM guests WHERE id = ?", (guest_id,)).rowcount:
                raise Error("No such account", 404)

    def start_guest_session(self, guest_id: int, device: str = "") -> str:
        token, now = secrets.token_urlsafe(32), time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO guest_sessions (token_hash, guest_id, device, created_at, seen_at) VALUES (?, ?, ?, ?, ?)",
                            (_hash(token), guest_id, device[:80], now, now))
            self.db.execute("DELETE FROM guest_sessions WHERE guest_id = ? AND token_hash NOT IN "
                            "(SELECT token_hash FROM guest_sessions WHERE guest_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                            (guest_id, guest_id, GUEST_SESSIONS))
            self.db.execute("UPDATE guests SET seen_at = ? WHERE id = ?", (time.time(), guest_id))
        return token

    def guest_session(self, token: Optional[str]) -> Optional[dict]:
        """The account a guest session token belongs to, or None."""
        if not token:
            return None
        now = time.time()
        with self.lock, self.db:
            row = self._one("SELECT g.*, s.seen_at AS used FROM guest_sessions s JOIN guests g ON g.id = s.guest_id "
                            "WHERE s.token_hash = ? AND g.disabled = 0 AND s.created_at > ?",
                            _hash(token), now - GUEST_SESSION_DAYS * 86400)
            if row and now - (row["used"] or 0) > 60:
                self.db.execute("UPDATE guest_sessions SET seen_at = ? WHERE token_hash = ?", (now, _hash(token)))
            return {"id": row["id"], "name": row["name"]} if row else None

    def end_guest_session(self, token: Optional[str] = None, session: Optional[str] = None) -> None:
        with self.lock, self.db:
            if session is not None:
                self.db.execute(f"DELETE FROM guest_sessions WHERE {PUBLIC_ID} = ?", (session,))
            else:
                self.db.execute("DELETE FROM guest_sessions WHERE token_hash = ?", (_hash(token or ""),))

    def guest_session_info(self, guest_id: int) -> dict:
        """What a signed-in guest sees about their account: its name and the rooms they're in."""
        with self.lock:
            guest = self._one("SELECT name FROM guests WHERE id = ?", guest_id)
            rooms = self.db.execute("SELECT r.code, r.name FROM singers s JOIN rooms r ON r.id = s.room_id "
                                    "WHERE s.guest_id = ? ORDER BY r.touched_at DESC LIMIT 20", (guest_id,))
            return {"name": guest["name"] if guest else "", "rooms": [dict(r) for r in rooms]}


    # -- singers -----------------------------------------------------------------

    def _device(self, singer_id: int) -> str:
        """Signs one more phone in as this singer, keeping the newest few."""
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO singer_tokens (token_hash, singer_id, created_at) VALUES (?, ?, ?)",
                        (_hash(token), singer_id, time.time()))
        self.db.execute("DELETE FROM singer_tokens WHERE singer_id = ? AND token_hash NOT IN "
                        "(SELECT token_hash FROM singer_tokens WHERE singer_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                        (singer_id, singer_id, DEVICES))
        return token

    def join(self, code: str, name: str, guest: Optional[dict] = None, by_host: bool = False,
             token: Optional[str] = None) -> dict:
        """A guest joins, or the host adds someone by hand (past the room's guest limit). A
        guest with an account joins under its name, and joining again finds their old spot.
        Signing in on a phone that's already in the room (`token`) ties that spot to the account."""
        if guest:
            name = guest["name"]
        name = _clean(name, "Name", self.name_limit)
        with self.lock, self.db:
            room = self._room(code)
            if guest:
                mine = self._one("SELECT id, name FROM singers WHERE room_id = ? AND guest_id = ?", room["id"], guest["id"])
                if not mine and token:
                    try:
                        role, singer_id = self.auth(code, token)
                    except Error:
                        role = None
                    if role == "singer" and self.db.execute(
                            "UPDATE singers SET guest_id = ? WHERE id = ? AND guest_id IS NULL",
                            (guest["id"], singer_id)).rowcount:
                        mine = self._one("SELECT id, name FROM singers WHERE id = ?", singer_id)
                if mine:
                    return {"singer_id": mine["id"], "name": mine["name"], "token": self._device(mine["id"])}
            if self._one("SELECT 1 FROM singers WHERE room_id = ? AND name = ? COLLATE NOCASE",
                         room["id"], name):
                raise Error("Someone in this room already has that name. If that's you, tap Tie this spot to an account on the "
                            "phone you joined with, or ask the host to sign this phone in from your name." if guest else
                            "Someone in this room already has that name", 409)
            if not by_host and self._one("SELECT COUNT(*) AS n FROM singers WHERE room_id = ?", room["id"])["n"] >= self.singers_per_room:
                raise Error("This room is full. Ask the host to remove someone who's gone home.", 409)
            last =self._one("SELECT COALESCE(MAX(position), -1) AS p FROM singers WHERE room_id = ?",
                             room["id"])["p"]
            # Sign-ins live in singer_tokens; token_hash only still signs in singers from before.
            cur = self.db.execute(
                "INSERT INTO singers (room_id, name, token_hash, position, guest_id) VALUES (?, ?, ?, ?, ?)",
                (room["id"], name, _hash(secrets.token_urlsafe(32)), last + 1, guest["id"] if guest else None))
            # Someone the host adds signs their phone in later, with a "this is me" code.
            return {"singer_id": cur.lastrowid, "name": name, "token": None if by_host else self._device(cur.lastrowid)}

    def claim_link(self, code: str, token: Optional[str], singer_id: int) -> str:
        """A one-time code that signs another phone in as this singer, for the host or the
        singer themselves to show as a QR code. It works once, for a few minutes."""
        with self.lock:
            self._may_edit(code, token, singer_id)
            room = self._room(code)
            singer = self._singer(room, singer_id)
            self._drop_claims(singer_ids=[singer["id"]])  # one live link per singer: a new one replaces it
            claim = secrets.token_urlsafe(24)
            self.claims[_hash(claim)] = (room["id"], singer["id"], time.time() + CLAIM_SECONDS)
            return claim

    def claim(self, code: str, claim: str) -> dict:
        with self.lock, self.db:
            room = self._room(code)
            found = self.claims.get(_hash(claim or ""))
            if not found or found[0] != room["id"] or found[2] < time.time():
                raise Error("That link has been used or has run out. Ask for a new one.", 403)
            del self.claims[_hash(claim)]
            singer = self._one("SELECT id, name FROM singers WHERE id = ? AND room_id = ?", found[1], room["id"])
            if singer is None:
                raise Error("That singer isn't in this room anymore", 404)
            return {"singer_id": singer["id"], "name": singer["name"], "token": self._device(singer["id"])}

    def me(self, code: str, token: Optional[str]) -> dict:
        with self.lock:
            role, singer_id = self.auth(code, token)
            if role == "host":
                return {"role": "host"}
            singer = self._singer(self._room(code), singer_id)
            return {"role": "singer", "id": singer["id"], "name": singer["name"], "account": singer["guest_id"] is not None,
                    "songs": [self._song(r) for r in self._queued(singer_id)]}

    def reorder_rotation(self, code: str, token: Optional[str], order: list) -> None:
        with self.lock, self.db:
            self.require_host(code, token)
            self._reorder("singers", self._rotation(self._room(code)["id"]), order)

    def set_hidden(self, code: str, token: Optional[str], singer_id: int, hidden: bool) -> None:
        with self.lock, self.db:
            self.require_host(code, token)
            singer = self._singer(self._room(code), singer_id)
            self.db.execute("UPDATE singers SET hidden = ? WHERE id = ?", (int(hidden), singer["id"]))

    def remove_singer(self, code: str, token: Optional[str], singer_id: int) -> list:
        """Remove a singer, their songs and their recordings. Returns the recordings' file names so
        the caller can delete the files too."""
        with self.lock, self.db:
            self.require_host(code, token)
            room = self._room(code)
            singer = self._singer(room, singer_id)
            current = room["current_song_id"]
            if current and self._one("SELECT 1 FROM songs WHERE id = ? AND singer_id = ?", current, singer["id"]):
                self.db.execute("UPDATE rooms SET current_song_id = NULL WHERE id = ?", (room["id"],))
            if room["last_singer_id"] == singer["id"]:
                # The turn passes on from where they stood, not back to the top of the rotation.
                ids = [s["id"] for s in self._rotation(room["id"])]
                before = ids[ids.index(singer["id"]) - 1] if len(ids) > 1 else None
                self.db.execute("UPDATE rooms SET last_singer_id = ? WHERE id = ?", (before, room["id"]))
            self._drop_claims(singer_ids=[singer["id"]])
            files = [r["file"] for r in self.db.execute(
                "SELECT recordings.file FROM recordings JOIN songs ON songs.id = recordings.song_id "
                "WHERE songs.singer_id = ?", (singer["id"],))]
            self.db.execute("DELETE FROM recordings WHERE song_id IN (SELECT id FROM songs WHERE singer_id = ?)",
                            (singer["id"],))
            self.db.execute("DELETE FROM singers WHERE id = ?", (singer["id"],))
            return files

    # -- songs -------------------------------------------------------------------

    def add_song(self, code: str, token: Optional[str], title: str, artist: str,
                 singer_id: Optional[int] = None, video_id: Optional[str] = None,
                 media: Optional[str] = None) -> dict:
        title = _clean(title, "Song", self.text_limit)
        artist = _clean(artist, "Artist", self.text_limit, optional=True)
        with self.lock, self.db:
            room = self._room(code)
            role, me = self.auth(code, token)
            options = self._options(room)
            if role == "singer":
                if room["paused"]:
                    raise Error("Requests are paused right now", 423)
                if video_id and not options["youtube"]:
                    raise Error("The host has turned off YouTube songs in this room", 403)
                if media and not options["library"]:
                    raise Error("The host has turned off the song library in this room", 403)
                singer_id = me
            elif singer_id is None:
                raise Error("Pick which singer this song is for")
            self._singer(room, singer_id)
            queued = self._queued(singer_id)
            limit = options["songs_per_singer"] or self.songs_per_singer
            if role == "singer" and len(queued) >= limit:
                raise Error(f"You can queue up to {limit} songs at a time", 409)
            position = queued[-1]["position"] + 1 if queued else 0
            cur = self.db.execute(
                "INSERT INTO songs (room_id, singer_id, title, artist, video_id, media, position, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (room["id"], singer_id, title, artist, video_id, media, position, time.time()))
        return {"id": cur.lastrowid}

    def _queued_song(self, code: str, song_id: int) -> sqlite3.Row:
        song = self._one("SELECT * FROM songs WHERE id = ? AND room_id = ? AND status = 'queued'",
                         song_id, self._room(code)["id"])
        if song is None:
            raise Error("Song not found", 404)
        return song

    def remove_song(self, code: str, token: Optional[str], song_id: int) -> None:
        with self.lock, self.db:
            song = self._queued_song(code, song_id)
            self._may_edit(code, token, song["singer_id"])
            self.db.execute("DELETE FROM songs WHERE id = ?", (song_id,))

    def reorder_songs(self, code: str, token: Optional[str], singer_id: int, order: list) -> None:
        with self.lock, self.db:
            self._singer(self._room(code), singer_id)
            self._may_edit(code, token, singer_id)
            self._reorder("songs", self._queued(singer_id), order)

    def _reorder(self, table: str, rows: list, order: list) -> None:
        """Put `order` ids first, keep anything it left out in its old order after them."""
        ids = [r["id"] for r in rows]
        wanted = [i for i in dict.fromkeys(order) if i in ids]
        final = wanted + [i for i in ids if i not in wanted]
        for position, row_id in enumerate(final):
            self.db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (position, row_id))

    # -- the show ----------------------------------------------------------------

    def next(self, code: str, token: Optional[str], after: Optional[int] = None) -> None:
        """Finish the current song and start the next singer's top song.

        With `after`, only move on if that song is still the one playing (0: if nothing is), so a
        tap that crosses a screen moving on by itself doesn't skip the next singer too."""
        with self.lock:
            self.require_host(code, token)
            self._advance(code, after)

    def song_ended(self, code: str, song_id: int) -> None:
        """A screen the host let in played `song_id` to the end: on to the next singer, unless the
        show already moved on. Screens can't skip anything else."""
        with self.lock:
            self._advance(code, song_id)

    def _advance(self, code: str, after: Optional[int]) -> None:
        with self.lock, self.db:
            room = self._room(code)
            if after is not None and (room["current_song_id"] or 0) != after:
                return
            if room["current_song_id"]:
                self.db.execute("UPDATE songs SET status = 'done', ended_at = ? WHERE id = ?",
                                (time.time(), room["current_song_id"]))
                self.db.execute("UPDATE rooms SET previous_song_id = ? WHERE id = ?", (room["current_song_id"], room["id"]))
            song = self._pick_next(room)
            if song is None:
                self.db.execute("UPDATE rooms SET current_song_id = NULL WHERE id = ?", (room["id"],))
                return
            self.db.execute("UPDATE songs SET status = 'playing' WHERE id = ?", (song["id"],))
            self.db.execute("UPDATE rooms SET current_song_id = ?, last_singer_id = ? WHERE id = ?",
                            (song["id"], song["singer_id"], room["id"]))

    # -- recordings --------------------------------------------------------------

    def recordable_song(self, code: str, song_id: int) -> None:
        """A screen may only save a recording of the song that's on, or one that ended in the last
        few minutes. Screens upload when the song changes, so a host tapping Next twice quickly,
        or a slow upload, doesn't lose the take."""
        with self.lock:
            room = self._room(code)
            if song_id != room["current_song_id"] and not self._one(
                    "SELECT 1 FROM songs WHERE id = ? AND room_id = ? AND status = 'done' AND ended_at > ?",
                    song_id, room["id"], time.time() - TAKE_WINDOW):
                raise Error("Recordings can only be saved for the song that just played", 404)

    def recordings_bytes(self, code: Optional[str] = None) -> int:
        """Space the recordings take, in one room or on the whole server."""
        with self.lock:
            if code is None:
                return self._one("SELECT COALESCE(SUM(size), 0) AS n FROM recordings")["n"]
            return self._one("SELECT COALESCE(SUM(size), 0) AS n FROM recordings WHERE room_id = ?",
                             self._room(code)["id"])["n"]

    def add_recording(self, code: str, song_id: int, file: str, size: int) -> int:
        with self.lock, self.db:
            self.recordable_song(code, song_id)
            cur = self.db.execute(
                "INSERT INTO recordings (room_id, song_id, file, size, created_at) VALUES (?, ?, ?, ?, ?)",
                (self._room(code)["id"], song_id, file, size, time.time()))
        return cur.lastrowid

    def recordings(self, code: str, token: Optional[str]) -> list:
        """The host sees every recording; a singer sees their own."""
        with self.lock:
            role, me = self.auth(code, token)
            rows = self.db.execute(
                "SELECT recordings.*, songs.title, songs.artist, songs.singer_id, singers.name AS singer "
                "FROM recordings JOIN songs ON songs.id = recordings.song_id "
                "JOIN singers ON singers.id = songs.singer_id "
                "WHERE recordings.room_id = ? ORDER BY recordings.created_at DESC",
                (self._room(code)["id"],)).fetchall()
            return [{"id": r["id"], "title": r["title"], "artist": r["artist"], "singer": r["singer"],
                     "size": r["size"], "created_at": r["created_at"]}
                    for r in rows if role == "host" or r["singer_id"] == me]

    def _recording(self, code: str, token: Optional[str], recording_id: int) -> sqlite3.Row:
        row = self._one("SELECT recordings.*, songs.singer_id FROM recordings JOIN songs "
                        "ON songs.id = recordings.song_id WHERE recordings.id = ? AND recordings.room_id = ?",
                        recording_id, self._room(code)["id"])
        if row is None:
            raise Error("Recording not found", 404)
        role, me = self.auth(code, token)
        if role != "host" and me != row["singer_id"]:
            raise Error("That isn't your recording", 403)
        return row

    def recording_file(self, code: str, token: Optional[str], recording_id: int) -> str:
        with self.lock:
            return self._recording(code, token, recording_id)["file"]

    def remove_recording(self, code: str, token: Optional[str], recording_id: int) -> str:
        """Forget a recording and return its file name so the caller can delete the file."""
        with self.lock, self.db:
            row = self._recording(code, token, recording_id)
            self.db.execute("DELETE FROM recordings WHERE id = ?", (row["id"],))
            return row["file"]
