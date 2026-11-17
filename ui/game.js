/* Kitty Run: MEV Finder's own little game. The site's cat runs along a chain of blocks, jumps over sandwich bots, gas
   spikes, revert walls and front-runners, and catches coins ("Kept $..."). A rare arbitrage loop doubles coin value for a
   while. Sounds are synthesised with Web Audio (no files): cha-ching for a coin, bling for the loop.
   window.ArbSound is shared with the cat companion (a coin from a block replay also goes cha-ching).
   window.ArbGame.open() starts it; the cat opens it on click. */
(() => {
  const store = {
    get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
  };

  // ---- sound -----------------------------------------------------------------------------------------------------
  let actx = null;
  const sound = {
    on: store.get("arb-sound", "on") !== "off",
    ctx() {
      if (!this.on) return null;
      try {
        actx = actx || new (window.AudioContext || window.webkitAudioContext)();
        if (actx.state === "suspended") actx.resume();
        return actx;
      } catch (e) { return null; }
    },
    set(on) { this.on = on; store.set("arb-sound", on ? "on" : "off"); },
    tone(ctx, { type = "sine", freq, to, at = 0, dur = 0.3, gain = 0.12, attack = 0.004 }) {
      const t = ctx.currentTime + at, o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type; o.frequency.setValueAtTime(freq, t);
      if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(gain, t + attack); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + dur + 0.02);
    },
    noise(ctx, { at = 0, dur = 0.05, gain = 0.1, freq = 3000, q = 1.5 }) {
      const t = ctx.currentTime + at, len = Math.max(1, Math.floor(ctx.sampleRate * dur));
      const buf = ctx.createBuffer(1, len, ctx.sampleRate), data = buf.getChannelData(0);
      for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / len);
      const src = ctx.createBufferSource(), bp = ctx.createBiquadFilter(), g = ctx.createGain();
      src.buffer = buf; bp.type = "bandpass"; bp.frequency.value = freq; bp.Q.value = q; g.gain.value = gain;
      src.connect(bp).connect(g).connect(ctx.destination); src.start(t);
    },
    /** Cash register: a short mechanical "cha" and a bright two-bell "ching". */
    chaChing() {
      const ctx = this.ctx(); if (!ctx) return;
      this.noise(ctx, { dur: 0.045, gain: 0.16, freq: 2400, q: 0.9 });
      this.noise(ctx, { at: 0.05, dur: 0.03, gain: 0.1, freq: 4200, q: 1.2 });
      for (const [f, g] of [[2093, 0.09], [3136, 0.07], [4186, 0.04], [2637, 0.05]]) this.tone(ctx, { freq: f, at: 0.085, dur: 0.75, gain: g });
      this.tone(ctx, { type: "triangle", freq: 6272, at: 0.09, dur: 0.25, gain: 0.02 });
    },
    /** Sparkle: a quick rising arpeggio with a shimmer on top. */
    bling() {
      const ctx = this.ctx(); if (!ctx) return;
      [1319, 1661, 1976, 2637, 3322].forEach((f, i) => this.tone(ctx, { type: "triangle", freq: f, at: i * 0.055, dur: 0.38, gain: 0.07 }));
      this.tone(ctx, { freq: 5274, at: 0.22, dur: 0.6, gain: 0.025 });
      this.tone(ctx, { freq: 5588, at: 0.26, dur: 0.6, gain: 0.02 });
    },
    jump() { const ctx = this.ctx(); if (ctx) this.tone(ctx, { type: "square", freq: 420, to: 760, dur: 0.11, gain: 0.03 }); },
    thud() {
      const ctx = this.ctx(); if (!ctx) return;
      this.tone(ctx, { freq: 180, to: 55, dur: 0.32, gain: 0.18 });
      this.noise(ctx, { dur: 0.12, gain: 0.08, freq: 600, q: 0.7 });
    },
  };
  window.ArbSound = sound;

  // ---- drawing helpers -------------------------------------------------------------------------------------------
  const CAT = {
    tail: new Path2D("M88 86 C112 84 114 60 102 54 C96 51 92 58 98 62"),
    body: new Path2D("M30 92 C24 70 30 52 48 48 C60 45 76 46 84 56 C92 66 92 82 86 93 Z"),
    head: new Path2D("M28 50 C22 36 24 22 30 14 L38 26 C46 22 56 22 62 26 L72 13 C78 22 80 36 74 50 C66 60 38 61 28 50 Z"),
    nose: new Path2D("M48.5 46 L52.5 46 L50.5 48.6 Z"),
    whisk: new Path2D("M33 47 L21 45 M33 50 L22 52 M68 47 L80 45 M68 50 L79 52"),
  };
  const css = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fallback;
  const round = (ctx, x, y, w, h, r) => { ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h); };

  function drawCat(ctx, x, y, s, t, airborne, glow) {
    const fill = css("--cat-fill", "#fde9cc"), line = css("--cat-line", "#3a2e2a");
    ctx.save(); ctx.translate(x, y); ctx.scale(s, s); ctx.translate(-14, -100);
    if (glow) { ctx.shadowColor = "#facc15"; ctx.shadowBlur = 22; }
    ctx.lineWidth = 3.2; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = line;
    ctx.save(); ctx.translate(88, 86); ctx.rotate(Math.sin(t * 9) * 0.22); ctx.translate(-88, -86); ctx.stroke(CAT.tail); ctx.restore();
    ctx.fillStyle = fill; ctx.fill(CAT.body); ctx.stroke(CAT.body); ctx.fill(CAT.head); ctx.stroke(CAT.head);
    ctx.shadowBlur = 0;
    ctx.fillStyle = line;
    const blink = (t % 3.2) < 0.12 ? 0.25 : 1;
    for (const ex of [41, 60]) { ctx.beginPath(); ctx.ellipse(ex, 39, 3.4, 4.2 * blink, 0, 0, Math.PI * 2); ctx.fill(); }
    ctx.fillStyle = "#f472b6"; ctx.fill(CAT.nose);
    ctx.stroke(CAT.whisk);
    // running legs: two pairs swing in turn; tucked up in the air
    const swing = airborne ? 0 : Math.sin(t * 22) * 5;
    ctx.beginPath();
    for (const [lx, dir] of [[44, 1], [70, -1]]) {
      const dx = swing * dir, lift = airborne ? -5 : 0;
      ctx.moveTo(lx - 4, 90); ctx.quadraticCurveTo(lx - 4 + dx, 99 + lift, lx + dx, 99 + lift); ctx.quadraticCurveTo(lx + 4 + dx, 99 + lift, lx + 4, 90);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawCoin(ctx, x, y, r, t) {
    const squash = Math.abs(Math.cos(t * 4 + x * 0.01));
    ctx.save(); ctx.translate(x, y); ctx.scale(Math.max(0.25, squash), 1);
    const g = ctx.createRadialGradient(-r * 0.3, -r * 0.3, 1, 0, 0, r);
    g.addColorStop(0, "#fde68a"); g.addColorStop(0.6, "#f59e0b"); g.addColorStop(1, "#b45309");
    ctx.fillStyle = g; ctx.strokeStyle = "#92400e"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    if (squash > 0.5) { ctx.fillStyle = "#92400e"; ctx.font = `700 ${r * 1.15}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText("$", 0, 1); }
    ctx.restore();
  }

  function drawLoop(ctx, x, y, t) {  // the arbitrage-loop bonus: three pools in a ring, like the logo
    ctx.save(); ctx.translate(x, y); ctx.rotate(t * 2);
    ctx.shadowColor = "#38bdf8"; ctx.shadowBlur = 16;
    ctx.strokeStyle = "#2563eb"; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(0, 0, 15, 0, Math.PI * 2); ctx.stroke();
    ctx.shadowBlur = 0;
    const pts = [0, 1, 2].map((i) => [Math.cos(i * 2.094 - 1.57) * 9, Math.sin(i * 2.094 - 1.57) * 9]);
    ctx.strokeStyle = "#0ea5e9"; ctx.beginPath(); pts.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py))); ctx.closePath(); ctx.stroke();
    pts.forEach(([px, py], i) => { ctx.fillStyle = i === 0 ? "#facc15" : "#fff"; ctx.beginPath(); ctx.arc(px, py, 3.2, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = "#1e3a8a"; ctx.lineWidth = 1.5; ctx.stroke(); });
    ctx.restore();
  }

  const OBSTACLES = {
    sandwich: { w: 40, h: 34, lose: "Sandwiched!", why: "A sandwich bot caught you between its buy and its sell." },
    gas: { w: 22, h: 46, lose: "Out of gas!", why: "Gas spiked and your transaction ran dry." },
    revert: { w: 32, h: 38, lose: "Reverted!", why: "Execution reverted. Better luck next block." },
    frontrunner: { w: 42, h: 22, fly: true, lose: "Front-run!", why: "A front-runner saw you in the mempool and got there first." },
  };

  function drawObstacle(ctx, o, t) {
    const { x, y, kind } = o, { w, h } = OBSTACLES[kind];
    ctx.save(); ctx.translate(x, y);
    ctx.lineWidth = 2.2; ctx.lineJoin = "round"; ctx.strokeStyle = "#3a2e2a";
    if (kind === "sandwich") {  // two bread slices with a filling and grumpy eyes
      ctx.fillStyle = "#f6c27a"; round(ctx, 0, 0, w, 12, 7); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#4ade80"; ctx.beginPath(); for (let i = 0; i <= 8; i++) ctx.lineTo(i * w / 8, 13 + (i % 2) * 4); ctx.lineTo(w, 19); ctx.lineTo(0, 19); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#ef4444"; ctx.fillRect(2, 18, w - 4, 4);
      ctx.fillStyle = "#e9a95c"; round(ctx, 0, 22, w, 12, 6); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#3a2e2a"; ctx.beginPath(); ctx.arc(13, 6, 2.2, 0, 7); ctx.arc(27, 6, 2.2, 0, 7); ctx.fill();
      ctx.beginPath(); ctx.moveTo(9, 2); ctx.lineTo(16, 4); ctx.moveTo(31, 2); ctx.lineTo(24, 4); ctx.stroke();
    } else if (kind === "gas") {  // a flame on a little pump
      const flick = Math.sin(t * 18) * 2;
      ctx.fillStyle = "#64748b"; round(ctx, 4, h - 14, w - 8, 14, 3); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#f97316"; ctx.beginPath(); ctx.moveTo(w / 2, flick); ctx.quadraticCurveTo(w + 2, h * 0.45, w / 2 + 6, h - 14); ctx.lineTo(w / 2 - 6, h - 14); ctx.quadraticCurveTo(-2, h * 0.45, w / 2, flick); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#fde047"; ctx.beginPath(); ctx.moveTo(w / 2, h * 0.38); ctx.quadraticCurveTo(w / 2 + 6, h * 0.62, w / 2, h - 16); ctx.quadraticCurveTo(w / 2 - 6, h * 0.62, w / 2, h * 0.38); ctx.fill();
    } else if (kind === "revert") {  // a red block with a cross
      ctx.fillStyle = "#ef4444"; round(ctx, 0, 0, w, h, 6); ctx.fill(); ctx.stroke();
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 4; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(9, 10); ctx.lineTo(w - 9, h - 10); ctx.moveTo(w - 9, 10); ctx.lineTo(9, h - 10); ctx.stroke();
    } else {  // front-runner: a small fast bot with a speed streak
      ctx.strokeStyle = "rgba(37,99,235,.35)"; ctx.lineWidth = 2; ctx.beginPath();
      for (let i = 0; i < 3; i++) { ctx.moveTo(w + 4, 5 + i * 6); ctx.lineTo(w + 16 + i * 4, 5 + i * 6); } ctx.stroke();
      ctx.strokeStyle = "#3a2e2a"; ctx.lineWidth = 2.2;
      ctx.fillStyle = "#a78bfa"; ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(14, 0); ctx.lineTo(w, 3); ctx.lineTo(w, h - 3); ctx.lineTo(14, h); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(16, h / 2 - 1, 4, 0, 7); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#3a2e2a"; ctx.beginPath(); ctx.arc(15, h / 2 - 1, 1.8, 0, 7); ctx.fill();
    }
    ctx.restore();
  }

  // ---- the game --------------------------------------------------------------------------------------------------
  // the playfield: 860 x 300 units on wider screens, 520 x 300 on phones so everything stays big enough to tap
  let W = 860;
  const H = 300, GROUND = 252, CAT_X = 90, CAT_SCALE = 0.5;
  let overlay = null, canvas = null, ctx = null, raf = 0, last = 0, state = null;
  const best = () => Number(store.get("arb-kitty-best", "0")) || 0;

  function fresh() {
    return { phase: "ready", t: 0, speed: 330, dist: 0, kept: 0, y: 0, vy: 0, holding: false, onGround: true,
             obstacles: [], coins: [], loops: [], pops: [], nextObstacle: 1.2, nextCoin: 0.6, nextLoop: 14, double: 0, lost: null,
             block: Number(window.ArbHeadBlock) || 26000000 };
  }

  function jumpPress() {
    if (!state) return;
    if (state.phase === "ready" || state.phase === "over") return start();
    if (state.phase === "paused") { state.phase = "run"; return; }
    state.holding = true;
    if (state.onGround) { state.vy = -640; state.onGround = false; sound.jump(); }
  }
  const jumpRelease = () => { if (state) state.holding = false; };

  function start() {
    state = fresh(); state.phase = "run"; last = performance.now();
    sound.ctx();  // unlock audio inside the click / key press
  }

  function spawn(dt) {
    const s = state;
    s.nextObstacle -= dt; s.nextCoin -= dt; s.nextLoop -= dt;
    if (s.nextObstacle <= 0) {
      const kinds = ["sandwich", "sandwich", "revert", "gas"].concat(s.dist > 4000 ? ["frontrunner"] : []);
      const kind = kinds[Math.floor(Math.random() * kinds.length)], o = OBSTACLES[kind];
      const y = o.fly ? GROUND - o.h - 46 - Math.random() * 14 : GROUND - o.h;
      s.obstacles.push({ kind, x: W + 20, y });
      const gap = Math.max(0.55, 1.35 - s.speed / 1400) + Math.random() * 0.9;
      s.nextObstacle = gap;
      if (Math.random() < 0.18 && s.dist > 2500) s.nextObstacle = 0.32 + Math.random() * 0.1;  // a quick pair
    }
    if (s.nextCoin <= 0) {
      const n = 3 + Math.floor(Math.random() * 4), high = Math.random() < 0.5;
      for (let i = 0; i < n; i++) {
        const arc = high ? Math.sin((i / (n - 1)) * Math.PI) * 70 : 0;
        s.coins.push({ x: W + 30 + i * 30, y: GROUND - 28 - arc - (high ? 20 : 0) });
      }
      s.nextCoin = 1.1 + Math.random() * 1.6;
    }
    if (s.nextLoop <= 0) { s.loops.push({ x: W + 40, y: GROUND - 110 }); s.nextLoop = 16 + Math.random() * 12; }
  }

  function update(dt) {
    const s = state;
    s.t += dt;
    if (s.phase !== "run") return;
    s.speed = Math.min(820, 330 + s.t * 9);
    const dx = s.speed * dt;
    s.dist += dx; s.block += dx / 40;
    // jump physics: a held jump floats higher, a tap is a hop
    const gravity = s.vy < 0 && s.holding ? 1500 : 2600;
    s.vy += gravity * dt; s.y += s.vy * dt;
    if (s.y >= 0) { s.y = 0; s.vy = 0; s.onGround = true; }
    spawn(dt);
    for (const list of [s.obstacles, s.coins, s.loops]) for (const it of list) it.x -= dx;
    s.obstacles = s.obstacles.filter((o) => o.x > -60);
    s.coins = s.coins.filter((c) => c.x > -20 && !c.got);
    s.loops = s.loops.filter((l) => l.x > -30 && !l.got);
    s.pops = s.pops.filter((p) => (p.life -= dt) > 0);
    if (s.double > 0) s.double -= dt;
    // the cat's box (forgiving)
    const cy = GROUND + s.y, box = { l: CAT_X - 2, r: CAT_X + 40, t: cy - 44, b: cy - 4 };
    for (const o of s.obstacles) {
      const d = OBSTACLES[o.kind], pad = 5;
      if (box.r > o.x + pad && box.l < o.x + d.w - pad && box.b > o.y + pad && box.t < o.y + d.h - pad) {
        s.phase = "over"; s.lost = o.kind; sound.thud();
        const score = Math.round(s.kept * 100) / 100;
        if (score > best()) store.set("arb-kitty-best", String(score));
        return;
      }
    }
    const magnet = s.double > 0 ? 70 : 0;
    for (const c of s.coins) {
      const ccx = c.x, ccy = c.y;
      if (magnet && Math.abs(ccx - (CAT_X + 20)) < 140) { c.x += ((CAT_X + 20) - ccx) * 0.12; c.y += ((cy - 25) - ccy) * 0.12; }
      if (ccx > box.l - 8 && ccx < box.r + 8 && ccy > box.t - 8 && ccy < box.b + 8) {
        c.got = true;
        const value = s.double > 0 ? 2 : 1;
        s.kept += value; sound.chaChing();
        s.pops.push({ x: ccx, y: ccy, text: `+$${value}`, life: 0.7 });
      }
    }
    for (const l of s.loops) {
      if (l.x > box.l - 14 && l.x < box.r + 14 && l.y > box.t - 20 && l.y < box.b + 14) {
        l.got = true; s.double = 6; sound.bling();
        s.pops.push({ x: l.x, y: l.y, text: "Arbitrage loop! 2x", life: 1.1 });
      }
    }
  }

  function render() {
    const s = state, dark = document.documentElement.dataset.theme === "dark";
    ctx.clearRect(0, 0, W, H);
    const sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, dark ? "#0b1530" : "#eaf2ff"); sky.addColorStop(1, dark ? "#111c38" : "#ffffff");
    ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
    // distant chain: faint linked blocks drifting slowly
    ctx.strokeStyle = dark ? "rgba(148,163,184,.18)" : "rgba(37,99,235,.10)"; ctx.lineWidth = 2;
    const off = -(s.dist * 0.15) % 120;
    for (let x = off; x < W + 120; x += 120) { round(ctx, x, 70, 54, 36, 8); ctx.stroke(); ctx.beginPath(); ctx.moveTo(x + 54, 88); ctx.lineTo(x + 120, 88); ctx.stroke(); }
    // ground: a chain of blocks with numbers
    const bw = 64, goff = -(s.dist % bw);
    for (let i = -1; i < W / bw + 2; i++) {
      const x = goff + i * bw;
      ctx.fillStyle = dark ? "#1e293b" : "#dbe7fb"; round(ctx, x + 2, GROUND, bw - 4, 22, 5); ctx.fill();
      ctx.strokeStyle = dark ? "#334155" : "#b6c9ec"; ctx.lineWidth = 1.5; ctx.stroke();
      const n = Math.floor(s.block) + i + Math.floor(s.dist / bw);
      ctx.fillStyle = dark ? "#64748b" : "#7a8fb5"; ctx.font = "600 9px ui-monospace, monospace"; ctx.textAlign = "center";
      ctx.fillText(String(n), x + bw / 2, GROUND + 15);
    }
    for (const c of s.coins) drawCoin(ctx, c.x, c.y, 9, s.t);
    for (const l of s.loops) drawLoop(ctx, l.x, l.y, s.t);
    for (const o of s.obstacles) drawObstacle(ctx, o, s.t);
    drawCat(ctx, CAT_X, GROUND + s.y + 3, CAT_SCALE, s.t, !s.onGround, s.double > 0);
    for (const p of s.pops) {
      ctx.globalAlpha = Math.min(1, p.life * 2); ctx.fillStyle = "#b45309"; ctx.font = "700 14px system-ui, sans-serif"; ctx.textAlign = "center";
      ctx.fillText(p.text, p.x, p.y - (0.8 - p.life) * 40); ctx.globalAlpha = 1;
    }
    // score
    ctx.textAlign = "left"; ctx.fillStyle = dark ? "#e2e8f0" : "#0a1832"; ctx.font = "700 16px system-ui, sans-serif";
    ctx.fillText(`Kept $${s.kept.toLocaleString()}`, 16, 28);
    ctx.font = "500 12px system-ui, sans-serif"; ctx.fillStyle = dark ? "#94a3b8" : "#5b6b86";
    ctx.fillText(`Best $${best().toLocaleString()}  ·  ${Math.floor(s.dist / 10).toLocaleString()} m`, 16, 46);
    if (s.double > 0) { ctx.fillStyle = "#2563eb"; ctx.font = "700 13px system-ui, sans-serif"; ctx.fillText(`2x coins  ${s.double.toFixed(1)} s`, 16, 64); }
    if (s.phase !== "run") {
      ctx.fillStyle = dark ? "rgba(2,6,23,.6)" : "rgba(255,255,255,.72)"; ctx.fillRect(0, 0, W, H);
      ctx.textAlign = "center"; ctx.fillStyle = dark ? "#f8fafc" : "#0a1832";
      if (s.phase === "ready") {
        ctx.font = "800 26px system-ui, sans-serif"; ctx.fillText("Kitty Run", W / 2, 110);
        ctx.font = "500 15px system-ui, sans-serif"; ctx.fillText(W < 700 ? "Jump over MEV bots, catch coins." : "Jump over MEV bots, catch coins. Hold to jump higher.", W / 2, 140);
        ctx.font = "600 14px system-ui, sans-serif"; ctx.fillStyle = "#2563eb"; ctx.fillText("Press Space or tap to start", W / 2, 172);
      } else if (s.phase === "paused") {
        ctx.font = "800 24px system-ui, sans-serif"; ctx.fillText("Paused", W / 2, 130);
        ctx.font = "500 14px system-ui, sans-serif"; ctx.fillText("Press Space or tap to go on", W / 2, 160);
      } else {
        const o = OBSTACLES[s.lost] || {};
        ctx.font = "800 28px system-ui, sans-serif"; ctx.fillText(o.lose || "Game over", W / 2, 104);
        ctx.font = "500 14px system-ui, sans-serif"; ctx.fillStyle = dark ? "#cbd5e1" : "#334155"; ctx.fillText(o.why || "", W / 2, 130);
        ctx.font = "700 18px system-ui, sans-serif"; ctx.fillStyle = dark ? "#f8fafc" : "#0a1832";
        ctx.fillText(`You kept $${s.kept.toLocaleString()}${s.kept >= best() && s.kept > 0 ? "  · new best!" : ""}`, W / 2, 162);
        ctx.font = "600 14px system-ui, sans-serif"; ctx.fillStyle = "#2563eb"; ctx.fillText("Press Space or tap to run again", W / 2, 192);
      }
    }
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    update(dt); render();
    const share = overlay && overlay.querySelector("#kitty-share");
    if (share) {
      share.hidden = state.phase !== "over";
      share.href = `https://x.com/intent/tweet?text=${encodeURIComponent(`I kept $${state.kept} dodging sandwich bots in Kitty Run on MEV Finder`)}&url=${encodeURIComponent("https://mevfinder.com")}`;
    }
    raf = requestAnimationFrame(frame);
  }

  function onKey(ev) {
    if (!overlay) return;
    if (ev.key === "Escape") { close(); return; }
    if (ev.key === "p" || ev.key === "P") { if (state.phase === "run") state.phase = "paused"; return; }
    if ([" ", "ArrowUp", "w", "W"].includes(ev.key)) { ev.preventDefault(); if (ev.type === "keydown" && !ev.repeat) jumpPress(); if (ev.type === "keyup") jumpRelease(); }
  }
  const onHidden = () => { if (document.hidden && state && state.phase === "run") state.phase = "paused"; };

  function soundLabel() { return sound.on ? "Sound on" : "Sound off"; }

  function open() {
    if (overlay) return;
    W = window.innerWidth < 640 ? 520 : 860;
    overlay = document.createElement("div");
    overlay.className = "kitty-overlay";
    overlay.innerHTML = `<div class="kitty-modal" role="dialog" aria-modal="true" aria-label="Kitty Run game">
      <div class="kitty-head"><b>Kitty Run</b><span class="muted">Space / tap to jump · hold for higher · P to pause</span>
        <button type="button" class="kitty-btn" id="kitty-sound" aria-pressed="${sound.on}">${soundLabel()}</button>
        <a class="kitty-btn" id="kitty-share" target="_blank" rel="noopener" hidden>Share score</a>
        <button type="button" class="kitty-btn" id="kitty-close" aria-label="Close the game">Close</button></div>
      <canvas id="kitty-canvas" width="${W}" height="${H}"></canvas></div>`;
    document.body.appendChild(overlay);
    canvas = overlay.querySelector("#kitty-canvas");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = W * dpr; canvas.height = H * dpr; canvas.style.aspectRatio = `${W} / ${H}`;
    ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
    state = fresh();
    overlay.querySelector("#kitty-close").addEventListener("click", close);
    overlay.querySelector("#kitty-sound").addEventListener("click", (ev) => {
      sound.set(!sound.on); ev.currentTarget.textContent = soundLabel(); ev.currentTarget.setAttribute("aria-pressed", String(sound.on));
      if (sound.on) sound.chaChing();
    });
    overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
    canvas.addEventListener("pointerdown", (ev) => { ev.preventDefault(); jumpPress(); });
    canvas.addEventListener("pointerup", jumpRelease);
    canvas.addEventListener("pointerleave", jumpRelease);
    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", onKey);
    document.addEventListener("visibilitychange", onHidden);
    last = performance.now(); raf = requestAnimationFrame(frame);
    overlay.querySelector("#kitty-close").focus();
  }

  function close() {
    if (!overlay) return;
    cancelAnimationFrame(raf); raf = 0;
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("keyup", onKey);
    document.removeEventListener("visibilitychange", onHidden);
    overlay.remove(); overlay = null; state = null;
    const cat = document.querySelector(".cat-btn"); if (cat) cat.focus();
  }

  window.ArbGame = { open, close };
})();
