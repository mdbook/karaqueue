// Draws CD+G karaoke graphics (the lyrics half of an MP3+CDG song) on a canvas, in step with the music.
//
// A .cdg file is a stream of 24-byte packets, 300 a second. Each one paints tiles, clears the
// screen, scrolls or changes the 16-colour palette of a 300x216 picture. To show the picture at
// a moment in the song, play every packet up to that moment.
"use strict";

function CdgPlayer(canvas) {
  const W = 300, H = 216, PER_SECOND = 300;
  const ctx = canvas.getContext("2d");
  canvas.width = W; canvas.height = H;
  const image = ctx.createImageData(W, H);
  let packets = new Uint8Array(0), done = 0, dirty = true;
  let pixels = new Uint8Array(W * H), palette = new Array(16).fill([0, 0, 0]);
  let offsetX = 0, offsetY = 0;

  function reset() {
    done = 0; dirty = true; offsetX = offsetY = 0;
    pixels = new Uint8Array(W * H);
    palette = new Array(16).fill([0, 0, 0]);
  }

  function fill(color, x0, y0, x1, y1) {
    for (let y = y0; y < y1; y++) pixels.fill(color, y * W + x0, y * W + x1);
  }

  function tile(d, xor) {
    const c0 = d[0] & 15, c1 = d[1] & 15, row = d[2] & 31, col = d[3] & 63;
    if (row >= 18 || col >= 50) return;
    for (let y = 0; y < 12; y++) {
      const bits = d[4 + y] & 63, base = (row * 12 + y) * W + col * 6;
      for (let x = 0; x < 6; x++) {
        const color = bits & (32 >> x) ? c1 : c0;
        pixels[base + x] = xor ? pixels[base + x] ^ color : color;
      }
    }
  }

  function scroll(d, wrap) {
    const color = d[0] & 15, h = d[1] & 63, v = d[2] & 63;
    offsetX = h & 7; offsetY = v & 15;
    const dx = [0, 6, -6][(h >> 4) & 3] || 0, dy = [0, 12, -12][(v >> 4) & 3] || 0;
    if (!dx && !dy) return;
    const old = pixels;
    pixels = new Uint8Array(W * H);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        let sx = x - dx, sy = y - dy;
        if (wrap) { sx = (sx + W) % W; sy = (sy + H) % H; }
        pixels[y * W + x] = sx >= 0 && sx < W && sy >= 0 && sy < H ? old[sy * W + sx] : color;
      }
    }
  }

  function colors(d, first) {
    for (let i = 0; i < 8; i++) {
      const hi = d[i * 2], lo = d[i * 2 + 1];
      palette[first + i] = [((hi & 60) >> 2) * 17, (((hi & 3) << 2) | ((lo & 48) >> 4)) * 17, (lo & 15) * 17];
    }
  }

  function step(at) {
    if ((packets[at] & 63) !== 9) return; // not a graphics packet
    const d = packets.subarray(at + 4, at + 20);
    switch (packets[at + 1] & 63) {
      case 1: fill(d[0] & 15, 0, 0, W, H); break;                        // clear the screen
      case 2: { const c = d[0] & 15;                                     // paint the border
        fill(c, 0, 0, W, 12); fill(c, 0, H - 12, W, H); fill(c, 0, 12, 6, H - 12); fill(c, W - 6, 12, W, H - 12); break; }
      case 6: tile(d, false); break;
      case 38: tile(d, true); break;
      case 20: scroll(d, false); break;
      case 24: scroll(d, true); break;
      case 30: colors(d, 0); break;
      case 31: colors(d, 8); break;
      default: return;
    }
    dirty = true;
  }

  function draw() {
    const out = image.data;
    for (let i = 0; i < pixels.length; i++) {
      const [r, g, b] = palette[pixels[i]];
      out[i * 4] = r; out[i * 4 + 1] = g; out[i * 4 + 2] = b; out[i * 4 + 3] = 255;
    }
    ctx.putImageData(image, -offsetX, -offsetY);
    dirty = false;
  }

  return {
    load(buffer) { packets = new Uint8Array(buffer); reset(); },
    clear() { packets = new Uint8Array(0); reset(); ctx.clearRect(0, 0, W, H); },
    // Bring the picture to `seconds` into the song. Going backwards replays from the start.
    sync(seconds) {
      const target = Math.min(Math.floor(seconds * PER_SECOND), packets.length / 24 | 0);
      if (target < done) reset();
      for (; done < target; done++) step(done * 24);
      if (dirty) draw();
    },
  };
}

if (typeof module !== "undefined") module.exports = { CdgPlayer };
