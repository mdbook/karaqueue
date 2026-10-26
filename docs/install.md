# Installing and running KaraQueue

KaraQueue is one Python process with a SQLite database. Run it in Docker (easiest, and it comes with ffmpeg and yt-dlp) or straight from Python.

## Docker Compose

```bash
git clone https://github.com/mdbook/karaqueue
cd karaqueue
cp .env.example .env      # then fill in what you want, every line is optional
mkdir songs               # or point compose.yaml at your karaoke folder
docker compose up -d
```

The `env_file` line in `compose.yaml` needs Docker Compose 2.24 or newer; on an older one, delete the `required: false` part and keep a `.env` file next to it.

Then pick an admin password (see [The first time](#the-first-time)) and open `http://localhost:8000`. Your songs go in `./songs` (or change the mount in [`compose.yaml`](../compose.yaml)), and everything else (the database, recordings and downloaded songs) lives in the `karaqueue-data` volume.

## Plain Docker

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

Python 3.9 or newer:

```bash
pip install .
karaqueue
```

For library files that need converting (AVI, MPG and friends), and for tidying recordings, install [ffmpeg](https://ffmpeg.org). For YouTube downloads, install [yt-dlp](https://github.com/yt-dlp/yt-dlp) with a JavaScript runtime: `pip install "yt-dlp[default,deno]"`. See [Your songs](library.md).

## The first time

You need an admin password. Either:

- start the server with `KARAQUEUE_ADMIN_PASSWORD=...`, or
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
