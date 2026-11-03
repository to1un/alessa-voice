// Port of the Voicebox painter in alessa_voice.py, driven by a scripted timeline instead of a microphone.
// render(t) is deterministic: the same t always gives the same frame, so a capture script can step it.
(function () {
  const COLS = 21, SEGS = 7, SW = 7, GAP = 5, SH = 3, SGAP = 2, W = COLS * 12 + 16, H = 66;
  const RED = [255, 36, 56];
  const STEP = 1 / 60;
  // one loop of the demo, in seconds
  const T = { listen: 0.6, think: 5.4, done: 7.4, idle: 8.3, end: 10 };

  // a speech-like loudness envelope: syllable bursts with short pauses between words
  function level(t) {
    const s = t - T.listen;
    if (s < 0 || t >= T.think) return 0;
    const word = Math.floor(s / 0.62), inWord = (s % 0.62) / 0.62;
    if (inWord > 0.78 || word % 4 === 3 && inWord > 0.45) return 0.05;
    const syl = Math.abs(Math.sin(s * 11.3 + word)) * 0.55 + Math.abs(Math.sin(s * 4.1 + word * 1.7)) * 0.45;
    return Math.min(1, 0.25 + syl * 0.75);
  }

  function state(t) {
    if (t < T.listen) return 'idle';
    if (t < T.think) return 'listening';
    if (t < T.done) return 'thinking';
    if (t < T.idle) return 'done';
    return 'idle';
  }

  function make(canvas, opts = {}) {
    const scale = opts.scale || 2;
    canvas.width = W * scale; canvas.height = H * scale;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    const p = canvas.getContext('2d');
    let leds, lvl, simT;
    const reset = () => { leds = Array.from({ length: COLS }, () => new Array(SEGS).fill(0)); lvl = 0; simT = 0; };
    reset();

    function target(i, st, t, since) {
      const d = Math.abs(i - (COLS - 1) / 2) / ((COLS - 1) / 2);
      if (st === 'thinking') {
        const ph = (Math.sin(since * 4.2) + 1) / 2;
        return Math.max(0, 1 - Math.abs((1 - d) - ph) * 4.5) * 0.9;
      }
      if (st === 'done') return 1;
      if (st === 'idle') return 0;
      return lvl * (1 - d * 0.7) * (0.82 + 0.18 * Math.sin(t * 7 - d * 5));
    }

    function step(t) {
      const st = state(t);
      const lt = level(t);
      lvl += (lt - lvl) * (lt > lvl ? 0.6 : 0.15);  // fast attack, slow decay
      const since = t - ({ listening: T.listen, thinking: T.think, done: T.done }[st] || 0);
      for (let i = 0; i < COLS; i++) {
        const v = target(i, st, t, since);
        for (let j = 0; j < SEGS; j++) {
          const dj = Math.abs(j - (SEGS - 1) / 2) / ((SEGS - 1) / 2 + 0.5);
          const tg = Math.max(0, Math.min(1, (v * 1.25 - dj) * 3));
          leds[i][j] += (tg - leds[i][j]) * (tg > leds[i][j] ? 0.45 : 0.10);
        }
      }
    }

    function render(t) {
      t = ((t % T.end) + T.end) % T.end;
      if (t < simT) reset();
      while (simT + STEP <= t) { simT += STEP; step(simT); }
      const st = state(t);
      let alpha = 1;
      if (st === 'done') alpha = Math.max(0, 1 - (t - T.done) / 0.9);
      if (st === 'idle') alpha = 0;
      p.setTransform(scale, 0, 0, scale, 0, 0);
      p.clearRect(0, 0, W, H);
      if (opts.background) { p.fillStyle = opts.background; p.fillRect(0, 0, W, H); }
      p.globalAlpha = alpha;
      const cx = W / 2, cy = 24;
      for (let i = 0; i < COLS; i++) {
        const x = cx + (i - (COLS - 1) / 2) * (SW + GAP) - SW / 2;
        for (let j = 0; j < SEGS; j++) {
          const cur = leds[i][j];
          if (cur < 0.02) continue;
          p.fillStyle = `rgba(${RED[0]},${RED[1]},${RED[2]},${cur})`;
          p.fillRect(x, cy + (j - (SEGS - 1) / 2) * (SH + SGAP) - SH / 2, SW, SH);
        }
      }
      p.fillStyle = 'rgb(128,128,134)';
      p.font = '600 11pt sans-serif';
      p.letterSpacing = '5px';
      p.textAlign = 'center';
      p.fillText('ALESSA', cx + 2.5, cy + 36);
      p.globalAlpha = 1;
      return st;
    }
    return { render, reset };
  }

  window.Voicebox = { make, T, W, H, state };
})();
