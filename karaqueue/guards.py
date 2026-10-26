"""Browser security headers, a cap on request bodies, and slowing down password guessing."""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from .store import Error

BODY_LIMIT = 64 * 1024  # the biggest thing the pages send is a settings value or a song order
# Recording uploads stream to disk and check their own size.
STREAMED = re.compile(r"^/api/rooms/[^/]+/recordings$")
HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")


def inline_scripts(folder: Path) -> str:
    """CSP hashes of the pages' inline scripts, so they run and nothing injected does."""
    hashes = []
    for page in sorted(folder.glob("*.html")):
        for body in re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S):
            hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return " ".join(hashes)


class Protect:
    """ASGI middleware: security headers on every response, and 413 for oversized request bodies."""

    def __init__(self, app, static: Path):
        self.app = app
        # YouTube's player is a script from youtube.com in an iframe from youtube-nocookie.com;
        # thumbnails come from ytimg.com. Inline style attributes are allowed: the pages set a few.
        self.csp = ("default-src 'self'; script-src 'self' https://www.youtube.com " + inline_scripts(static) + "; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://*.ytimg.com; "
                    "media-src 'self' blob:; frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
                    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'; connect-src 'self'")
        self.headers = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"SAMEORIGIN"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin"),
                        (b"permissions-policy", b"camera=(), geolocation=(), payment=(), usb=()")]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        csp = self.csp
        host = headers.get(b"host", b"").decode("latin-1")
        if HOST.match(host):
            # Older browsers don't count WebSockets as 'self'. connect-src is last, so this adds to it.
            csp += f" ws://{host} wss://{host}"
        extra = [*self.headers, (b"content-security-policy", csp.encode())]
        if scope.get("scheme") == "https":
            extra.append((b"strict-transport-security", b"max-age=31536000"))
        # Pages and scripts are checked with the server every time, so an upgrade never leaves a
        # TV running an old page against the new server. Songs are big and never change: cached.
        if not scope["path"].startswith(("/media/", "/downloads/")):
            extra.append((b"cache-control", b"no-cache"))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        if STREAMED.match(scope["path"]):
            return await self.app(scope, receive, send_with_headers)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > BODY_LIMIT:
                # FastAPI passes an HTTPException through as is, where anything else becomes a 400.
                raise HTTPException(413, "That request is too big")
            return message

        await self.app(scope, limited, send_with_headers)


class Logins:
    """Wrong passwords per address, per account name and, if `per_pair` is set, per address and
    name together. Past a limit, sign-ins answer 429 until the oldest wrong try is `window` seconds old. Answering at once, instead of sleeping, means
    guessing can't tie up the server's threads."""

    def __init__(self, per_address: int = 10, per_name: int = 50, per_pair: Optional[int] = None,
                 window: float = 900):
        self.limits = {"address": per_address, "name": per_name, "pair": per_pair}
        self.window = window
        self.fails: dict = {}  # (kind, value) -> times of wrong tries
        self.lock = threading.Lock()

    def _recent(self, key, now: float) -> list:
        tries = [t for t in self.fails.get(key, []) if now - t < self.window]
        if tries:
            self.fails[key] = tries
        else:
            self.fails.pop(key, None)
        return tries

    def check(self, keys: list, scale: float = 1) -> None:
        """`scale` stretches the per-address count, like the Rate limits setting; the per-name one stays put."""
        now = time.time()
        with self.lock:
            for key in keys:
                tries = self._recent(key, now)
                limit = self.limits[key[0]]
                if limit is None:
                    continue
                if key[0] == "address":
                    limit = max(1, round(limit * scale)) if scale > 0 else float("inf")
                if len(tries) >= limit:
                    minutes = max(1, round((tries[0] + self.window - now) / 60))
                    raise Error(f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes > 1 else ''}.", 429)

    def failed(self, keys: list) -> None:
        now = time.time()
        with self.lock:
            if len(self.fails) > 10000:  # ponytail: a flood of addresses; forget the stale ones
                for key in list(self.fails):
                    self._recent(key, now)
            for key in keys:
                self.fails.setdefault(key, []).append(now)

    def passed(self, *keys) -> None:
        with self.lock:
            for key in keys:
                self.fails.pop(key, None)
