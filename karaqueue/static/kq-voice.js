// The singer's voice chain (Web Audio), used by screen.html. Mic in, then an optional chipmunk
// pitch shift, then reverb and echo, all mixed into `mix`. Nothing here touches the page, so
// tests can run it in an OfflineAudioContext.
(function () {
  const CHIPMUNK_SEMITONES = 8;
  const GRAIN = 0.07; // seconds per pitch-shift window; shorter warbles, longer smears
  const ECHO_TIME = 0.28; // seconds between echoes
  const MAX_BOOST = 30; // dB a quiet mic can be turned up past its plain level; displays.py has the same

  // A made-up room: noise that fades out. Longer and slower fades sound like bigger rooms.
  function impulse(ctx, seconds, decay) {
    const length = Math.floor(ctx.sampleRate * seconds);
    const buffer = ctx.createBuffer(2, length, ctx.sampleRate);
    for (let c = 0; c < 2; c++) {
      const data = buffer.getChannelData(c);
      for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
    }
    return buffer;
  }

  // Real-time pitch shift (up or down) by resampling: two delay lines whose delay shrinks steadily play the
  // voice back faster, and crossfade as one wraps around. Pitch and formants (the throat and
  // mouth resonances) move up together, which is exactly the Alvin and the Chipmunks sound.
  function chipmunk(ctx, semitones) {
    const rate = Math.pow(2, semitones / 12);
    const size = Math.floor(ctx.sampleRate * GRAIN);
    const ramp = ctx.createBuffer(1, size, ctx.sampleRate), fade = ctx.createBuffer(1, size, ctx.sampleRate);
    const delays = ramp.getChannelData(0), gains = fade.getChannelData(0);
    for (let i = 0; i < size; i++) {
      delays[i] = (rate - 1) * GRAIN * (1 - i / size) + Math.max(0, 1 - rate) * GRAIN; // going down needs a head start
      gains[i] = Math.pow(Math.sin(Math.PI * i / size), 2); // two taps half a grain apart sum to 1
    }
    const input = ctx.createGain(), output = ctx.createGain(), start = ctx.currentTime + 0.01;
    for (const offset of [0, GRAIN / 2]) {
      const delay = ctx.createDelay(1), tap = ctx.createGain();
      delay.delayTime.value = 0;
      tap.gain.value = 0;
      for (const [buffer, param] of [[ramp, delay.delayTime], [fade, tap.gain]]) {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.loop = true;
        source.connect(param);
        source.start(start, offset);
      }
      input.connect(delay).connect(tap).connect(output);
    }
    return { input, output };
  }

  // Build the chain. Mics join it with add(): each one's channels are summed to the middle, so
  // a Scarlett 2i2 with a mic in each input, or several USB mics, all come out of both speakers.
  function build(ctx) {
    const bus = ctx.createGain(), plain = ctx.createGain(), shifted = ctx.createGain();
    const voice = ctx.createGain(), level = ctx.createGain(), dry = ctx.createGain(), wet = ctx.createGain();
    const echoSend = ctx.createGain(), echoDelay = ctx.createDelay(1), echoBack = ctx.createGain(), echoWet = ctx.createGain();
    const room = ctx.createConvolver(), pitch = chipmunk(ctx, CHIPMUNK_SEMITONES);
    // Several mics can add up louder than one; a gentle limiter keeps that from clipping.
    const mix = ctx.createDynamicsCompressor();
    mix.threshold.value = -8;
    mix.ratio.value = 12;
    mix.attack.value = 0.003;
    const sum = ctx.createGain();
    room.buffer = impulse(ctx, 2.4, 3);
    echoDelay.delayTime.value = ECHO_TIME;
    plain.gain.value = 1;
    shifted.gain.value = 0;

    bus.connect(plain).connect(voice);
    bus.connect(pitch.input);
    pitch.output.connect(shifted).connect(voice);
    voice.connect(level);
    level.connect(dry).connect(sum);
    level.connect(room).connect(wet).connect(sum);
    level.connect(echoSend).connect(echoDelay);
    echoDelay.connect(echoBack).connect(echoDelay);
    echoDelay.connect(echoWet).connect(sum);
    sum.connect(mix);

    // One mic: channel `ch` (1 and up) of `source`, or all its channels summed when ch is 0. It has
    // its own volume, boost and left to right balance (volume 0 to 100, boost 0 to MAX_BOOST dB,
    // pan -100 to 100). level() is what it puts into the mix right now, for the host's meter.
    function add(source, channels, ch) {
      const split = ctx.createChannelSplitter(Math.max(1, Math.min(channels || 1, 8)));
      const gain = ctx.createGain(), panner = ctx.createStereoPanner(), meter = ctx.createAnalyser();
      const samples = new Float32Array(meter.fftSize = 4096); // about the time between two meter reports
      source.connect(split);
      for (let i = 0; i < split.numberOfOutputs; i++) if (!ch || ch - 1 === i) split.connect(gain, i);
      gain.connect(panner).connect(bus);
      gain.connect(meter);
      return {
        set(m) {
          // The panner takes a little off the middle, so give it back.
          const boost = Math.pow(10, Math.max(0, Math.min(MAX_BOOST, m.boost || 0)) / 20);
          gain.gain.setTargetAtTime((m.volume / 100) * boost * Math.SQRT2, ctx.currentTime, 0.02);
          panner.pan.setTargetAtTime(Math.max(-1, Math.min(1, m.pan / 100)), ctx.currentTime, 0.02);
        },
        // [average, peak] in dB below full scale (0 is as loud as the speakers go before clipping).
        level() {
          meter.getFloatTimeDomainData(samples);
          let sum = 0, peak = 0;
          for (const x of samples) { sum += x * x; peak = Math.max(peak, Math.abs(x)); }
          const db = (v) => Math.round(Math.max(-60, Math.min(12, 20 * Math.log10(v / Math.SQRT2 || 1e-6))));
          return [db(Math.sqrt(sum / samples.length)), db(peak)];
        },
        remove() { try { source.disconnect(split); split.disconnect(); gain.disconnect(); panner.disconnect(); meter.disconnect(); } catch {} },
      };
    }

    // Settings are the host panel's: volume, reverb and echo 0 to 100, voice "normal" or "chipmunk".
    function set(s) {
      const now = ctx.currentTime, to = (param, value, slow) => param.setTargetAtTime(value, now, slow || 0.02);
      const r = s.reverb / 100, e = (s.echo || 0) / 100, chip = s.voice === "chipmunk";
      to(level.gain, s.muted ? 0 : (s.volume / 100) * 1.5);
      to(dry.gain, 1 - r * 0.35, 0.05);
      to(wet.gain, r * 1.2, 0.05);
      to(echoSend.gain, e ? 1 : 0, 0.05);
      to(echoWet.gain, e * 0.9, 0.05);
      to(echoBack.gain, 0.25 + e * 0.3, 0.05);
      to(plain.gain, chip ? 0 : 1, 0.03);
      to(shifted.gain, chip ? 1 : 0, 0.03);
    }
    return { mix, set, add };
  }

  window.KQVoice = { build, shifter: chipmunk, CHIPMUNK_SEMITONES, MAX_BOOST };
})();
