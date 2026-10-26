"""Server settings: each one can come from the admin page, an environment variable or its default.

A value saved on the admin page wins over the environment, so the page always shows what's
really in effect. Resetting a setting on the page falls back to the environment again.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .downloads import DEFAULT_FORMAT
from .store import Error, Store

# key, type, default, group, label, help. The env var is KARAQUEUE_ + the key in capitals.
SETTINGS = [
    ("songs_per_singer", int, 10, "Rooms", "Songs per guest",
     "How many songs a guest can have queued at once. The host isn't limited. Rooms can set their own."),
    ("name_limit", int, 32, "Rooms", "Longest singer name", "In characters."),
    ("code_length", int, 6, "Rooms", "Room code length",
     "New rooms only. Between 5 and 12. Longer codes are harder to guess."),
    ("rate_limit", float, 1.0, "Rooms", "Rate limits",
     "Caps how fast one address can guess room codes, start rooms, join and search. "
     "2 doubles every cap, 0 turns them off. Wrong passwords have their own cap. Raise it if lots of guests share one internet connection."),
    ("room_days", float, 30.0, "Rooms", "Delete quiet rooms after (days)",
     "Rooms nobody has changed in this long go, with their recordings. Open rooms are kept. 0 keeps them forever."),
    ("public_url", str, "", "Rooms", "Public address",
     "The address guests should use, for the join link and QR code. Leave empty to use whatever address the host opened."),
    ("new_rooms", str, "accounts", "Rooms", "Who can start rooms",
     "Only people with an account (add them under Access), or anyone who can reach the server. "
     "Guests never need an account to join."),
    ("singers_per_room", int, 100, "Rooms", "Singers per room",
     "Guests who can join one room. The host can still add more by hand."),
    ("screens_per_address", int, 4, "Rooms", "Screens asking to join from one network",
     "How many screens on one internet connection can wait for the host to let them in at once. "
     "Raise it if you set up lots of TVs at a time."),
    ("media_dir", str, "", "Song library", "Library folders",
     "Folders of karaoke songs on the server, separated by \":\". Empty turns the library off. "
     "The Library tab adds and removes them one at a time."),
    ("save_dir", str, "", "Song library", "Folder for saved songs",
     "Where songs saved from YouTube go for good, named \"Artist - Title [video id]\". They join the library. "
     "Empty puts them in a YouTube folder inside the first library folder."),
    ("who_saves", str, "hosts", "Song library", "Who can save YouTube songs",
     "Admins save songs into the library from the Library tab. Hosts too lets anyone hosting a room save "
     "them from the host page, up to the saves per hour limit. Needs yt-dlp."),
    ("saves_per_hour", int, 10, "Song library", "Saves per room per hour",
     "YouTube songs one room's host can save into the library in an hour. Admins aren't limited."),
    ("save_max_mb", int, 500, "Song library", "Biggest saved song (MB)",
     "Songs bigger than this aren't saved. A four minute 1080p video is about 60."),
    ("song_lookup", str, "musicbrainz", "Song library", "Song info lookup",
     "Where Look up, when you fix a song's name, finds its real title and artist. MusicBrainz is free and needs "
     "no key. Off means the server never asks anyone."),
    ("conversions", int, 2, "Song library", "Old videos converting at once",
     "AVI, MPG and the like get converted while they play, which takes a lot of CPU. Each screen playing "
     "one uses a slot; past this many, screens are told to try again shortly."),
    ("youtube_api_key", str, "", "YouTube", "YouTube API key",
     "A YouTube Data API key for song search. Without one, search uses yt-dlp if it's set up."),
    ("downloads_dir", str, "", "YouTube", "Downloads folder",
     "Where yt-dlp keeps downloaded songs. Empty turns downloading off."),
    ("downloads_max_gb", float, 10.0, "YouTube", "Downloads folder size (GB)",
     "The songs played longest ago go first."),
    ("downloads_per_hour", int, 200, "YouTube", "New downloads per room per hour",
     "Songs not on the server yet that one room can download in an hour. Songs past that still queue and download later."),
    ("ytdlp", str, "yt-dlp", "YouTube", "yt-dlp command",
     "If it isn't on the PATH. Set by KARAQUEUE_YTDLP only, so the admin page can't make the server run a program."),
    ("ytdlp_format", str, DEFAULT_FORMAT, "YouTube", "yt-dlp format", "A yt-dlp -f format string."),
    ("recordings_dir", str, "", "Recordings", "Recordings folder",
     "Where recorded performances are kept. Empty turns recording off."),
    ("recording_max_mb", int, 100, "Recordings", "Biggest recording (MB)", "An hour of singing is about 30."),
    ("recordings_room_mb", int, 1000, "Recordings", "Recordings per room (MB)",
     "Once a room's takes add up to this, it saves no more until some are deleted."),
    ("recordings_max_gb", float, 20.0, "Recordings", "All recordings (GB)",
     "The most every room's takes can add up to. The server also always leaves 1 GB of the disk free."),
]
SECRET = {"youtube_api_key"}
# Shown on the admin page but only changed in the environment: whoever has the admin page
# shouldn't get to pick a program for the server to run.
ENV_ONLY = {"ytdlp"}
# Folders the admin page can point at. They have to sit inside KARAQUEUE_ALLOWED_FOLDERS, or
# inside the folders the environment already sets, so the admin page can't read or delete
# files anywhere else on the server.
FOLDERS = ("media_dir", "save_dir", "downloads_dir", "recordings_dir")
# Settings that are one of a few values: value -> label.
CHOICES = {"new_rooms": {"accounts": "Account holders", "anyone": "Anyone"},
           "who_saves": {"admins": "Admins", "hosts": "Hosts too"},
           "song_lookup": {"musicbrainz": "MusicBrainz", "off": "Off"}}
ROLES = ("admin", "host")
LIMITS = {"songs_per_singer": (1, 1000), "name_limit": (1, 200), "code_length": (5, 12),
          "rate_limit": (0, 100), "room_days": (0, 36500), "downloads_max_gb": (0.1, 100000), "recording_max_mb": (1, 10000),
          "recordings_room_mb": (1, 1000000), "recordings_max_gb": (0.1, 100000),
          "singers_per_room": (1, 10000), "screens_per_address": (1, 12), "conversions": (1, 64), "downloads_per_hour": (1, 10000),
          "saves_per_hour": (1, 1000), "save_max_mb": (1, 100000)}
# Read once at startup, so they're shown on the admin page but changed only in the environment.
STARTUP = [("db", "karaqueue.db"), ("host", "0.0.0.0"), ("port", "8000"), ("trusted_proxies", "127.0.0.1"),
           ("allowed_folders", "the library, downloads and recordings folders set in the environment")]
SPEC = {s[0]: s for s in SETTINGS}
PASSWORD_KEY = "admin:password"
ENV_PASSWORD_KEY = "admin:env-password"  # a hash of the last KARAQUEUE_ADMIN_PASSWORD, to notice a new one
LINK_KEY = "media:key"  # signs the links screens play songs from
RULES_KEY = "library:rules"  # folder path -> naming rule, set on the Library tab


def env_name(key: str) -> str:
    return "KARAQUEUE_" + key.upper()


def _coerce(key: str, value: Any) -> Any:
    kind = SPEC[key][1]
    try:
        if kind is str:
            value = str(value).strip()
            if len(value) > 2000:
                raise ValueError
        else:
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
            if kind is int:
                if not value.is_integer():
                    raise ValueError
                value = int(value)
    except (TypeError, ValueError):
        raise Error(f"{SPEC[key][4]} needs a {'whole number' if kind is int else 'number' if kind is float else 'shorter value'}")
    if key in LIMITS:
        low, high = LIMITS[key]
        if not low <= value <= high:
            raise Error(f"{SPEC[key][4]} must be between {low:g} and {high:g}")
    if key in CHOICES and value not in CHOICES[key]:
        raise Error(f"{SPEC[key][4]} must be one of: {', '.join(CHOICES[key])}")
    if key == "public_url":
        value = value.rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise Error("The public address must start with http:// or https://")
    return value


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise Error("Use at least 8 characters")
    if len(password) > 1000:
        raise Error("That password is too long")
    salt = secrets.token_bytes(16)
    return salt.hex() + "$" + _scrypt(password, salt)


def password_matches(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    return secrets.compare_digest(_scrypt(password, bytes.fromhex(salt)), digest)


class Settings:
    def __init__(self, store: Store, env=os.environ, setup_file: Optional[Path] = None):
        self.store = store
        self.env = env
        self.saved = store.load_settings()
        # With no admin password yet, the one-time setup link goes in a file only someone on the
        # server can read, never in the log. It stays the same across restarts until it's used.
        self.setup_file = setup_file
        self.setup_token: Optional[str] = None
        if not self.env.get("KARAQUEUE_ADMIN_PASSWORD") and PASSWORD_KEY not in self.saved:
            self.setup_token = self._read_setup_file() or secrets.token_urlsafe(24)
            self._write_setup_file()
        # Sign-ins outlast a restart, so a new KARAQUEUE_ADMIN_PASSWORD signs out everyone using the old one.
        env_password = self.env.get("KARAQUEUE_ADMIN_PASSWORD")
        if env_password and (ENV_PASSWORD_KEY not in self.saved
                             or not password_matches(env_password, self.saved[ENV_PASSWORD_KEY])):
            store.end_staff_sessions(None)
            store.drop_invites_by(None)
            salt = secrets.token_bytes(16)
            self.saved[ENV_PASSWORD_KEY] = salt.hex() + "$" + _scrypt(env_password, salt)
            store.save_setting(ENV_PASSWORD_KEY, self.saved[ENV_PASSWORD_KEY])
        elif not env_password and ENV_PASSWORD_KEY in self.saved:
            # It was taken out: whoever signed in with it is signed out, and putting it back later counts as new.
            store.end_staff_sessions(None)
            store.drop_invites_by(None)
            store.save_setting(ENV_PASSWORD_KEY, None)
            del self.saved[ENV_PASSWORD_KEY]
        # Kept in the database so song links still work after a restart, mid-song.
        if LINK_KEY not in self.saved:
            self.saved[LINK_KEY] = secrets.token_hex(32)
            store.save_setting(LINK_KEY, self.saved[LINK_KEY])
        self.link_key = bytes.fromhex(self.saved[LINK_KEY])

    def _read_setup_file(self) -> Optional[str]:
        try:
            token = self.setup_file.read_text().strip().rpartition("#setup=")[2] if self.setup_file else ""
        except OSError:
            return None
        return token if len(token) >= 32 else None

    def _write_setup_file(self) -> None:
        if not self.setup_file:
            return
        link = f"{self.get('public_url')}/admin#setup={self.setup_token}"
        try:
            fd = os.open(self.setup_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(link + "\n")
        except OSError:
            pass  # then only KARAQUEUE_ADMIN_PASSWORD can set up the admin, which is fine

    # -- folders the admin page may use -----------------------------------------

    def allowed_folders(self) -> list:
        raw = self.env.get("KARAQUEUE_ALLOWED_FOLDERS")
        if raw:
            parts = raw.split(os.pathsep)
        else:
            parts = [f for key in FOLDERS for f in (self.env.get(env_name(key)) or "").split(os.pathsep)]
        return [Path(p.strip()).resolve() for p in parts if p.strip()]

    def folder_allowed(self, folder: str) -> bool:
        path = Path(folder).resolve()
        return any(path == root or path.is_relative_to(root) for root in self.allowed_folders())

    def folder_problem(self, key: str, value: str) -> Optional[Error]:
        """Why the admin page can't set this folder setting to `value`, or None if it can."""
        folders = [f.strip() for f in value.split(os.pathsep) if f.strip()]
        if value.strip() and not folders:
            return Error("Type the full path of a folder, like /srv/karaoke")
        if key != "media_dir" and os.pathsep in value:
            return Error(f"{SPEC[key][4]} takes one folder")
        for folder in folders:
            # A relative path would land wherever the server happened to start.
            if not os.path.isabs(folder):
                return Error(f"Type the full path of the folder, like /srv/karaoke, not {folder}")
            if not self.folder_allowed(folder):
                return Error(f"The server doesn't allow {folder}. Folders have to be inside "
                             "KARAQUEUE_ALLOWED_FOLDERS, or inside the folders set in the environment.", 403)
        return None

    def _saved_ok(self, key: str) -> bool:
        # A value saved before these rules (or written to the database by hand) is ignored
        # rather than trusted.
        if key in ENV_ONLY:
            return False
        if key in FOLDERS:
            return self.folder_problem(key, str(self.saved[key])) is None
        try:
            _coerce(key, self.saved[key])  # still in range, like a room code length saved before the minimum went up
        except Error:
            return False
        return True

    def ignored(self) -> list:
        """Values saved on the admin page that the rules above now ignore, to warn about at startup."""
        return [k for k in SPEC if k in self.saved and not self._saved_ok(k)]

    def source(self, key: str) -> str:
        if key in self.saved and self._saved_ok(key):
            return "admin"
        return "env" if self.env.get(env_name(key)) not in (None, "") else "default"

    def get(self, key: str) -> Any:
        if key in self.saved and self._saved_ok(key):
            return self.saved[key]
        raw = self.env.get(env_name(key))
        if raw not in (None, ""):
            try:
                return _coerce(key, raw)
            except Error:
                pass  # a bad env value falls back to the default rather than crashing a request
        return SPEC[key][2]

    def set(self, key: str, value: Any) -> None:
        if key not in SPEC:
            raise Error("No such setting", 404)
        if key in ENV_ONLY:
            raise Error(f"{SPEC[key][4]} is set by {env_name(key)} only, change it there", 409)
        value = _coerce(key, value)
        problem = self.folder_problem(key, value) if key in FOLDERS else None
        if problem:
            raise problem
        if key in FOLDERS and key != "media_dir":
            value = value.strip()
        if key == "media_dir":
            # Catch a typo here, not later when a guest's request fails.
            folders = [f.strip() for f in value.split(os.pathsep) if f.strip()]
            for folder in folders:
                if not Path(folder).is_dir():
                    raise Error(f"There's no folder at {folder} on the server")
            value = os.pathsep.join(dict.fromkeys(folders))  # no repeats
        elif key.endswith("_dir") and value:
            path = Path(value)
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise Error(f"Can't use {value}: {exc.strerror or exc}")
        self.store.save_setting(key, value)
        self.saved[key] = value

    def reset(self, key: str) -> None:
        if key not in SPEC:
            raise Error("No such setting", 404)
        self.store.save_setting(key, None)
        self.saved.pop(key, None)

    def describe(self) -> list:
        out = []
        for key, kind, default, group, label, help_text in SETTINGS:
            value = self.get(key)
            item = {"key": key, "type": kind.__name__, "group": group, "label": label, "help": help_text,
                    "env": env_name(key), "source": self.source(key), "secret": key in SECRET,
                    "env_only": key in ENV_ONLY}
            if key in SECRET:
                item["value"], item["default"] = bool(value), False  # set or not set, never the value
            else:
                item["value"], item["default"] = value, default
            if key in LIMITS:
                item["min"], item["max"] = LIMITS[key]
            if key in CHOICES:
                item["choices"] = CHOICES[key]
            out.append(item)
        return out

    def startup(self) -> list:
        return [{"env": env_name(k), "value": self.env.get(env_name(k)) or default} for k, default in STARTUP]

    # -- admin sign-in -----------------------------------------------------------

    def password_from_env(self) -> bool:
        return bool(self.env.get("KARAQUEUE_ADMIN_PASSWORD"))

    def check_password(self, password: str) -> bool:
        env = self.env.get("KARAQUEUE_ADMIN_PASSWORD")
        if env:
            return secrets.compare_digest(password.encode(), env.encode())
        saved = self.saved.get(PASSWORD_KEY)
        return bool(saved) and password_matches(password, saved)

    def set_password(self, password: str, by: Optional[dict] = None) -> None:
        if self.password_from_env():
            raise Error("The admin password is set by KARAQUEUE_ADMIN_PASSWORD, change it there", 409)
        value = hash_password(password)
        self.store.save_setting(PASSWORD_KEY, value)
        self.saved[PASSWORD_KEY] = value
        # Everyone signed in with the old password is signed out, except whoever changed it.
        self.store.end_staff_sessions(None, keep=by and by["id"] is None and by["session"] or None)
        self.store.drop_invites_by(None)
        self.setup_token = None
        if self.setup_file:
            self.setup_file.unlink(missing_ok=True)

    # -- sign-ins: the built-in admin (password only) or an account (name and password) --

    def log_in(self, name: Optional[str], password: str, device: str = "") -> Optional[str]:
        """A session token, or None for a wrong name or password."""
        if not name:
            return self.store.start_staff_session(None, device) if self.check_password(password) else None
        user = self.store.user(name=name.strip())
        if user is None:
            _scrypt(password, bytes(16))  # takes as long as a real check, so timing can't tell which names exist
            return None
        if not password_matches(password, user["password"]):
            return None
        if user["disabled"]:
            raise Error("This account is turned off. Ask an admin to turn it back on.", 403)
        return self.store.start_staff_session(user["id"], device)

    def whoami(self, token: Optional[str]) -> Optional[dict]:
        found = self.store.staff_session(token)
        if found is None:
            return None
        if found["user_id"] is None:
            return {"id": None, "name": "admin", "role": "admin", "session": found["session"]}
        user = self.store.user(found["user_id"])
        if user is None or user["disabled"]:
            return None
        return {"id": user["id"], "name": user["name"], "role": user["role"], "session": found["session"]}

    def accept_invite(self, token: str, password: str, name: str = "", device: str = "") -> str:
        """Use an invite or new-password link; signs in on this device."""
        found = self.store.invite(token)
        if found is None:  # before the slow password hash, so made-up links cost nothing
            raise Error("That link has already been used or is out of date. Ask an admin for a new one.", 404)
        user = self.store.user(found["user_id"]) if found["user_id"] is not None else None
        if user is not None and user["disabled"]:  # (deleted meanwhile: the link went with it, use_invite says so)
            raise Error("This account is turned off. Ask an admin to turn it back on.", 403)
        user_id = self.store.use_invite(token, hash_password(password), name)
        return self.store.start_staff_session(user_id, device)

    def require(self, token: Optional[str], role: str = "admin") -> dict:
        """The signed-in person, if they're allowed `role` (an admin may do anything a host may)."""
        who = self.whoami(token)
        if who is None:
            raise Error("Sign in first", 401)
        if role == "admin" and who["role"] != "admin":
            raise Error("Only admins can do that", 403)
        return who

    # -- accounts ----------------------------------------------------------------

    def add_user(self, name: str, role: str, password: str) -> int:
        if role not in ROLES:
            raise Error("Role must be admin or host")
        return self.store.add_user(name, role, hash_password(password))

    def update_user(self, by: dict, user_id: int, name: Optional[str] = None, role: Optional[str] = None,
                    disabled: Optional[bool] = None, password: Optional[str] = None) -> None:
        if role is not None and role not in ROLES:
            raise Error("Role must be admin or host")
        if by["id"] == user_id and (role == "host" or disabled):
            raise Error("You can't take away your own admin access", 409)
        fields = {k: v for k, v in (("name", name), ("role", role)) if v is not None}
        if disabled is not None:
            fields["disabled"] = int(disabled)
        if password is not None:
            fields["password"] = hash_password(password)
        self.store.update_user(user_id, **fields)
        if disabled or password is not None:
            # Signed out everywhere, except on the device of an admin changing their own password.
            self.store.end_staff_sessions(user_id, keep=by["session"] if by["id"] == user_id else None)
        if disabled or password is not None or role == "host":
            self.store.drop_invites_by(user_id)  # links they made go too (deleting them does it by itself)

    def delete_user(self, by: dict, user_id: int) -> None:
        if by["id"] == user_id:
            raise Error("You can't delete your own account", 409)
        self.store.delete_user(user_id)
