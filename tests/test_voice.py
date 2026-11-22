"""The voice chain (kq-voice.js) rendered offline in a real browser: does the chipmunk raise the
pitch, does the echo repeat, does the balance pan. Skips itself when Playwright or Chromium is missing."""
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

VOICE_JS = (Path(__file__).resolve().parent.parent / "karaqueue" / "static" / "kq-voice.js").read_text()

# Renders `seconds` of a 440 Hz tone (or one click) through the chain and measures the result.
PAGE = """
async (opts) => {
  const rate = 44100, ctx = new OfflineAudioContext(2, rate * opts.seconds, rate);
  const chain = KQVoice.build(ctx);
  const key = opts.key ? KQVoice.shifter(ctx, opts.key) : null;
  const osc = ctx.createBufferSource();
  const buffer = ctx.createBuffer(1, rate * opts.seconds, rate);
  const data = buffer.getChannelData(0);
  if (opts.click) data[0] = 1;
  else for (let i = 0; i < data.length; i++) data[i] = 0.3 * Math.sin(2 * Math.PI * 440 * i / rate);
  osc.buffer = buffer;
  if (key) { osc.connect(key.input); key.output.connect(ctx.destination); }
  const strip = chain.add(osc, 1, 0);
  strip.set({ volume: opts.silence_mic ? 0 : 100, pan: opts.pan || 0 });
  chain.set({ volume: 100, reverb: 0, echo: opts.echo || 0, voice: opts.voice || "normal", muted: false });
  chain.mix.connect(ctx.destination);
  osc.start();
  const out = await ctx.startRendering();
  const left = out.getChannelData(0), right = out.getChannelData(1);
  const from = Math.floor(rate * opts.from);
  let crossings = 0, last = 0, energyL = 0, energyR = 0;
  for (let i = from; i < left.length; i++) {
    if (last < 0 && left[i] >= 0) crossings++;
    last = left[i];
    energyL += left[i] * left[i];
    energyR += right[i] * right[i];
  }
  const echo = [];
  for (const t of [0.05, 0.28, 0.56]) {
    let peak = 0;
    for (let i = Math.floor(rate * (t - 0.01)); i < Math.floor(rate * (t + 0.01)); i++) peak = Math.max(peak, Math.abs(left[i]));
    echo.push(peak);
  }
  return { hz: crossings / (opts.seconds - opts.from), energyL, energyR, echo };
}
"""


@pytest.fixture(scope="module")
def render():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser installed here
            pytest.skip(f"Chromium isn't available: {e}")
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(content=VOICE_JS)
        yield lambda **opts: page.evaluate(PAGE, {"seconds": 2, "from": 1, **opts})
        browser.close()


def test_normal_voice_keeps_its_pitch(render):
    assert render()["hz"] == pytest.approx(440, abs=8)


def test_chipmunk_raises_the_pitch_by_eight_semitones(render):
    expected = 440 * 2 ** (8 / 12)
    assert render(voice="chipmunk")["hz"] == pytest.approx(expected, rel=0.03)


def test_echo_repeats_the_sound(render):
    plain, echoed = render(click=True, seconds=1, **{"from": 0}), render(click=True, seconds=1, echo=60, **{"from": 0})
    assert echoed["echo"][1] > 0.05 > plain["echo"][1]


def test_balance_moves_a_mic_to_one_side(render):
    left, right = render(pan=-100), render(pan=100)
    assert left["energyL"] > 20 * left["energyR"]
    assert right["energyR"] > 20 * right["energyL"]


@pytest.mark.parametrize("semitones", [12, -12, -5])
def test_song_key_shifter_moves_the_pitch(render, semitones):
    # With the mic strip silenced, what's left is the shifter's own output.
    out = render(key=semitones, silence_mic=True)
    assert out["hz"] == pytest.approx(440 * 2 ** (semitones / 12), rel=0.06)
