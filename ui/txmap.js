/* Shared pieces of the dashboard and the explorer: the block transaction map (one cell per transaction in execution
   order, Etherscan "Transaction Actions" style) and the "where the gross went" cost bar. No external libraries. */
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const usd = (v) => v == null ? "—" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, Math.abs(v) >= 1000 ? { maximumFractionDigits: 0 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const short = (h) => h ? h.slice(0, 8) + "…" + h.slice(-4) : "";

  // Roles, in legend order. Three hues only (blue = arbitrage, orange = sandwich, aqua = other MEV); the transaction a
  // party reacted to or attacked is a hollow ring of the same hue, a losing arbitrage is hatched: identity never rests on hue alone.
  const KINDS = [
    ["arbitrage", "Arbitrage"], ["trigger", "Trigger (backrun target)"], ["losing", "Losing arbitrage"],
    ["sandwich", "Sandwich leg"], ["victim", "Sandwich victim"], ["other_mev", "Liquidation · JIT · snipe"], ["other", "Other transaction"],
  ];
  const LABEL = Object.fromEntries(KINDS);
  const POSITION = { atomic_backrun: "backrun", atomic_top_of_block: "top of block", atomic_mid_block: "mid block",
                     atomic_top_of_flashblock: "top of flashblock", embedded_cycle: "embedded in another tx" };

  const tipEl = () => document.getElementById("tooltip");
  function showTip(html, ev) { const t = tipEl(); if (!t) return; t.innerHTML = html; t.hidden = false; moveTip(ev); }
  function moveTip(ev) { const t = tipEl(); if (!t || t.hidden) return; t.style.left = Math.min(ev.clientX + 14, innerWidth - t.offsetWidth - 8) + "px"; t.style.top = Math.min(ev.clientY + 14, innerHeight - t.offsetHeight - 8) + "px"; }
  function hideTip() { const t = tipEl(); if (t) t.hidden = true; }

  function cellTip(r) {
    const lines = [`<strong>#${r.tx_index} · ${esc(LABEL[r.kind] || r.kind)}</strong>`];
    if (r.kind === "arbitrage" || r.kind === "losing") {
      if (POSITION[r.mev_type]) lines.push(esc(POSITION[r.mev_type]) + (r.trigger_tx_index != null ? ` of #${r.trigger_tx_index}` : ""));
      if (r.gross_usd != null) lines.push(`gross ${usd(r.gross_usd)} · net ${usd(r.net_usd)}`);
      if (r.builder_tip_usd) lines.push(`tip to builder ${usd(r.builder_tip_usd)}`);
    } else if (r.sandwich) {
      lines.push(`bundle ${esc(r.sandwich.bundle_id)} · ${esc(r.sandwich.role)}`);
      if (r.sandwich.role === "back" && r.sandwich.profit_usd != null) lines.push(`bundle result ${usd(r.sandwich.profit_usd)}`);
    } else if (r.kind === "other_mev") {
      lines.push(esc((r.mev_type || "").replace(/_/g, " ")));
    }
    lines.push(`<span class="muted">${short(r.tx_hash)}</span>`);
    return lines.join("<br>");
  }

  /** Render the map of `rows` (a block's transactions, any order) into `el`. `onOpen(row)` runs on click. */
  function renderMap(el, rows, onOpen) {
    const sorted = [...rows].sort((a, b) => a.tx_index - b.tx_index);
    if (!sorted.length) { el.innerHTML = ""; return; }
    const counts = {}; sorted.forEach((r) => { counts[r.kind] = (counts[r.kind] || 0) + 1; });
    const cells = sorted.map((r, i) => `<button type="button" class="cell k-${esc(r.kind)}" data-i="${i}" aria-label="#${r.tx_index} ${esc(LABEL[r.kind] || r.kind)}"></button>`).join("");
    const legend = KINDS.filter(([k]) => counts[k]).map(([k, label]) => `<span class="key"><i class="cell k-${k}"></i>${esc(label)} <b>${counts[k]}</b></span>`).join("");
    el.innerHTML = `<div class="txmap-grid" role="group" aria-label="Transactions in execution order">${cells}</div>` +
      `<div class="txmap-axis"><span>#${sorted[0].tx_index} first</span><span>execution order →</span><span>#${sorted[sorted.length - 1].tx_index} last</span></div>` +
      `<div class="txmap-legend">${legend}</div>`;
    const buttons = [...el.querySelectorAll(".txmap-grid .cell")];
    el._rows = sorted;
    const byIndex = new Map(sorted.map((r, i) => [r.tx_index, i]));
    const partners = (r) => {
      const out = [];
      if (r.trigger_tx_index != null && byIndex.has(r.trigger_tx_index)) out.push(byIndex.get(r.trigger_tx_index));
      if (r.kind === "trigger") sorted.forEach((x, i) => { if (x.trigger_tx_index === r.tx_index) out.push(i); });
      const bundle = r.sandwich && r.sandwich.bundle_id;
      if (bundle) sorted.forEach((x, i) => { if (x.sandwich && x.sandwich.bundle_id === bundle) out.push(i); });
      return out;
    };
    buttons.forEach((b) => {
      const r = sorted[+b.dataset.i];
      b.addEventListener("mouseenter", (ev) => { showTip(cellTip(r), ev); partners(r).forEach((i) => buttons[i].classList.add("linked")); b.classList.add("linked"); });
      b.addEventListener("mousemove", moveTip);
      b.addEventListener("mouseleave", () => { hideTip(); buttons.forEach((x) => x.classList.remove("linked")); });
      b.addEventListener("click", () => { if (onOpen && r.kind !== "other") onOpen(r); });
    });
  }

  // ---- cost bar --------------------------------------------------------------------------------------------------
  // Segments in a fixed order (categorical slots 1-5): what the searcher kept first, then each party paid.
  // `ff`: the chain's [fee_flow] labels (chain.toml): who actually receives each fee on that chain, measured on chain.
  function costSegments(t, ff = {}) {
    return [
      { key: "net", label: "Kept by searchers", value: t.net, color: "--kept" },
      { key: "producer", label: ff.producer_label || "Paid to block producers", value: (t.priority || 0) + (t.tip || 0), color: "--producer" },
      { key: "network", label: ff.network_label || "Network fees", value: (t.chain_fee || 0) + (t.l1_fee || 0), color: "--network" },
      { key: "orderflow", label: "Order-flow payments", value: t.kickback || 0, color: "--orderflow" },
    ];
  }

  /** A 100 % bar of `totals.gross` split by recipient, with direct labels on wide segments and a legend. */
  function renderCostBar(el, totals, ff) {
    const segs = costSegments(totals, ff).filter((s) => s.value > 0.0049);
    const whole = segs.reduce((a, s) => a + s.value, 0);
    if (!whole) { el.innerHTML = `<p class="empty-note">No priced cycles yet.</p>`; return; }
    const bar = segs.map((s, i) => { const pct = 100 * s.value / whole; return `<div class="seg" data-i="${i}" style="flex-grow:${s.value};background:var(${s.color})" aria-label="${esc(s.label)} ${pct.toFixed(1)}%">${pct >= 9 ? `<span>${pct.toFixed(0)}%</span>` : ""}</div>`; }).join("");
    const legend = segs.map((s) => `<span class="key"><i style="background:var(${s.color})"></i>${esc(s.label)} <b>${usd(s.value)}</b> <em>${(100 * s.value / whole).toFixed(1)}%</em></span>`).join("");
    el.innerHTML = `<div class="costbar">${bar}</div><div class="costbar-legend">${legend}</div>`;
    el.querySelectorAll(".seg").forEach((node) => {
      const s = segs[+node.dataset.i];
      node.addEventListener("mouseenter", (ev) => showTip(`<strong>${esc(s.label)}</strong><br>${usd(s.value)} · ${(100 * s.value / whole).toFixed(1)}% of what the cycles paid out`, ev));
      node.addEventListener("mousemove", moveTip);
      node.addEventListener("mouseleave", hideTip);
    });
  }

  /** One cycle: gross → each cost → what the searcher kept, as aligned bars on one scale (HTML string). */
  function waterfall(costs, ff = {}) {
    if (!costs) return "";
    const steps = [["Gross result", costs.gross, "--kept", true], [ff.priority_step || "Priority fee", -costs.priority, "--producer"], [ff.tip_step || "Tip to the block producer", -costs.tip, "--producer"],
      [ff.chain_fee_step || "Base fee", -costs.chain_fee, "--network"], ["L1 data fee", -costs.l1_fee, "--network"], ["Order-flow payment", -costs.kickback, "--orderflow"]]
      .filter((s) => s[3] || Math.abs(s[1]) > 0.00049);
    const scale = Math.max(costs.gross, Math.abs(costs.net), 1e-9);
    let level = 0;
    const rows = steps.map(([label, v, color, first]) => {
      const from = first ? 0 : level, to = first ? v : level + v; level = to;
      const left = 100 * Math.min(from, to) / scale, width = Math.max(0.6, 100 * Math.abs(to - from) / scale);
      return `<div class="lbl">${esc(label)}</div><div class="track"><span style="left:${Math.max(0, left)}%;width:${Math.min(100, width)}%;background:var(${color})"></span></div><div class="amt">${usd(v)}</div>`;
    });
    rows.push(`<div class="lbl total">Kept by the searcher</div><div class="track"><span style="left:0;width:${Math.max(0.6, 100 * Math.max(0, costs.net) / scale)}%;background:var(--kept)"></span></div><div class="amt ${costs.net >= 0 ? "pos" : "neg"}"><b>${usd(costs.net)}</b></div>`);
    return `<div class="waterfall">${rows.join("")}</div>`;
  }
  // ---- the arbitrage as a loop ----------------------------------------------------------------------------------
  const DEX_NAME = { uniswap_v2: "Uniswap V2", uniswap_v3: "Uniswap V3", uniswap_v4: "Uniswap V4", pancakeswap_v2: "PancakeSwap V2", pancakeswap_v3: "PancakeSwap V3",
                     pancakeswap_infinity: "PancakeSwap Infinity", sushiswap: "SushiSwap", curve: "Curve", balancer_v2: "Balancer V2", balancer_v3: "Balancer V3",
                     aerodrome: "Aerodrome", velodrome: "Velodrome", ekubo: "Ekubo", fluid: "Fluid", camelot: "Camelot", maverick_v2: "Maverick V2", singleton: "Singleton pool", pool: "Pool" };
  const dexName = (d) => DEX_NAME[d] || String(d || "pool").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const trimAmount = (a) => { const n = Number(String(a).replace(/,/g, "")); if (!isFinite(n)) return String(a); const abs = Math.abs(n);
    return n.toLocaleString(undefined, { maximumSignificantDigits: abs >= 1000 ? 7 : 5 }); };

  const num = (a) => { const n = Number(String(a ?? "").replace(/,/g, "")); return a != null && isFinite(n) ? n : null; };

  // A small seeded generator, so a hand-drawn diagram looks the same every time it is drawn.
  const seeded = (seed) => () => { seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const hashSeed = (s) => [...s].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) | 0, 7);
  /** A rounded box drawn by hand: corners and sides drift a little from the true line. */
  function roughBox(x, y, w, h, rnd, wob = 1.6) {
    const r = 10, j = () => (rnd() - .5) * 2 * wob;
    const pts = [[x + r, y + j()], [x + w - r, y + j()], [x + w + j(), y + r], [x + w + j(), y + h - r], [x + w - r, y + h + j()], [x + r, y + h + j()], [x + j(), y + h - r], [x + j(), y + r]];
    const c = [[x + w, y], [x + w, y + h], [x, y + h], [x, y]];
    let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
    for (let k = 0; k < 4; k++) {
      const a = pts[2 * k + 1], b = pts[(2 * k + 2) % 8], q = c[k];
      d += ` L${a[0].toFixed(1)} ${a[1].toFixed(1)} Q${(q[0] + j()).toFixed(1)} ${(q[1] + j()).toFixed(1)} ${b[0].toFixed(1)} ${b[1].toFixed(1)}`;
    }
    return d + " Z";
  }

  /** One loop: token boxes joined by the DEX that swapped them, the return arc labelled with what the loop gained.
   *  Drawn in a hand style (sketched boxes, bowed arrows, handwritten names); amounts stay in the plain face. */
  function oneLoop(legs, label) {
    const nodes = [{ amount: legs[0].amount_in, symbol: legs[0].symbol_in }, ...legs.map((l) => ({ amount: l.amount_out, symbol: l.symbol_out }))];
    const n = nodes.length, nodeW = 136, nodeH = 60, gap = 112, pad = 16, top = 42;
    const W = pad * 2 + n * nodeW + (n - 1) * gap, closes = nodes[0].symbol === nodes[n - 1].symbol, H = closes ? 214 : 124;
    const x = (i) => pad + i * (nodeW + gap);
    const rnd = seeded(hashSeed(nodes.map((nd) => `${nd.amount}${nd.symbol}`).join("|")));
    const jit = (a) => (rnd() - .5) * 2 * a;
    let svg = "";
    nodes.forEach((node, i) => {
      const end = i === 0 || i === n - 1;
      svg += `<g class="node${end ? " end" : ""}" data-step="${2 * i}">` +
        `<path class="paper-fill" d="${roughBox(x(i), top, nodeW, nodeH, rnd, .6)}"></path>` +
        `<path class="sketch" d="${roughBox(x(i), top, nodeW, nodeH, rnd)}"></path>` +
        `<path class="sketch" d="${roughBox(x(i) + 1.2, top - .8, nodeW, nodeH, rnd, 1.1)}" opacity=".45"></path>` +
        (node.amount != null ? `<text class="amt" x="${x(i) + nodeW / 2}" y="${top + 27}" text-anchor="middle">${esc(trimAmount(node.amount))}</text>` +
          `<text class="sym" x="${x(i) + nodeW / 2}" y="${top + 46}" text-anchor="middle">${esc(node.symbol)}</text>`
          : `<text class="amt" x="${x(i) + nodeW / 2}" y="${top + 36}" text-anchor="middle">${esc(node.symbol)}</text>`) + `</g>`;
      if (i === 0) svg += `<text class="cap" x="${x(i) + nodeW / 2}" y="${top - 12}" text-anchor="middle">start</text>`;
      else if (i === n - 1) svg += `<text class="cap" x="${x(i) + nodeW / 2}" y="${top - 12}" text-anchor="middle">end</text>`;
    });
    legs.forEach((leg, i) => {
      const x1 = x(i) + nodeW + 8, x2 = x(i + 1) - 9, y = top + nodeH / 2 + jit(1.5), bow = -8 + jit(3);
      const tip = `M${(x2 - 9).toFixed(1)} ${(y - 6 + jit(1)).toFixed(1)} L${x2.toFixed(1)} ${y.toFixed(1)} L${(x2 - 10).toFixed(1)} ${(y + 5 + jit(1)).toFixed(1)}`;
      const name = leg.count > 1 ? `${[...new Set(leg.dexes.map(dexName))].join(" + ")} ×${leg.count}` : dexName(leg.dexes ? leg.dexes[0] : leg.dex);
      svg += `<g data-step="${2 * i + 1}"><path class="hand-edge" d="M${x1.toFixed(1)} ${(y + jit(1)).toFixed(1)} Q${((x1 + x2) / 2).toFixed(1)} ${(y + bow).toFixed(1)} ${x2.toFixed(1)} ${y.toFixed(1)}"></path>` +
        `<path class="hand-edge" d="${tip}"></path>` +
        `<text class="dex" x="${(x1 + x2) / 2}" y="${y - 14}" text-anchor="middle">${esc(name)}</text>` +
        (leg.hook_fee ? `<text class="cap" x="${(x1 + x2) / 2}" y="${y + 22}" text-anchor="middle">hook fee</text>` +
          `<text class="cap" x="${(x1 + x2) / 2}" y="${y + 38}" text-anchor="middle">${esc(trimAmount(leg.hook_fee))} ${esc(leg.hook_fee_symbol || "")}</text>` : "") + `</g>`;
    });
    if (closes) {
      const a = x(n - 1) + nodeW / 2, b = x(0) + nodeW / 2, y0 = top + nodeH + 5, dip = 74;
      const tip = `M${(b - 7).toFixed(1)} ${(y0 + 17).toFixed(1)} L${b.toFixed(1)} ${(y0 + 7).toFixed(1)} L${(b + 8).toFixed(1)} ${(y0 + 16).toFixed(1)}`;
      svg += `<g data-step="${2 * n - 1}"><path class="loop" d="M${a} ${y0} C${a + jit(4)} ${y0 + dip + jit(5)} ${b + jit(4)} ${y0 + dip + jit(5)} ${b} ${y0 + 8}"></path><path class="hand-edge" style="stroke:var(--kept)" d="${tip}"></path></g>` +
        (label ? `<text class="loop-label" data-step="${2 * n}" x="${(a + b) / 2}" y="${y0 + dip + 28}" text-anchor="middle">${esc(label)}</text>` : "");
    }
    return `<div class="cycle" style="--min:${Math.round(W * 0.62)}px;--w:${W}px"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="route: ${esc(nodes.map((nd) => nd.symbol).join(" to "))}">${svg}</svg></div>`;
  }

  /** The route of one arbitrage. A route that comes back to its start token and goes round again is drawn as one
   *  loop per round, so every box holds what actually flowed into the next swap. Returns an HTML string. */
  function cycleDiagram(rawLegs, profit) {
    if (!rawLegs || !rawLegs.length) return "";
    // consecutive swaps of the same pair (one trade split across pools) are one step with their amounts summed
    const legs = [];
    rawLegs.forEach((leg) => {
      const last = legs[legs.length - 1];
      if (last && last.symbol_in === leg.symbol_in && last.symbol_out === leg.symbol_out) {
        const add = (a, b) => { const x = num(a), y = num(b); return x != null && y != null ? String(x + y) : null; };
        last.amount_in = add(last.amount_in, leg.amount_in); last.amount_out = add(last.amount_out, leg.amount_out);
        last.dexes.push(leg.dex); last.count += 1;
        if (leg.hook_fee) { last.hook_fee = add(last.hook_fee || "0", leg.hook_fee); last.hook_fee_symbol = leg.hook_fee_symbol; }
      } else legs.push({ ...leg, dexes: [leg.dex], count: 1 });
    });
    const note = "";
    const start = legs[0].symbol_in, loops = [];
    let current = [];
    legs.forEach((leg, i) => { current.push(leg); if (leg.symbol_out === start && i < legs.length - 1) { loops.push(current); current = []; } });
    if (current.length) loops.push(current);
    if (loops.length === 1) return oneLoop(legs, profit ? `+${profit}` : start) + note;
    const gain = (lp) => { const a = num(lp[0].amount_in), b = num(lp[lp.length - 1].amount_out); return a != null && b != null ? `${b - a >= 0 ? "+" : "−"}${trimAmount(Math.abs(b - a))} ${start}` : start; };
    return loops.map((lp, k) => `<div class="loop-row"><span class="loop-tag">loop ${k + 1}</span>${oneLoop(lp, gain(lp))}</div>`).join("") +
      (profit ? `<div class="cycle-total">total +${esc(profit)}</div>` : "") + note;
  }

  /** Block order as a linked list: the transaction that moved the price, then the bot's arbitrage. */
  function orderChain(tx, chain) {
    const me = `<div class="link-node arb"><b>#${tx.tx_index}</b><span>arbitrage</span></div>`;
    if (tx.trigger_type === "backrun" && (tx.trigger_tx_index != null || tx.trigger_tx_hash)) {
      const where = tx.trigger_distance_blocks ? `+${tx.trigger_distance_blocks} block(s)` : (tx.trigger_tx_index != null ? `+${tx.tx_index - tx.trigger_tx_index} tx, same block` : "same block");
      const trig = `<b>${tx.trigger_tx_index != null ? "#" + tx.trigger_tx_index : short(tx.trigger_tx_hash)}</b><span>trigger</span>`;
      return `<div class="chain">${chain && tx.trigger_tx_hash ? `<a class="link-node trigger" href="#/${esc(chain)}/tx/${esc(tx.trigger_tx_hash)}">${trig}</a>` : `<div class="link-node trigger">${trig}</div>`}` +
        `<div class="link-arrow"><span>→</span><small>${esc(where)}</small></div>${me}</div>`;
    }
    const where = tx.trigger_type === "top_of_block" ? "top of block" : tx.trigger_type === "top_of_flashblock" ? "top of flashblock" : "mid block";
    return `<div class="chain">${me}<div class="link-note">${esc(where)}</div></div>`;
  }

  const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /** Replay a route: each box and swap lights up in the order it happened, then the loop closes and its gain shows.
   *  Returns a function that stops the replay and shows everything. */
  function animateRoute(root, step = 420) {
    const loops = [...root.querySelectorAll(".cycle")];
    const timers = [];
    const finish = () => { timers.forEach(clearTimeout); loops.forEach((c) => { c.classList.remove("dim"); c.querySelectorAll(".on, .drawing").forEach((e) => e.classList.remove("on", "drawing")); }); };
    if (!loops.length || reduced()) return finish;
    let at = 0;
    loops.forEach((c) => {
      const parts = [...c.querySelectorAll("[data-step]")];
      const max = Math.max(...parts.map((p) => +p.dataset.step));
      timers.push(setTimeout(() => { c.classList.add("dim"); parts.forEach((p) => p.classList.remove("on", "drawing")); }, at));
      for (let k = 0; k <= max; k++) {
        timers.push(setTimeout(() => parts.filter((p) => +p.dataset.step === k).forEach((p) => {
          p.classList.add("on");
          p.querySelectorAll(".loop").forEach((l) => l.classList.add("drawing"));
          if (p.classList.contains("loop-label")) { const r = p.getBoundingClientRect(); window.dispatchEvent(new CustomEvent("arb:arbitrage", { detail: { x: r.left + r.width / 2, y: r.top } })); }
        }), at + step * (k + 1)));
      }
      at += step * (max + 2);
    });
    timers.push(setTimeout(finish, at + 600));
    return finish;
  }

  const CAPTION = {
    trigger: (r) => `#${r.tx_index} · trigger`,
    arbitrage: (r) => `#${r.tx_index} · arbitrage${r.trigger_tx_index != null ? ` · backruns #${r.trigger_tx_index}` : ""}${r.gross_usd != null ? ` · gross ${usd(r.gross_usd)}` : ""}${r.net_usd != null ? ` · kept ${usd(r.net_usd)}` : ""}`,
    losing: (r) => `#${r.tx_index} · losing cycle`,
    sandwich: (r) => `#${r.tx_index} · sandwich ${r.sandwich ? r.sandwich.role : ""}${r.sandwich && r.sandwich.role === "back" && r.sandwich.profit_usd != null ? ` · result ${usd(r.sandwich.profit_usd)}` : ""}`,
    victim: (r) => `#${r.tx_index} · sandwich victim`,
    other_mev: (r) => `#${r.tx_index} · ${String(r.mev_type || "").replace(/_/g, " ")}`,
  };

  /** Walk a rendered block map in execution order: quick over ordinary transactions, a pause with a caption on each
   *  MEV one. Returns a function that stops the walk. */
  function playBlock(mapEl, captionEl, { fast = 14, pause = 1300, onDone } = {}) {
    const rows = mapEl._rows || [], cells = [...mapEl.querySelectorAll(".txmap-grid .cell")];
    let i = 0, timer = null, stopped = false;
    const clear = () => cells.forEach((c) => c.classList.remove("playing", "passed"));
    const stop = () => { stopped = true; clearTimeout(timer); clear(); if (onDone) onDone(); };
    clear();
    const counts = {};
    const tick = () => {
      if (stopped) return;
      if (i > 0) { cells[i - 1].classList.remove("playing"); cells[i - 1].classList.add("passed"); }
      if (i >= rows.length) {
        const parts = [["arbitrage", "arbitrage cycle"], ["sandwich", "sandwich leg"], ["other_mev", "other MEV"]].filter(([k]) => counts[k]).map(([k, label]) => `${counts[k]} ${label}${counts[k] > 1 ? "s" : ""}`);
        captionEl.innerHTML = `Replayed ${rows.length} transactions${parts.length ? ": " + esc(parts.join(", ")) : ""}.`;
        timer = setTimeout(() => { clear(); if (onDone) onDone(); }, 1200);
        return;
      }
      const r = rows[i];
      cells[i].classList.add("playing");
      const say = CAPTION[r.kind];
      if (say) { counts[r.kind] = (counts[r.kind] || 0) + 1; captionEl.innerHTML = esc(say(r)); }
      if (r.kind === "arbitrage") { const b = cells[i].getBoundingClientRect(); window.dispatchEvent(new CustomEvent("arb:arbitrage", { detail: { x: b.left + b.width / 2, y: b.top + b.height / 2 } })); }
      else captionEl.innerHTML = `<span class="muted">#${r.tx_index}</span>`;
      i += 1;
      timer = setTimeout(tick, say ? pause : fast);
    };
    tick();
    return stop;
  }


  window.ArbUI = { renderMap, renderCostBar, costSegments, waterfall, cycleDiagram, orderChain, dexName, animateRoute, playBlock, esc, usd, short, showTip, moveTip, hideTip, KIND_LABEL: LABEL };
})();
