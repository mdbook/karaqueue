"""Optional yt-dlp support: fetch YouTube songs to the server's disk and search without an API key.

Played from disk, a YouTube song behaves like a library song: it can go to any speaker, gets
recorded with its music, has no ads, and keeps playing if the internet drops.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .media import parse
from .store import Error
from .youtube import VIDEO_ID

log = logging.getLogger("karaqueue.downloads")
# Browsers play these without help. The default format asks for mp4 so this is almost always it.
EXTENSIONS = (".mp4", ".webm", ".m4a")
# H.264 first: it plays on every browser and smart TV, VP9 and AV1 don't.
DEFAULT_FORMAT = ("bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]"
                  "/b[ext=mp4]/bv*[height<=1080]+ba/b")
RETRY_AFTER = 600


class Downloads:
    def __init__(self, folder: str, command: str = "yt-dlp", fmt: str = DEFAULT_FORMAT,
                 max_bytes: int = 10 * 1024 ** 3, workers: int = 2):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        sweep(self.folder, HALF_DONE)
        self.command = command
        self.fmt = fmt
        self.max_bytes = max_bytes
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yt-dlp")
        self.lock = threading.Lock()
        self.pending: set = set()
        self.failed: dict = {}  # video id -> when it failed, so a bad video isn't retried every request

    def ready(self, video_id: Optional[str]) -> Optional[Path]:
        if not video_id or not VIDEO_ID.match(video_id):
            return None
        for ext in EXTENSIONS:
            path = self.folder / (video_id + ext)
            if path.is_file():
                return path
        return None

    def fetch(self, video_id: Optional[str]) -> None:
        """Start downloading in the background, unless it's here, on its way, or just failed."""
        if not video_id or not VIDEO_ID.match(video_id) or self.ready(video_id):
            return
        with self.lock:
            if video_id in self.pending or time.time() - self.failed.get(video_id, 0) < RETRY_AFTER:
                return
            self.pending.add(video_id)
        self.pool.submit(self._download, video_id)

    def status(self, video_id: Optional[str]) -> Optional[str]:
        """"ready", "pending" (on its way), "failed" (gave up for now) or None (never asked for)."""
        if self.ready(video_id):
            return "ready"
        with self.lock:
            if video_id in self.pending:
                return "pending"
            if time.time() - self.failed.get(video_id, 0) < RETRY_AFTER:
                return "failed"
        return None

    def _download(self, video_id: str) -> None:
        try:
            # Only ever a YouTube watch URL built from a checked id: guests never get to point
            # the server at an address of their choosing.
            result = run(
                [self.command, "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
                 "-f", self.fmt, "--merge-output-format", "mp4",
                 "-o", str(self.folder / f"{video_id}.%(ext)s"),
                 "--", f"https://www.youtube.com/watch?v={video_id}"],
                timeout=900)
            if result.returncode != 0 or not self.ready(video_id):
                raise RuntimeError(result.stderr.strip()[-300:] or "no playable file came out")
            self._trim()
        except Exception as exc:
            log.warning("Couldn't download %s: %s", video_id, exc)
            with self.lock:
                self.failed[video_id] = time.time()
        finally:
            with self.lock:
                self.pending.discard(video_id)

    def touch(self, path: Path) -> None:
        # Playing a file marks it recently used, so the cache trims the ones nobody sings.
        try:
            os.utime(path)
        except OSError:
            pass

    def _files(self) -> list:
        return [p for p in self.folder.iterdir() if p.is_file() and p.suffix in EXTENSIONS]

    def usage(self) -> dict:
        files = self._files()
        return {"files": len(files), "bytes": sum(p.stat().st_size for p in files),
                "max_bytes": self.max_bytes, "pending": len(self.pending)}

    def songs(self) -> list:
        """Every downloaded song, most recently played first."""
        files = []
        for path in self._files():
            stat = path.stat()
            files.append({"video_id": path.stem, "bytes": stat.st_size, "played_at": stat.st_mtime})
        return sorted(files, key=lambda f: -f["played_at"])

    def delete(self, video_id: str) -> bool:
        path = self.ready(video_id)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    def clear(self) -> None:
        """Delete every downloaded song. They come back the next time someone queues them."""
        for path in self._files():
            path.unlink(missing_ok=True)

    def _trim(self) -> None:
        files = self._files()
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > self.max_bytes:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)


def search(command: str, query: str, suffix: str = "karaoke", limit: int = 8) -> list:
    """Search YouTube with yt-dlp, no API key needed."""
    query = " ".join((query or "").split())[:100]
    if not query:
        return []
    try:
        result = run(
            [command, "--quiet", "--no-warnings", "--flat-playlist", "-J",
             "--", f"ytsearch{limit}:{query} {suffix}".strip()],
            timeout=20)
        data = json.loads(result.stdout)
    except Exception:
        raise Error("Search isn't working right now", 502)
    hits = []
    for item in data.get("entries") or []:
        if not VIDEO_ID.match(item.get("id") or ""):
            continue
        thumbs = item.get("thumbnails") or []
        hits.append({"video_id": item["id"], "title": item.get("title") or "",
                     "channel": item.get("channel") or item.get("uploader") or "",
                     "thumbnail": thumbs[0].get("url") if thumbs else None})
    return hits


# Characters file systems (or Windows shares) won't take in a name, and ones that flip text
# around so a name reads differently than it sorts.
UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*‎‏‪-‮⁦-⁩]+')
QUEUE = 20  # saves waiting their turn; past that, try again in a bit


class SaveFailed(Exception):
    """A reason a save failed that's fine to show whoever asked for it."""


def file_stem(video_id: str, title: str, artist: str = "") -> str:
    """"Artist - Title [id]": the library reads the names back from it, and the id keeps it unique.
    Each part stays under 90 bytes, so the whole name fits the usual 255 byte limit in any script."""
    def clean(text: str) -> str:
        text = " ".join(UNSAFE.sub(" ", text or "").split()).strip(". ")
        return text.encode()[:90].decode(errors="ignore").strip(". ")
    name = " - ".join(p for p in (clean(artist), clean(title)) if p) or "Song"
    return f"{name} [{video_id}]"


def readable(text: Optional[str]) -> str:
    """Text with its spaces tidied and any lone surrogate (only possible in hand-made JSON) left
    out, since one can't become a file name or a reply."""
    return " ".join((text or "").encode(errors="ignore").decode().split())


def run(args: list, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run, but a timeout ends yt-dlp's helpers too (it runs ffmpeg to merge video and
    sound), not just yt-dlp, so a stuck download can't leave an ffmpeg running for good."""
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=os.name == "posix")
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


# What yt-dlp leaves while it works: partial files, and the video and sound halves before merging.
HALF_DONE = re.compile(r"\.(part|ytdl)$|\.f\d+\.\w+$")


def sweep(folder: Path, only: Optional[re.Pattern] = None, older_than: float = 3600) -> None:
    """Clear what a server stopped mid-download left behind. Only old things, since a downloader
    replaced by a settings change may still be busy with new ones (a download gives up after 15 minutes)."""
    cutoff = time.time() - older_than
    try:
        leftovers = [p for p in folder.iterdir() if only is None or only.search(p.name)]
    except OSError:
        return
    for path in leftovers:
        try:
            if path.lstat().st_mtime >= cutoff:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


class Saver:
    """Saves YouTube songs into a library folder for good, unlike the downloads folder, which
    trims the songs nobody sings. One at a time, so a big batch doesn't hog the connection."""
    # ponytail: the list of saves lives in memory, newest 200; the files are what lasts. Changing
    # the yt-dlp format or the biggest song on the admin page starts a new Saver, which forgets
    # saves still waiting (the one saving finishes). Keep the queue across rebuilds if that bites.
    def __init__(self, command: str = "yt-dlp", fmt: str = DEFAULT_FORMAT, keep_free: int = 1024 ** 3,
                 max_bytes: int = 500 * 1024 ** 2):
        self.command = command
        self.fmt = fmt
        self.keep_free = keep_free
        self.max_bytes = max_bytes
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yt-dlp-save")
        self.lock = threading.RLock()
        self.jobs: dict = {}  # video id -> what's happening with it

    @staticmethod
    def saved(folder: Path, video_id: str) -> Optional[Path]:
        ends = tuple(f"[{video_id}]{ext}" for ext in EXTENSIONS)
        try:
            return next((folder / n for n in os.listdir(folder) if n.endswith(ends)), None)
        except OSError:
            return None

    def save(self, folder: Path, video_id: str, title: str, artist: str = "", room: Optional[str] = None,
             cached: Optional[Path] = None, done=None) -> dict:
        """Start saving, unless it's already saved or on its way. `cached` is a copy the
        downloads folder already has, so it doesn't come down twice. `done` runs once it's in."""
        if not VIDEO_ID.match(video_id or ""):
            raise Error("That isn't a YouTube video")
        title, artist = readable(title), readable(artist)
        if not title:
            raise Error("Give the song a title")
        with self.lock:
            job = self.jobs.get(video_id)
            if job and job["state"] in ("waiting", "saving"):
                return self.shown(job)
            existing = self.saved(folder, video_id)
            if not existing:
                self.check_room()
            job = {"video_id": video_id, "title": title, "artist": artist,
                   "room": room, "state": "waiting", "error": None, "file": None, "at": time.time()}
            if existing:
                # Already saved, maybe under another name: show the name the library has.
                job.update(state="saved", file=existing.name, title=parse(existing.name)[0], artist=parse(existing.name)[1])
            self.jobs[video_id] = job
            while len(self.jobs) > 200:
                self.jobs.pop(next(iter(self.jobs)))
        if not existing:
            self.pool.submit(self._save, folder, job, cached, done)
        return self.shown(job)

    @staticmethod
    def shown(job: dict) -> dict:
        # Without the room it came from: a room code is what lets someone join that room.
        return {k: v for k, v in job.items() if k != "room"}

    def check_room(self) -> None:
        """Turn a new save away if the queue is full, before it counts against anyone's hour."""
        with self.lock:
            if sum(j["state"] == "waiting" for j in self.jobs.values()) >= QUEUE:
                raise Error("Lots of songs are waiting to save. Try again in a few minutes.", 429)

    def _save(self, folder: Path, job: dict, cached: Optional[Path], done) -> None:
        video_id = job["video_id"]
        # Half-finished downloads go in a hidden folder the library doesn't look in, one of its own
        # per save, so two savers (say, one replaced by a settings change) never touch each other's.
        work = folder / ".saving" / f"{video_id}-{secrets.token_hex(4)}"
        sweep(work.parent)  # scratch folders of saves a restart cut off
        try:
            with self.lock:
                job["state"] = "saving"
            work.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(folder).free < self.keep_free:
                raise SaveFailed("the server's disk is nearly full")
            stem = file_stem(video_id, job["title"], job["artist"])
            too_big = SaveFailed(f"it's bigger than {self.max_bytes // 1024 ** 2} MB")
            said = ""  # what yt-dlp printed
            if cached and cached.is_file():
                if cached.stat().st_size > self.max_bytes:
                    raise too_big
                shutil.copyfile(cached, work / (video_id + cached.suffix))
            else:
                result = run(
                    [self.command, "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
                     "-f", self.fmt, "--merge-output-format", "mp4", "--max-filesize", str(self.max_bytes),
                     # % starts a yt-dlp template field, so one in the folder's name has to be doubled.
                     "-o", str(work).replace("%", "%%") + f"/{video_id}.%(ext)s",
                     "--", f"https://www.youtube.com/watch?v={video_id}"],
                    timeout=900)
                said = result.stdout + result.stderr
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip()[-300:] or "yt-dlp failed")
            made = next((work / (video_id + ext) for ext in EXTENSIONS if (work / (video_id + ext)).is_file()), None)
            if made is None:
                # yt-dlp skips a video over --max-filesize without failing (or saying so, with --quiet).
                log.warning("yt-dlp made no file for %s: %s", video_id, said.strip()[-300:])
                raise SaveFailed(f"YouTube had no copy under {self.max_bytes // 1024 ** 2} MB")
            if made.stat().st_size > self.max_bytes:
                raise too_big
            # The download itself could have filled the disk.
            if shutil.disk_usage(folder).free < self.keep_free:
                raise SaveFailed("the server's disk is nearly full")
            final = folder / (stem + made.suffix)
            made.replace(final)
            with self.lock:
                job.update(state="saved", file=final.name)
            if done:
                done()
        except Exception as exc:
            # The details (paths, yt-dlp's output) go in the log, not to whoever asked.
            log.warning("Couldn't save %s: %s", video_id, exc)
            reason = str(exc) if isinstance(exc, SaveFailed) else "the download didn't work. The server log says why"
            with self.lock:
                job.update(state="failed", error="Couldn't save it: " + reason)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def list(self, room: Optional[str] = None) -> list:
        """Saves, newest first: all of them, or just one room's."""
        with self.lock:
            jobs = [dict(j) for j in self.jobs.values() if room is None or j["room"] == room]
        return sorted(jobs, key=lambda j: -j["at"])
