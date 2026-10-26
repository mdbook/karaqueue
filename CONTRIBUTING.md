# Contributing to KaraQueue

Thanks for wanting to help! Bug reports, ideas and pull requests are all welcome. KaraQueue is a side project, so replies might take a few days.

## Reporting a bug or asking for something

Open an issue with what you did, what you expected, and what happened instead. Screenshots help a lot for anything on a screen or phone. Say which browser, and whether you're running the Docker image or from Python. For anything security-related, please don't open an issue: see [SECURITY.md](SECURITY.md).

## Setting up

```bash
git clone https://github.com/mdbook/karaqueue
cd karaqueue
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

That runs the whole server test suite, no browser or internet needed. Run the app itself with `.venv/bin/karaqueue` and open `http://localhost:8000`.

To check the pages in a real browser (optional, but please do for UI changes):

```bash
.venv/bin/pip install -e '.[ui]'
.venv/bin/playwright install chromium
.venv/bin/python tests/browser_smoke.py --screenshots /tmp/karaqueue-ui
```

It starts its own throwaway server with a temporary database and clicks through every page, from 320px phones to 1440px desktops. Look through the screenshots it saves. If you changed how something looks, `tests/readme_screenshots.py` retakes the pictures in the README and docs (it needs ffmpeg too).

## Making a change

- **Keep it small.** No build step, no frontend framework, no new dependency for something a few lines can do. The pages are plain HTML and JavaScript in `karaqueue/static/`.
- **Settings go through `karaqueue/settings.py`.** Anything someone might want different on their own server is a setting with a sensible default, documented in [docs/configuration.md](docs/configuration.md) and `.env.example`. A test checks they all match.
- **Rules live in the store.** Who can do what, and how the rotation works, belong in `karaqueue/store.py`, not just in the page.
- **Add a test** for new behavior, next to the tests for the module you changed.
- **Write for people.** User-facing text (and the docs) is short, plain and friendly. Say "the host lets the screen in", not "authorize the display client". No em dashes.

Then open a pull request saying what changed and why. GitHub runs the tests on Python 3.9 and 3.12.

## License

KaraQueue is GPL-3.0-or-later. By sending a pull request you agree your change is released under the same license.
