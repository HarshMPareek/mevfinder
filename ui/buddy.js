/* Appearance and companion: light (default) or dark theme, and a doodle cat, floating coins or nothing.
   Every choice is remembered per visitor. The cat catches a coin whenever a replay reaches an arbitrage and says a
   true fact about the page on hover (pages publish it as window.ArbBuddyFact). */
(() => {
  const $ = (s) => document.querySelector(s);
  const store = {
    get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
  };
  const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---- theme -----------------------------------------------------------------------------------------------------
  function setTheme(theme) {
    if (theme === "dark") document.documentElement.dataset.theme = "dark"; else delete document.documentElement.dataset.theme;
    store.set("arb-theme", theme);
    mark();
  }

  // ---- the cat ---------------------------------------------------------------------------------------------------
  // Drawn like a doodle: round strokes, a slightly uneven outline, fills from the theme.
  const CAT = `
    <svg viewBox="0 0 120 110" class="cat" aria-hidden="true">
      <path class="tail" d="M88 86 C112 84 114 60 102 54 C96 51 92 58 98 62" />
      <path class="body" d="M30 92 C24 70 30 52 48 48 C60 45 76 46 84 56 C92 66 92 82 86 93 Z" />
      <path class="head" d="M28 50 C22 36 24 22 30 14 L38 26 C46 22 56 22 62 26 L72 13 C78 22 80 36 74 50 C66 60 38 61 28 50 Z" />
      <g class="eyes"><ellipse cx="41" cy="39" rx="3.4" ry="4.2" /><ellipse cx="60" cy="39" rx="3.4" ry="4.2" /></g>
      <path class="nose" d="M48.5 46 L52.5 46 L50.5 48.6 Z" />
      <path class="whisk" d="M33 47 L21 45 M33 50 L22 52 M68 47 L80 45 M68 50 L79 52" />
      <path class="paws" d="M42 92 C42 98 50 98 50 92 M64 92 C64 98 72 98 72 92" />
    </svg>`;
  let caught = +store.get("arb-coins", "0") || 0;
  function renderCat() {
    const el = $("#buddy");
    el.innerHTML = `<div class="bubble" id="bubble" hidden></div><button class="cat-btn" type="button" aria-label="Cat companion: ${caught} coins caught">${CAT}<span class="coin-count" title="Coins caught">${caught}</span></button>`;
    const btn = el.querySelector(".cat-btn");
    const say = () => { const b = $("#bubble"); b.innerHTML = esc(window.ArbBuddyFact || "Meow. Play a block and I'll catch the coins."); b.hidden = false; };
    btn.addEventListener("mouseenter", say);
    btn.addEventListener("focus", say);
    btn.addEventListener("mouseleave", () => { $("#bubble").hidden = true; });
    btn.addEventListener("blur", () => { $("#bubble").hidden = true; });
    // easter egg: three quick taps on the cat start Kitty Run (ui/game.js); one tap just hops and talks
    let taps = [];
    btn.addEventListener("click", () => {
      const now = Date.now();
      taps = taps.filter((t) => now - t < 1500).concat(now);
      hop();
      if (taps.length >= 3 && window.ArbGame) { taps = []; $("#bubble").hidden = true; window.ArbGame.open(); return; }
      if (taps.length === 2) { const b = $("#bubble"); b.textContent = "Meow? Once more…"; b.hidden = false; return; }
      say();
    });
  }
  function hop() { const c = document.querySelector(".cat-btn"); if (!c || reduced()) return; c.classList.remove("hop"); void c.offsetWidth; c.classList.add("hop"); }

  /** A coin flies from (x, y) to the cat, which hops; the count goes up. */
  function catchCoin(x, y) {
    if (mode !== "cat") return;
    const target = document.querySelector(".cat-btn");
    caught += 1; store.set("arb-coins", String(caught));
    if (window.ArbSound) window.ArbSound.chaChing();  // a replay hands the cat a coin: cha-ching
    const count = document.querySelector(".coin-count");
    if (!target || reduced()) { if (count) count.textContent = caught; return; }
    const box = target.getBoundingClientRect();
    const coin = document.createElement("div");
    coin.className = "flying-coin";
    coin.style.left = `${x - 9}px`; coin.style.top = `${y - 9}px`;
    document.body.appendChild(coin);
    requestAnimationFrame(() => { coin.style.transform = `translate(${box.left + box.width / 2 - x}px, ${box.top + 10 - y}px) scale(.8)`; coin.style.opacity = ".9"; });
    setTimeout(() => { coin.remove(); hop(); if (count) count.textContent = caught; }, 700);
  }

  // ---- floating coins --------------------------------------------------------------------------------------------
  let raf = null;
  function startCoins() {
    const canvas = $("#coins-bg"), ctx = canvas.getContext("2d");
    canvas.hidden = false;
    const coins = Array.from({ length: 14 }, () => ({ x: Math.random(), y: Math.random(), r: 7 + Math.random() * 9, v: .00025 + Math.random() * .0004, w: Math.random() * 6.28 }));
    const draw = () => {
      const dpr = window.devicePixelRatio || 1, W = innerWidth, H = innerHeight;
      if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      const ink = getComputedStyle(document.documentElement).getPropertyValue("--coin").trim() || "#f59e0b";
      coins.forEach((c) => {
        if (!reduced()) { c.y -= c.v; c.w += .02; if (c.y < -.05) { c.y = 1.05; c.x = Math.random(); } }
        const x = c.x * W + Math.sin(c.w) * 8, y = c.y * H, squash = Math.abs(Math.cos(c.w * .7)) * .6 + .4;
        ctx.save(); ctx.translate(x, y); ctx.scale(squash, 1);
        ctx.lineWidth = 2; ctx.strokeStyle = ink; ctx.globalAlpha = .45;
        ctx.beginPath(); ctx.arc(0, 0, c.r, 0, 6.283); ctx.stroke();
        ctx.beginPath(); ctx.arc(0, 0, c.r * .62, .3, 5.9); ctx.stroke();
        ctx.restore();
      });
      raf = document.hidden ? null : requestAnimationFrame(draw);
    };
    document.addEventListener("visibilitychange", () => { if (!document.hidden && mode === "coins" && !raf) draw(); });
    draw();
  }
  function stopCoins() { if (raf) cancelAnimationFrame(raf); raf = null; const c = $("#coins-bg"); c.hidden = true; }

  // ---- companion choice ------------------------------------------------------------------------------------------
  let mode = store.get("arb-buddy", "cat");
  function setBuddy(next) {
    mode = next; store.set("arb-buddy", next);
    const el = $("#buddy");
    stopCoins();
    if (next === "cat") { el.hidden = false; renderCat(); } else { el.hidden = true; el.innerHTML = ""; }
    if (next === "coins") startCoins();
    mark();
  }

  // ---- the Appearance menu ---------------------------------------------------------------------------------------
  function mark() {
    const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.themeChoice === theme)));
    document.querySelectorAll("[data-buddy-choice]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.buddyChoice === mode)));
    const note = $("#coin-note"); if (note) note.textContent = caught ? `The cat has caught ${caught} coin${caught === 1 ? "" : "s"}.` : "";
  }
  const btn = $("#appearance-btn"), menu = $("#appearance");
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", (ev) => { ev.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute("aria-expanded", String(!menu.hidden)); mark(); });
  menu.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const t = ev.target.closest("[data-theme-choice]"), b = ev.target.closest("[data-buddy-choice]");
    if (t) setTheme(t.dataset.themeChoice);
    if (b) setBuddy(b.dataset.buddyChoice);
  });
  document.addEventListener("click", close);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(); });

  window.addEventListener("arb:arbitrage", (ev) => { const d = ev.detail || {}; catchCoin(d.x ?? innerWidth / 2, d.y ?? innerHeight / 2); });
  setBuddy(mode);
})();
