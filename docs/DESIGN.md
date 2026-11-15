# Design standards

How KaraQueue looks and sounds, taken from the CSS and pages as they ship. The source of truth is `karaqueue/static/style.css` (tokens and shared components), plus one CSS file per page. Read this and [STANDARDS.md](STANDARDS.md) before you change a page.

## The idea

A warm, dark party app that gets out of the way. Flat surfaces, hairline borders, one warm accent, and big type for the things people look for from across a room: whose turn it is and the room code. The singer's name is always the loudest thing on a screen.

- Flat. The only shadows are on things that float (the toast, the singer menu) and the home page art.
- Rows are separated by space and a faint tone, not heavy lines.
- Pages hold no state worth losing, so nothing needs a "you have unsaved changes" pattern.

## Themes and color tokens

Three themes, picked per browser with the theme picker and saved as `kq:theme`. `theme.js` applies it before first paint and sets `<meta name="theme-color">`. Always use the tokens, never a raw color.

| Token | Sunset (default) | Midnight | Daylight | Use |
| --- | --- | --- | --- | --- |
| `--ground` | `#211820` | `#111c2b` | `#faf5ef` | Page background |
| `--surface` | `#2c222b` | `#1b293c` | `#ffffff` | Cards and panels |
| `--raised` | `#382c35` | `#25364b` | `#f2e9e4` | Fields, ghost buttons, things inside a card |
| `--control` | `#57434d` | `#3c526c` | `#e0cfc8` | Pressed segment, hover on ghost buttons |
| `--rule` | `#42333d` | `#304258` | `#e6d7d1` | Lines between rows |
| `--line` | `#51404a` | `#3b5069` | `#d8c5bd` | Card and field borders |
| `--line-strong` | `#816573` | `#6c88a6` | `#a5857a` | Hover borders, toast and menu borders |
| `--text` | `#fff4e9` | `#edf5ff` | `#35232e` | Body text |
| `--muted` | `#d1bbc4` | `#b7c9de` | `#705760` | Secondary text, labels |
| `--faint` | `#b49da8` | `#a0b5cd` | `#80666c` | Hints, placeholders, footers |
| `--accent` | `#ff967b` | `#a6c1ff` | `#a63836` | Primary buttons, live state, focus ring |
| `--gold` | `#ffc397` | `#c5d9ff` | `#985015` | Sunset gradient start, logo bar |
| `--red` | `#ff737d` | `#bda6ef` | `#a93154` | Sunset gradient end, logo bar |
| `--negative` | `#ff9ba9` | `#ff9caa` | `#ae2544` | Errors, remove, danger |
| `--positive` | `#b6dcb3` | `#a0dfc5` | `#286c45` | Connected, enabled |
| `--warning` | `#ffce8d` | `#ffd09a` | `#845311` | Reconnecting, paused, waiting |
| `--button-ink` | `#2c1c24` | `#15243b` | `#ffffff` | Text on an accent button |
| `--pill-accent-bg` | `#4b3035` | `#304064` | `#fbe4db` | Live card, current nav item, accent tag |
| `--pill-negative-bg` | `#4b2634` | `#492c42` | `#fbe1e8` | Danger button, remove hover |
| `--pill-positive-bg` | `#293d32` | `#254339` | `#dfefdf` | Positive pill |
| `--art-top`, `--art-mid`, `--art-bottom` | `#ffd496` `#ff9479` `#c34d75` | `#d4d8ff` `#899eea` `#5d548b` | as Sunset | The record on the home page |

`--sunset` is `linear-gradient(105deg, var(--gold), var(--accent) 60%, var(--red))`. It fills the room code and the karaoke wipe on a singer's name, and the library space meter. Keep it to those.

With `prefers-contrast: more`, `--line` becomes `--faint` and `--line-strong` becomes `--text`.

Fixed colors, on purpose: the big screen's video stage is true black (`#000`) and the QR code sits on a white tile so phones can scan it.

## Type

- Display: Bricolage Grotesque (bundled variable font, weights 200 to 800, width 75% to 100%), as `--display`. Headings, the wordmark, singer names, the room code, big numbers.
- Body: the system font stack, as `--body` (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`). Base size 100%, line height 1.5.
- Scale tokens: `--fs-title` 1.375rem, `--fs-section` 1.0625rem, `--fs-body` .9375rem, `--fs-small` .8125rem.
- Headings: `h1` is `clamp(1.75rem, 5vw, 2.5rem)` at weight 700, line height 1, tracking -0.03em. `h2` uses `--fs-title` at 600. `h3` uses `--fs-section` at 600. Page heroes go bigger with tighter tracking (down to -0.055em on the home page).
- The room code: display face, weight 700, width 85%, tracking .06em or wider, tabular numbers, filled with `--sunset`.
- Numbers that line up (queue positions, counts, stats) use `font-variant-numeric: tabular-nums`.
- Inputs are at least 16px so phones do not zoom on focus.
- The big screen has its own scale in `screen.css`, sized in `vmin` so it reads from across a room: `--tv-hero` `clamp(2.5rem, 12.5vmin, 13rem)`, `--tv-big` `clamp(1.6rem, 5.4vmin, 4.8rem)`, `--tv-read` `clamp(1.1rem, 3.2vmin, 2.6rem)`, `--tv-small` `clamp(.95rem, 2.3vmin, 1.8rem)`.

## Space, size and shape

- `--gap` 16px between things, `--pad` 24px inside cards. The big screen overrides both with `vmin` clamps.
- Other spacing is in rem steps: .25, .5, .75, 1, 1.25, 1.5, 2, 3.
- `--radius-card` 20px for cards and panels, `--radius-control` 12px for buttons, fields and segmented controls. Circles (`50%`) for icon buttons, avatars and dots; `999px` for pills and meters.
- `--control-height` 2.75rem (44px) is the smallest tap target. The guest page raises inputs and buttons to 3rem.
- Page widths: shared `main` 40rem, guest page 780px, admin 1080px, host up to 1550px beside a 224px sidebar, home page 1240px.
- Each page sets its own breakpoints. Design from 320px phones up to 1440px desktops; the browser smoke test checks that range.

## Components

All shared ones live in `style.css`. Reuse them before writing page CSS.

- **Buttons.** Default `button` is the primary action: `--accent` background, `--button-ink` text, weight 600, radius 12px. Hover mixes 15% `--text` into the accent. Variants: `.ghost` (secondary, `--raised` with a `--line` border), `.quiet` (text only, `--muted`), `.danger` (`--pill-negative-bg` with a `--negative` border), `.small` (2.5rem tall, `--fs-small`), `.icon` (round, 44px square), `.link` (looks like a link). Disabled is half opacity. Press scales to .97.
- **Segmented control.** `.segmented` for one choice out of a few. Mark the chosen button `aria-pressed="true"`.
- **Tabs.** Text tabs on a `--line` bottom border; the current one gets a 2px `--accent` underline (`.entry-tabs` on home, `.admin-tabs` on admin). Mark it `aria-selected` or `aria-current`.
- **Navigation.** Host: fixed sidebar with icon plus label buttons; current item is `--pill-accent-bg` with `--accent` text. Guest: `.guest-nav`, a pill bar of equal buttons with the same current style. Every page header carries the wordmark and the theme picker.
- **Cards.** `.panel` (or `.card` on host and desktop): `--surface`, 1px `--line` border, 20px radius, `--pad` padding. Live or current cards use `--pill-accent-bg` or a gradient from it to `--surface`.
- **Lists.** Plain `ol`/`ul` with no bullets, flex rows, `.6rem` vertical padding, `--rule` lines between rows where needed. Your own row (`li.me`) is bold and tinted. Lists where order matters show the number in a muted tabular column.
- **Tags and pills.** Small, weight 600, `999px` radius, a tinted background with matching text (`.tag.singing` uses accent).
- **Forms.** `form` is a grid with a .6rem gap. Fields are `--raised` with a `--line` hairline, 12px radius, `--line-strong` on hover, `--faint` placeholder. Labels sit above the field in `--muted` at `--fs-small`.
- **Toast.** `#flash`, set with `flash(message, kind)` in `kq.js`. Floats at the bottom center, `--raised` with a `--line-strong` border and the only shadow (`0 12px 32px #0009`). Errors turn the text `--negative`; `ok` turns the border `--positive`. Hides after 4 seconds.
- **Floating menu.** The host's singer menu uses the native `popover`, `--raised`, 16px radius and the toast's shadow.
- **Connection status.** `[data-connection]` shows a 6px dot: `--positive` with "Live updates", `--warning` with "Reconnecting…".
- **Empty states.** Muted text that says what to do next ("Nobody's singing yet.", "Request a song and you'll get a turn."). On the host screens tab, a dashed empty TV next to the link that fills it.
- **Error states.** Messages come from the server as a plain sentence and show in the toast. A missing room gets its own page with a big heading and one button home.
- **Focus.** `:focus-visible` is a 2px `--accent` outline with a 2px offset. The home, guest, host and admin pages have a skip link (`.skip-link`); use `.sr` for screen reader text.

## Motion

- Tokens: `--quick` 120ms (presses, hovers, menus), `--standard` 240ms (toasts, docks, fades), `--slow` 1400ms (the karaoke wipe). Easing: `--ease` `cubic-bezier(.2,0,0,1)` for moves, `--ease-out` `cubic-bezier(.05,.7,.1,1)` for things arriving.
- Signature move: the karaoke wipe. A singer's name fills left to right with the sunset gradient when they go on stage (`wipeText()` in `kq.js` plus the `.sung` class).
- Things that arrive rise 8 to 12px and fade in. List results stagger by 40ms each.
- All movement sits inside `@media (prefers-reduced-motion: no-preference)`. With reduced motion, things fade or just appear; the screen identify flash still shows because it does not move.
- With `prefers-reduced-transparency: reduce`, the big screen's overlays use a solid `--ground`.

## Icons

- Inline SVG, 24 by 24 viewBox, `fill="none"`, `stroke="currentColor"`, stroke width 1.7, round caps and joins, `aria-hidden="true"`. They inherit text color.
- Inside buttons they are 1.2rem. Icon-only buttons need an `aria-label` and a matching `title`.
- No icon font and no icon library. Simple chevrons are drawn in CSS (`.chev`).

## Logo and name

- The name is **KaraQueue**: one word, capital K and Q. Never "Karaqueue", "Kara Queue" or "KQ" in text people read. `kq` is fine in code (`kq.js`, `kq:` storage keys).
- The wordmark is the brand symbol plus the name in the display face at weight 750, tracking -0.04em (`.wordmark`).
- The brand symbol is three rounded bars, 5px wide and tilted 16 degrees, in `--accent`, `--gold` and `--red` at 15, 28 and 21px tall (`.brand-symbol` with three `<i>`). It follows the theme. Use the markup, not an image.
- `karaqueue/static/KaraQueue-Icon.png` (1024 by 1024) is the favicon and the desktop app icon.
- Taglines in use: "Make a night of it" (home title), "Good friends. Questionable high notes." (home hero), "Good nights. Fair turns." (host sidebar).

## Copy voice

- Short, warm and plain, like a friend running the night. Second person, contractions, no jargon. "The host lets the screen in", not "authorize the display client".
- Errors say what happened and what to do: "Can't reach the party. Check you're on the Wi-Fi, then try again."
- Buttons are verbs or short phrases in sentence case: "Join room", "Add a song".
- Sentence case for headings and labels. No em dashes anywhere. Use the real ellipsis character for in-progress text ("Reconnecting…").
- Made-up names in examples and screenshots, never real guests.

## Known drift

Recorded here, not fixed. Fix these in their own changes.

1. Radius: besides the 20px and 12px tokens, the page CSS uses literal 8, 10, 14, 16 and 18px and `.5rem` radii. 14px is common enough to deserve a token.
2. `style.css` sets the toast radius twice (12px, then 14px) and disabled button opacity twice (.4, then .5).
3. Durations: a later block in `style.css` uses a literal `120ms` instead of `--quick`, and pages use literal 360, 380, 400, 500 and 600ms beside the three motion tokens.
4. The floating shadow `0 12px 32px #0009` is written out in two files instead of a token; the home art has three more literal shadows.
5. Type: most page CSS uses literal sizes (.75rem, .875rem and others) instead of the four `--fs` tokens, and writes `.8125rem` where it means `--fs-small`.
6. `room.css` and `admin.css` open with an older indented block that later rules override (guest page width 34rem then 780px, admin width 48rem then 1080px, admin `.wordmark` at weight 700 where every other page uses 750).
7. Screen reader helpers are defined several times: `.sr` and `.visually-hidden` in `style.css`, again in `room.css` and `host.css`, along with a repeated `[hidden]` rule.
8. The volume slider comment in `host.css` describes a data URL with `#fff5ee` and `#4d4038`; the code now uses a token gradient, and neither color matches a token.
9. Page titles use three patterns: "KaraQueue · Make a night of it", "Host | KaraQueue", "KaraQueue screen".
10. Room and host pages each define their own `icon()` helper instead of sharing one in `kq.js`.
11. The big screen and the desktop dashboard have no skip link.
12. Breakpoints differ on every page (380, 400, 500, 520, 560, 600, 640, 700, 760, 950, 1000 and 1200px) with no shared set.
