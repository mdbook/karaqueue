"""A folder of karaoke songs on the server's own disk, so the party works with no internet.

Three kinds of song live here:
- files a browser plays as they are (mp4, webm, mp3...),
- MP3+CDG pairs, the classic karaoke disc format, loose or zipped, drawn on the screen by kq-cdg.js,
- older video files (avi, mpg, wmv...) that ffmpeg converts on the fly, if it's installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator, Optional

from .store import Error

PLAYABLE = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".ogv", ".mp3", ".m4a", ".ogg", ".wav", ".flac"}
CDG_AUDIO = (".mp3", ".m4a", ".ogg", ".wav", ".flac")
CONVERT = {".avi", ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".ts", ".3gp", ".divx", ".asf", ".m2ts"}
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".wav": "audio/wav",
               ".flac": "audio/flac"}
# "(Karaoke Version)", "[Karaoke]", "(With Lyrics)" and friends add nothing to a title.
NOISE = re.compile(r"\s*[\(\[][^\)\]]*(karaoke|lyrics|instrumental|version|hd)[^\)\]]*[\)\]]", re.I)
# Disc codes from karaoke libraries, like "SC8123-05" or "SF001-01".
DISC = re.compile(r"^[A-Z]{1,5}\s?\d[\w-]*$", re.I)
# The " [dQw4w9WgXcQ]" yt-dlp and saved YouTube songs end with.
VIDEO_TAG = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]$")


def parse(name: str) -> tuple:
    """Guess (title, artist) from a file name like "Artist - Title (Karaoke).mp4"."""
    stem = NOISE.sub("", VIDEO_TAG.sub("", Path(name).stem).replace("_", " ")).strip()
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
    if len(parts) > 2 and DISC.match(parts[0]):
        parts = parts[1:]
    if len(parts) >= 2:
        return " - ".join(parts[1:]), parts[0]
    return stem, ""


# Naming rules: a pattern like "{artist} - {title}" or "{artist}/{title}" that reads a folder's
# file names. {skip} matches a part to leave out, like a disc code or a track number.
RULE_PART = re.compile(r"\{(\w+)\}")


def naming_rule(pattern: str) -> list:
    """A pattern -> one (start, [(part, text after it)...], last part, end) per "/"-separated piece,
    read against the end of a song's path inside its folder, without the extension. Spaces match any
    run of spaces. Each part runs up to the first place the text after it shows up, so a name is read
    in one pass however many parts a pattern has (a regex of lazy parts backtracks badly on misses)."""
    pattern = " ".join(pattern.split()).strip("/ ")
    names = RULE_PART.findall(pattern)
    for name in names:
        if name not in ("artist", "title", "skip"):
            raise ValueError(f"There's no {{{name}}}. Use {{artist}}, {{title}} and {{skip}}.")
    if names.count("title") != 1 or names.count("artist") > 1:
        raise ValueError("A pattern needs {title} once, and {artist} at most once")
    find = lambda text, end="": re.compile(re.escape(text) + end, re.I)
    pieces = []
    for piece in pattern.split("/"):
        texts, parts = RULE_PART.split(piece.strip())[::2], RULE_PART.findall(piece)
        if "" in texts[1:-1]:
            raise ValueError("Put something between two parts, like {artist} - {title}")
        if not parts:
            pieces.append((find(texts[0], r"\Z"), [], None, None))
            continue
        middle = list(zip(parts[:-1], map(find, texts[1:-1])))
        pieces.append((find(texts[0]), middle, parts[-1], find(texts[-1], r"\Z")))
    return pieces


def read_piece(piece: tuple, text: str) -> Optional[dict]:
    """The parts one piece of a naming rule reads from one folder or file name, or None."""
    start, middle, last, end = piece
    found = start.match(text)
    if not found or last is None:
        return {} if found else None
    out, pos = {}, found.end()
    for name, after in middle:
        found = after.search(text, pos + 1)
        if not found:
            return None
        out[name], pos = text[pos:found.start()], found.end()
    found = end.search(text, pos + 1)
    if not found:
        return None
    out[last] = text[pos:found.start()]
    return out if all(v.strip() for v in out.values()) else None


def by_rule(rule: list, rel: str) -> Optional[tuple]:
    """(title, artist) a naming rule reads from a song's path, or None if it doesn't fit."""
    folder, _, name = rel.rpartition("/")
    stem = NOISE.sub("", VIDEO_TAG.sub("", Path(name).stem).replace("_", " ")).strip()
    names = [" ".join(n.split()) for n in (folder.split("/") if folder else []) + [stem]]
    if len(names) < len(rule):
        return None
    found = {}
    for piece, text in zip(rule, names[-len(rule):]):
        got = read_piece(piece, text)
        if got is None:
            return None
        found.update(got)
    return " ".join(found["title"].split()), " ".join(found.get("artist", "").split())


MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording"
USER_AGENT = "KaraQueue/2 (https://github.com/mdbook/karaqueue)"  # MusicBrainz asks every app to name itself
_asked = [0.0]
_asking = threading.Lock()


def lookup(title: str, artist: str = "", limit: int = 8) -> list:
    """Real titles and artists from MusicBrainz that match what a song is called now."""
    title, artist = " ".join(title.split())[:120], " ".join(artist.split())[:120]
    if not title:
        raise Error("Type a title to look up")
    quote = lambda s: '"' + s.replace("\\", " ").replace('"', " ") + '"'
    query = f"recording:{quote(title)}" + (f" AND artist:{quote(artist)}" if artist else "")
    url = MUSICBRAINZ + "?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 25})
    with _asking:  # MusicBrainz allows about one request a second from each server
        time.sleep(max(0.0, _asked[0] + 1 - time.monotonic()))
        _asked[0] = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=8) as res:
                data = json.load(res)
        except Exception:
            raise Error("MusicBrainz isn't answering right now", 502)
    if not isinstance(data, dict):
        raise Error("MusicBrainz sent back something odd", 502)
    seen, out = set(), []
    for rec in data.get("recordings", []):
        name = "".join(c.get("name", "") + c.get("joinphrase", "") for c in rec.get("artist-credit", [])).strip()
        key = (rec.get("title", "").lower(), name.lower())
        if not rec.get("title") or key in seen:
            continue
        seen.add(key)
        year = min((r.get("date", "")[:4] for r in rec.get("releases", []) if r.get("date")), default="")
        out.append({"title": rec["title"], "artist": name, "year": year})
        if len(out) == limit:
            break
    return out


def zip_pair(path: Path) -> Optional[tuple]:
    """(audio member, cdg member) if this zip is an MP3+CDG pair, else None."""
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
    except (zipfile.BadZipFile, OSError):
        return None
    cdg = next((n for n in names if n.lower().endswith(".cdg")), None)
    audio = next((n for n in names if n.lower().endswith(CDG_AUDIO)), None)
    return (audio, cdg) if cdg and audio else None


def folder_keys(paths: list) -> list:
    """A short, readable name per folder for song ids: its own name, numbered if two match."""
    keys = []
    for path in paths:
        base = re.sub(r"[^\w.-]+", "-", Path(path).name).strip("-") or "library"
        key, n = base, 2
        while key in keys:
            key, n = f"{base}-{n}", n + 1
        keys.append(key)
    return keys


class Library:
    """One or more folders of songs. With one folder a song's id is its path inside it; with
    several it's "<folder name>/<path>", and an old bare path still finds its song."""
    # ponytail: rescans every folder at most once a minute and searches in memory.
    # Fine for tens of thousands of files; add a proper index if libraries get bigger.
    def __init__(self, roots: str, rescan_after: float = 60, ffmpeg: Optional[str] = "ffmpeg",
                 edits: Optional[dict] = None, rules: Optional[dict] = None):
        paths = [p for p in roots.split(os.pathsep) if p.strip()]
        self.folders = [{"key": key, "root": Path(p).resolve()} for key, p in zip(folder_keys(paths), paths)]
        self.root = self.folders[0]["root"]
        self.rescan_after = rescan_after
        self.ffmpeg = shutil.which(ffmpeg) if ffmpeg else None
        self.edits = dict(edits or {})  # song id -> (title, artist) an admin fixed
        self.rules: dict = {}  # folder path -> naming rule pattern
        self.set_rules(rules or {})
        self.entries: dict = {}
        self.by_rel: dict = {}
        self.scanned = 0.0
        self.lock = threading.Lock()

    def set_rules(self, rules: dict) -> None:
        """Naming rules by folder path. Bad patterns are left out, so a typo can't empty the library."""
        compiled = {}
        for path, pattern in rules.items():
            try:
                compiled[str(Path(path).resolve())] = (pattern, naming_rule(pattern))
            except ValueError:
                continue
        self.rules = compiled
        self.scanned = 0.0

    def guess(self, root: Path, rel: str) -> tuple:
        """(title, artist) for a file, from its folder's naming rule or else from its name."""
        rule = self.rules.get(str(root))
        return (rule and by_rule(rule[1], rel)) or parse(rel.rpartition("/")[2])

    def _scan(self) -> None:
        with self.lock:
            if time.monotonic() - self.scanned < self.rescan_after and self.scanned:
                return
            entries, by_rel = {}, {}
            several = len(self.folders) > 1
            for folder in self.folders:
                root, prefix = folder["root"], folder["key"] + "/" if several else ""
                for here, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    here = Path(here)
                    cdgs = {Path(n).stem.lower(): n for n in files if n.lower().endswith(".cdg")}
                    for name in files:
                        suffix = Path(name).suffix.lower()
                        if name.startswith("."):
                            continue
                        entry = None
                        if suffix in PLAYABLE:
                            entry = {"kind": "file"}
                            cdg = cdgs.get(Path(name).stem.lower()) if suffix in CDG_AUDIO else None
                            if cdg:
                                entry = {"kind": "cdg", "cdg": here / cdg}
                        elif suffix in CONVERT and self.ffmpeg:
                            entry = {"kind": "convert"}
                        elif suffix == ".zip":
                            pair = zip_pair(here / name)
                            if pair:
                                entry = {"kind": "zip", "audio": pair[0], "cdg": pair[1]}
                        if entry is None:
                            continue
                        rel = (here / name).relative_to(root).as_posix()
                        try:
                            size = (here / name).stat().st_size + (entry["cdg"].stat().st_size if entry["kind"] == "cdg" else 0)
                        except OSError:
                            continue
                        title, artist = self.edits.get(prefix + rel) or self.guess(root, rel)
                        entries[prefix + rel] = {"id": prefix + rel, "title": title, "artist": artist,
                                                 "folder": folder["key"], "root": root, "path": here / name,
                                                 "bytes": size, **entry}
                        by_rel.setdefault(rel, prefix + rel)
            self.entries, self.by_rel = entries, by_rel
            self.scanned = time.monotonic()

    def search(self, query: str, limit: int = 25) -> list:
        self._scan()
        words = query.lower().split()
        if not words:
            return []
        hits = [e for e in self.entries.values()
                if all(w in (e["title"] + " " + e["artist"] + " " + e["id"]).lower() for w in words)]
        hits.sort(key=lambda e: (e["artist"].lower(), e["title"].lower()))
        return [{"id": e["id"], "title": e["title"], "artist": e["artist"]} for e in hits[:limit]]

    # -- for the admin page --------------------------------------------------------

    def songs(self, query: str = "", offset: int = 0, limit: int = 100) -> dict:
        """Every song matching `query` (all of them when it's empty), a page at a time."""
        self._scan()
        words = query.lower().split()
        hits = [e for e in self.entries.values()
                if all(w in (e["title"] + " " + e["artist"] + " " + e["id"] + " " + e["kind"]).lower() for w in words)]
        hits.sort(key=lambda e: (e["artist"].lower(), e["title"].lower(), e["id"]))
        return {"total": len(hits), "songs": [
            {"id": e["id"], "title": e["title"], "artist": e["artist"], "kind": e["kind"], "folder": e["folder"],
             "bytes": e["bytes"], "edited": e["id"] in self.edits} for e in hits[offset:offset + limit]]}

    def folder_stats(self) -> list:
        self._scan()
        out = []
        for folder in self.folders:
            mine = [e for e in self.entries.values() if e["root"] == folder["root"]]
            rule = self.rules.get(str(folder["root"]))
            out.append({"key": folder["key"], "path": str(folder["root"]), "exists": folder["root"].is_dir(),
                        "songs": len(mine), "bytes": sum(e["bytes"] for e in mine), "rule": rule[0] if rule else ""})
        return out

    def preview(self, path: str, pattern: str, limit: int = 12) -> dict:
        """What a naming rule would call a folder's songs: how many it fits, and a few examples."""
        rule = naming_rule(pattern)
        self._scan()
        mine = sorted((e for e in self.entries.values() if str(e["root"]) == path), key=lambda e: e["id"])
        fits, examples, misses = 0, [], []
        for entry in mine:
            rel = entry["path"].relative_to(entry["root"]).as_posix()
            found = by_rule(rule, rel)
            if found is None:
                misses.append(rel)
                continue
            fits += 1
            if len(examples) < limit:
                examples.append({"file": rel, "title": found[0], "artist": found[1], "edited": entry["id"] in self.edits})
        # A file the rule doesn't fit keeps the name KaraQueue guesses from it.
        return {"songs": len(mine), "fits": fits, "examples": examples, "misses": misses[:5]}

    def rename(self, song: str, title: Optional[str], artist: str = "") -> dict:
        """Fix a song's title and artist (title None goes back to what the file name says)."""
        entry = self.get(song)
        if entry is None:
            raise KeyError(song)
        if title is None:
            self.edits.pop(entry["id"], None)
            title, artist = self.guess(entry["root"], entry["path"].relative_to(entry["root"]).as_posix())
        else:
            self.edits[entry["id"]] = (title, artist)
        entry["title"], entry["artist"] = title, artist
        return entry

    def delete(self, song: str) -> Optional[str]:
        """Delete a song's file (and its lyrics file). Returns the id it had, or None if unknown."""
        entry = self.get(song)
        if entry is None:
            return None
        entry["path"].unlink(missing_ok=True)
        if entry["kind"] == "cdg":
            entry["cdg"].unlink(missing_ok=True)
        with self.lock:
            self.entries.pop(entry["id"], None)
            self.edits.pop(entry["id"], None)
        return entry["id"]

    def counts(self, rescan: bool = False) -> dict:
        """How many songs of each kind the library has, for the admin page."""
        if rescan:
            self.scanned = 0.0
        self._scan()
        counts: dict = {}
        for entry in self.entries.values():
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        return counts

    def get(self, rel: Optional[str]) -> Optional[dict]:
        if not rel:
            return None
        self._scan()
        return self.entries.get(rel) or self.entries.get(self.by_rel.get(rel, ""))

    @staticmethod
    def _inside(root: Path, path: Path) -> Optional[Path]:
        path = path.resolve()
        return path if root in path.parents and path.is_file() else None

    def path(self, rel: str) -> Optional[Path]:
        """The file on disk, but only for something we listed ourselves."""
        entry = self.get(rel)
        return self._inside(entry["root"], entry["path"]) if entry else None

    def cdg_path(self, rel: str) -> Optional[Path]:
        entry = self.get(rel)
        return self._inside(entry["root"], entry["cdg"]) if entry and entry["kind"] == "cdg" else None

    def zip_member(self, rel: str, part: str) -> Optional[tuple]:
        """(bytes, media type) of the audio or the graphics inside a zipped MP3+CDG."""
        entry = self.get(rel)
        path = self.path(rel)
        if not entry or entry["kind"] != "zip" or path is None:
            return None
        member = entry["cdg"] if part == "cdg" else entry["audio"]
        # ponytail: reads the member into memory, a few MB per song. Fine for a party.
        with zipfile.ZipFile(path) as z:
            data = z.read(member)
        kind = "application/octet-stream" if part == "cdg" else \
            AUDIO_TYPES.get(Path(member).suffix.lower(), "audio/mpeg")
        return data, kind

    def _h264(self) -> list:
        # Distro ffmpeg builds often leave out libx264 for licensing reasons but ship OpenH264.
        if not hasattr(self, "_encoder"):
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True).stdout
            self._encoder = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] if " libx264 " in out \
                else ["-c:v", "libopenh264", "-b:v", "4M"]
        return self._encoder

    def convert(self, rel: str) -> Iterator[bytes]:
        """Stream an old video format as fragmented MP4 that any browser plays."""
        path = self.path(rel)
        proc = subprocess.Popen(
            [self.ffmpeg, "-loglevel", "error", "-i", str(path), *self._h264(), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", "pipe:1"],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()
