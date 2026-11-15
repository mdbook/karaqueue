# Code standards

The rules this code already follows. Where the code disagrees with itself, the rule below is the pattern most of it uses. Read this and [DESIGN.md](DESIGN.md) before you change code or a page.

## Language and runtime

- Python 3.9 or newer. Do not use syntax or standard library calls newer than 3.9. CI tests 3.9 and 3.12; the container runs 3.12.
- Start every module with `from __future__ import annotations`. Use `Optional[...]` and `List[...]` from `typing`, not `X | None`: FastAPI and pydantic evaluate some annotations at runtime.
- Runtime dependencies are the four in `pyproject.toml` (FastAPI, uvicorn, pydantic, segno). Do not add one for something a few lines can do. ffmpeg, yt-dlp, cloudflared and tailscale are optional, found at runtime, and the feature that needs them turns itself off when they are missing.
- The pages are plain HTML, CSS and JavaScript in `karaqueue/static/`. No build step, no framework, no bundler, no npm.

## Layout

- One module per concern in `karaqueue/` (see the table in [architecture.md](architecture.md)). Put new code in the module that owns the concern before making a new file.
- `app.py` holds routes and wiring only. Rules about who can do what, and the rotation, go in `store.py`.
- Pages: one HTML file per page, its own CSS file (`room.css`, `host.css`, ...), shared CSS in `style.css`, shared JavaScript in `kq.js`. Page logic lives in one inline `<script>` at the end of the page.
- A new folder under `karaqueue/static/` must be added to `[tool.setuptools.package-data]` in `pyproject.toml`.
- Tests: `tests/test_<module>.py`, one file per module.

## Naming and style

### Python

- 4 spaces, double quotes, f-strings for text and `%` arguments for log calls.
- `snake_case` functions and variables, `PascalCase` classes, `UPPER_CASE` module constants. A leading underscore marks a module-private helper.
- Short plain names (`Store`, `Hub`, `Limiter`, `readable`). No `Manager`, `Helper` or `Util` suffixes.
- Lines up to about 120 characters. No formatter or linter is configured; match the surrounding code.
- Docstrings and comments say why, in one or two plain sentences. Every module opens with a docstring that says what it is for.

### JavaScript

- 2 spaces, double quotes, semicolons, `const` by default.
- Build DOM with `h(tag, attrs, ...children)` from `kq.js` so user text always goes in as text. Never put user text in `innerHTML`. The only `innerHTML` is server-generated QR markup.
- Wrap anything a click starts in `act(fn)` so errors land in the toast. Call the server through `api(method, path, body)`.
- Browser storage keys start with `kq:` (`kq:theme`, `kq:<ROOM>`).
- Inline scripts are allowed by a Content Security Policy hash computed at startup, and only exact `<script>` tags are hashed. Do not add attributes to an inline `<script>` tag, inline event handlers (`onclick="..."`), or scripts from a new origin.

## Errors

- Reject a bad request by raising `store.Error("Plain sentence for the person.", status)`. The default status is 400. The app turns it into `{"detail": "..."}` with that status, and the page shows `detail` in the toast.
- Write error messages for the guest, not the developer: say what happened and what to do next ("Search is busy. Try again in a moment.").
- `HTTPException` appears once, in the `guards.py` middleware for oversized bodies. Routes use `store.Error`.
- Log with `logging.getLogger("karaqueue")` (the desktop app uses `karaqueue.desktop`). `log.warning` for something recovered from, `log.exception` inside an `except` that swallows. Use `print` only for the desktop app's console fallback.
- Never log or return a raw key, token or password. Keys are stored as a SHA-256 hash, passwords as salted scrypt.

## Configuration

- Every server setting is a row in `SETTINGS` (or `STARTUP` for the few read once at startup, like the database path, host and port) in `karaqueue/settings.py`, with a type, a default, a group, a label and one line of help.
- The environment name is `KARAQUEUE_` plus the key in capitals (`settings.env_name`). A value saved on the admin page beats the environment, which beats the default.
- A new setting also needs a row in the table in [configuration.md](configuration.md) and a line in `.env.example`. `tests/test_docs.py` fails if the three disagree.
- Defaults must work out of the box: a fresh `docker compose up -d` with only `KARAQUEUE_ADMIN_PASSWORD` set is a working server.

## Data

- One SQLite file. Schema changes run on their own when a new version starts and must upgrade an existing database, not only create a new one.
- Media plays only from signed links scoped to one room that expire. Do not add a route that serves files without that check.

## Tests

- Run the suite:

  ```bash
  python -m venv .venv
  .venv/bin/pip install -e '.[dev]'
  .venv/bin/pytest -q
  ```

- The suite needs no browser and no internet. Tests that need ffmpeg, ffprobe or node skip themselves when those are missing; install ffmpeg if you change `media.py`.
- New behavior gets a test in the file for the module you changed. `tests/conftest.py` lets anyone start a room in every test; account tests switch that back.
- For a page change, also run the browser smoke test and look at its screenshots:

  ```bash
  .venv/bin/pip install -e '.[ui]'
  .venv/bin/playwright install chromium
  .venv/bin/python tests/browser_smoke.py --screenshots /tmp/karaqueue-ui
  ```

- If a change alters how something looks, rerun `tests/readme_screenshots.py` to retake the pictures in the docs. Do not edit `docs/screenshots/` by hand.

## Docs

- Behavior changes update the matching page in `docs/` in the same change.
- Short, plain, friendly sentences. No em dashes in any Markdown file in the root or `docs/`; `tests/test_docs.py` checks this and that every relative link and anchor resolves.

## Commits and merge requests

- One change per branch, merged into `main` through a merge request (a pull request on GitHub) with the tests passing.
- Commit subjects are one plain sentence in the imperative that says what changed for the user ("Download new songs at their true queue place"). No type prefix is needed; a few older commits use `fix:` or `docs:`, which is drift, not the rule.
- Do not mention security problems in a commit or a public issue: see [SECURITY.md](../SECURITY.md).

## Build and release

- CI runs `pip install '.[dev]'` and `pytest -q` on every push and merge request. GitLab CI also checks `requirements.lock` and builds a container image per branch (`latest` on `main`); GitHub Actions also builds and smoke-tests the desktop app on Windows, macOS and Linux.
- The container installs only hash-pinned wheels from `requirements.lock`, and `python ci/lock.py check` fails the build if it is out of step with `pyproject.toml`. A scheduled CI job refreshes it with `python ci/lock.py resolve`; a person reviews and commits the result. Do not edit it by hand.
- The version lives in `pyproject.toml`. A `v*` tag publishes the desktop builds to the GitHub release.
- Nothing in this repo deploys a server. People upgrade with `git pull && docker compose up -d --build` (see [install.md](install.md)).
- Leave `.gitlab-ci.yml`, `.github/workflows/`, `Dockerfile`, `compose.yaml`, `LICENSE` and `karaqueue/static/fonts/` alone unless the change is about them.

## Known drift

Recorded here, not fixed.

1. No linter or formatter is configured. About 135 lines run past 120 characters, and a default `ruff check` reports 9 lambda assignments, 3 unused imports, 1 unused variable and 1 line with two imports.
2. A few older commit subjects use `fix:`, `docs:` or `ci:` prefixes.
