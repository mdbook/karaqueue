# Installing and running KaraQueue

KaraQueue is one Python process with a SQLite database. Run it in Docker (easiest, and it comes with ffmpeg and yt-dlp) or straight from Python.

## Docker Compose

Follow the [README quick start](../README.md#quick-start): install Docker with Compose v2 and Git, clone the repo, copy `.env.example` to `.env`, set your admin password, and run `docker compose up -d`.

Keep `.env` next to `compose.yaml`, even if it is empty. The file is required, but every setting in it is optional. Compose uses the simple `env_file` syntax, so it does not need Compose 2.24's optional-file support.

Compose builds locally and includes ffmpeg and yt-dlp. No public image is published by this repo's GitHub workflow yet; this is not a registry-only install. Your songs go in `./songs` (or change the mount in [`compose.yaml`](../compose.yaml)). The database, recordings and downloaded songs live in the `karaqueue-data` volume. The library, downloads and recordings folders are enabled by default in Compose; override their `KARAQUEUE_` settings in `.env` if needed.

Open `http://localhost:8000` and sign in with the admin password, leaving the name empty. If you left the password unset, see [The first time](#the-first-time).

## Plain Docker

From a checkout of this repo, build and run the image:

```bash
docker build -t karaqueue .
docker run -d --name karaqueue -p 8000:8000 \
  -v karaqueue-data:/data -v /path/to/karaoke:/media:ro \
  -e KARAQUEUE_MEDIA_DIR=/media \
  -e KARAQUEUE_DOWNLOADS_DIR=/data/downloads \
  -e KARAQUEUE_RECORDINGS_DIR=/data/recordings \
  karaqueue
```

Drop the `-v /path/to/karaoke...` and `KARAQUEUE_MEDIA_DIR` lines if you don't have a song library yet. Guests can still paste YouTube links.

## Python

You need Git and Python 3.9 or newer with venv support. Install [ffmpeg](https://ffmpeg.org) for converting library files and tidying recordings. For YouTube downloads you also need yt-dlp with a JavaScript runtime; Docker includes both tools.

Get the source and install it in a virtual environment:

```bash
git clone https://github.com/mdbook/karaqueue
cd karaqueue
python3 -m venv .venv
.venv/bin/pip install .
```

For YouTube downloads, also run `.venv/bin/pip install "yt-dlp[default,deno]"`. See [Your songs](library.md).

Start the server, then follow [The first time](#the-first-time) to pick an admin password:

```bash
.venv/bin/karaqueue
```

Unlike Compose, the Python command does not read `.env`; export any `KARAQUEUE_` settings in your shell before starting it.

## The first time

You need an admin password. Either:

- set `KARAQUEUE_ADMIN_PASSWORD` to your own password (at least 8 characters) in `.env` for Compose, or in the environment for Python or plain Docker, then start the server, or
- open the one-time setup link the server writes to `admin-setup-link.txt` next to its database. With Docker: `docker exec karaqueue cat /data/admin-setup-link.txt`. If `KARAQUEUE_PUBLIC_URL` isn't set, the link starts at `/admin#setup=`, so put your server's address in front, like `http://localhost:8000/admin#setup=...`.

The server never prints the link to its log, since logs often end up where other people can read them. The file goes away once you've picked a password.

Then open `http://localhost:8000`, pick Host a night, and start a room. Out of the box the server asks hosts to sign in: use the admin password with the name left empty, or invite hosts from the admin page (see [Admin and accounts](admin.md)). On a home network you trust, `KARAQUEUE_NEW_ROOMS=anyone` lets anyone start a room without signing in.

## Behind a reverse proxy

Put KaraQueue behind a proxy with a certificate (Caddy, nginx, Traefik) to give it an https address, then tell it that address:

```bash
KARAQUEUE_PUBLIC_URL=https://karaoke.example.com
```

Without it, KaraQueue sees the plain http request the proxy forwards, so the guest link and QR code point at `http://` and the Cast a screen button stays hidden. You can also set the address on the admin page, under Public address.

The proxy has to pass WebSockets through (`/ws/...`). Caddy does that out of the box:

```
karaoke.example.com {
    reverse_proxy localhost:8000
}
```

KaraQueue limits how fast one address can guess room codes, start rooms, join and search. Behind a proxy, every request seems to come from the proxy, so tell KaraQueue where the proxy is and it reads each guest's real address from `X-Forwarded-For`:

```bash
KARAQUEUE_TRUSTED_PROXIES=172.18.0.0/16   # your proxy's address or network; * trusts anyone
```

Only use `*` when nothing but the proxy can reach KaraQueue, or anyone can pick the address they're limited by.

https matters for more than the padlock: browsers only allow the speaker picker, the mic, second monitors and casting on https pages (or on `localhost`). See [Screens](screens.md).

### https at home with Caddy

KaraQueue doesn't get certificates itself; Caddy does it for you, for free, and renews them. With a dynamic DNS name from [Public access](public-access.md), like `myparty.duckdns.org`:

1. Install [Caddy](https://caddyserver.com/docs/install) on the same machine as KaraQueue.
2. Put this in its `Caddyfile`:

   ```
   myparty.duckdns.org {
       reverse_proxy localhost:8000
   }
   ```

3. Forward ports 80 and 443 on your router to this machine, by hand (Caddy needs both to get its certificate). Leave KaraQueue's Open the port on the router off: it forwards to KaraQueue itself, which would skip Caddy.
4. Set Public address on the admin page to `https://myparty.duckdns.org`, and `KARAQUEUE_TRUSTED_PROXIES=127.0.0.1` (the default) already trusts Caddy on the same machine.

## Backups

Everything that matters is in the database file (`KARAQUEUE_DB`, `/data/karaqueue.db` in Docker) plus your recordings and saved songs folders. The downloads folder is a cache and refills itself.

The safe way to copy a live database is SQLite's own backup, which works while the server runs:

```bash
sqlite3 karaqueue.db ".backup 'karaqueue-backup.db'"
```

The Docker image doesn't have the `sqlite3` command, but it has Python:

```bash
docker exec karaqueue python -c "import sqlite3; sqlite3.connect('/data/karaqueue.db').backup(sqlite3.connect('/data/karaqueue-backup.db'))"
docker cp karaqueue:/data/karaqueue-backup.db .
```

Or stop the server and copy the file. To restore, stop the server, put the file back, and start it again. Rooms nobody touches for `KARAQUEUE_ROOM_DAYS` (30 by default) get deleted anyway, so the database stays small.

## Upgrading from KaraQueue 1

KaraQueue 2 is a rewrite, and it doesn't read KaraQueue 1's `data/queue.json` or its database. Install it in a fresh folder (or a fresh Docker volume) rather than on top of the old one. Old parties don't carry over, but nobody's queue from 2023 is still waiting on you.

One thing that might surprise you coming from version 1: new rooms need a host account or the admin password. Set `KARAQUEUE_NEW_ROOMS=anyone` for the old open-door behavior.

## Updating

With Compose: `git pull && docker compose up -d --build`. YouTube breaks old yt-dlp versions every so often, so if downloads stop working, rebuild the image (or `pip install -U yt-dlp`) to get a fresh one. Database changes happen by themselves when the new version starts.
