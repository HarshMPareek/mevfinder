/* MEV Finder: one site over the JSON API of `./arb ui` (private) or `./arb ui --public` (visitor view).
   Every page has its own address (#/<chain>, #/<chain>/txs, #/<chain>/block/<n>, #/<chain>/tx/<hash>, #/learn/<slug>).
   Numbers are shown as measured: totals with their medians, the largest cycle's share, and what the sample covers. */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const UI = window.ArbUI;
  const state = { chains: [], chain: null, public: false, overview: {}, limit: 100, window: "7d" };
  // rolling windows a visitor can pick; the API counts each from the top of an hour (the exact start is shown)
  const WINDOWS = [["24h", "24 h", "Last 24 hours"], ["3d", "3 days", "Last 3 days"], ["7d", "7 days", "Last 7 days"]];
  try { const w = localStorage.getItem("arb-window"); if (WINDOWS.some((x) => x[0] === w)) state.window = w; } catch (e) { /* storage unavailable */ }

  // ---- formatting ------------------------------------------------------------------------------------------------
  const esc = UI.esc;
  const usd = (v) => v == null ? "—" : (v < 0 ? "−$" : "$") + (Math.abs(v) >= 1e6 ? (Math.abs(v) / 1e6).toFixed(2) + "M" : Math.abs(v) >= 1e4 ? (Math.abs(v) / 1e3).toFixed(1) + "k"
    : Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v, d = 0) => v == null || !isFinite(v) ? "—" : (100 * v).toFixed(d) + "%";
  const share = (v) => v == null || !isFinite(v) ? "—" : v >= 0.1 ? pct(v) : v >= 0.001 ? pct(v, 1) : v > 0 ? pct(v, 2) : "0%";
  const plural = (n, word) => `${int(n)} ${word}${Number(n) === 1 ? "" : "s"}`;
  let stopPlayer = () => {};
  const int = (v) => v == null ? "—" : Number(v).toLocaleString();
  const amount = (v) => { const n = Number(v); return v == null || !isFinite(n) ? "—" : n.toLocaleString(undefined, { maximumSignificantDigits: 6 }); };
  const short = (h) => h ? h.slice(0, 8) + "…" + h.slice(-6) : "";
  const date = (t) => t ? new Date(t * 1000).toISOString().slice(0, 10) : "";
  const time = (t) => t ? new Date(t * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "";
  const ago = (t) => { if (!t) return ""; const s = Math.max(0, Date.now() / 1000 - t); return s < 3600 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
  const TOKEN = (document.querySelector('meta[name="mf-token"]') || {}).content || "";
  const api = async (path) => { const r = await fetch(path, { headers: { "X-MF-Token": TOKEN } }); const body = await r.json().catch(() => ({})); if (!r.ok) throw new Error(body.error || r.statusText); return body; };
  const chainOf = () => state.chains.find((c) => c.name === state.chain) || {};
  const NAMES = { bsc: "BSC", robinhood: "Robinhood Chain", optimism: "Optimism", ethereum: "Ethereum", base: "Base", arbitrum: "Arbitrum" };
  const title = (s) => NAMES[s] || String(s || "").replace(/\b\w/g, (c) => c.toUpperCase());
  const ext = (kind, value) => { const base = chainOf()[`explorer_${kind}`]; return base ? `<a href="${esc(base + value)}" target="_blank" rel="noopener">${esc(chainOf().explorer_name || "Explorer")} ↗</a>` : ""; };
  const ICON = {
    gross: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 7c0-1.4 3.6-2.5 8-2.5s8 1.1 8 2.5-3.6 2.5-8 2.5S4 8.4 4 7zm0 0v5c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V7m-16 5v5c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-5'/></svg>`,
    producer: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 20l7-7m2-6l4 4-3 3-4-4zm-3.5 2.5L13 5l2 2-3.5 3.5M15 5l2-2 2 2-2 2'/></svg>`,
    network: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M12 3c1 3 4 4.5 4 8.5A4 4 0 0 1 8 11.5C8 9 9.5 8 10 6c1 1 1.5 2 1.5 3.5C12 7.5 12 5 12 3zm-4 15h8'/></svg>`,
    kept: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M5 13c0-3.3 3-5.5 7-5.5 2.4 0 4.3.8 5.5 2l2-1v4l-1.5.8c-.6 2.4-3 4.2-6 4.2H9c-2.3 0-4-1.7-4-4.5zm2 4.5V19m8-1.5V19M9.5 7.5V5.5a2 2 0 0 1 4 0'/><circle cx='15' cy='12' r='.8'/></svg>`,
    largest: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M7 4h10v3a5 5 0 0 1-10 0V4zM7 6H4a3 3 0 0 0 3 3m10-3h3a3 3 0 0 1-3 3m-5 3v4m-3 3h6m-3-3v3'/></svg>`,
    bots: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M12 3v3m-3.5 0h7a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-7a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3zM9 20h6M5 11H3m18 0h-2'/><circle cx='9.5' cy='11' r='1'/><circle cx='14.5' cy='11' r='1'/><path d='M9.5 15h5'/></svg>`,
    latest: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx='12' cy='12' r='8.5'/><path d='M12 7v5l3.5 2'/></svg>`,
    backrun: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 12h9m-3-4l4 4-4 4m5-8h2m0 8h2M16 4l3 2-3 2'/></svg>`,
    top: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 18l1.5-9L10 13l2-7 2 7 4.5-4L20 18H4zm0 2h16'/></svg>`,
    mid: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 5h16M4 19h16M6 12h12m-3-3l3 3-3 3'/></svg>`,
    sandwich: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M4 9c0-2 3.6-4 8-4s8 2 8 4H4zm0 2h16m-16 4c0 2 3.6 4 8 4s8-2 8-4H4zm1-2.5 3 1.5 3-1.5 3 1.5 3-1.5 3 1.5'/></svg>`,
    liquidation: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M12 3s-5.5 6.2-5.5 10a5.5 5.5 0 0 0 11 0C17.5 9.2 12 3 12 3z'/><path d='M9.5 14a2.5 2.5 0 0 0 2 2.4'/></svg>`,
    jit: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M13 3L5 13h6l-1 8 9-11h-6l0-7z'/></svg>`,
    snipe: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx='12' cy='12' r='7'/><path d='M12 2v4m0 12v4M2 12h4m12 0h4'/><circle cx='12' cy='12' r='1.5'/></svg>`,
    losing: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx='12' cy='12' r='8'/><path d='M12 4l-2 4 3 3-3 4 2 5M9 12h-1m7 0h1'/></svg>`,
    size: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M3 17l4-4 4 3 5-7 5 4M3 20h18'/></svg>`,
    day: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x='4' y='5' width='16' height='15' rx='2'/><path d='M4 10h16M8 3v4m8-4v4'/></svg>`,
    position: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x='3' y='8' width='18' height='8' rx='2'/><path d='M8 8v8m4-8v8m4-8v8'/></svg>`,
    mev: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M5 20V9a7 7 0 0 1 14 0v11l-2.3-2-2.3 2-2.4-2-2.3 2-2.4-2L5 20z'/><circle cx='9.5' cy='10' r='1'/><circle cx='14.5' cy='10' r='1'/></svg>`,
    route: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx='5' cy='12' r='2'/><circle cx='19' cy='12' r='2'/><path d='M7 12h10m-3-3l3 3-3 3'/></svg>`,
    block: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zm0 9l8-4.5M12 12v9m0-9L4 7.5'/></svg>`,
    bot: `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d='M12 3v3m-3.5 0h7a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-7a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3zM9 20h6'/><circle cx='9.5' cy='11' r='1'/><circle cx='14.5' cy='11' r='1'/><path d='M9.5 15h5'/></svg>`
  };
  const ico = (k) => ICON[k] || "";
  const LINES = ["Every block is a tiny auction nobody told you about.", "The sandwich is the only meal where you are the filling.", "Bots don't sleep. They just wait for the next block.", "gm. Someone backran a swap while you read this.", "The house always gets a tip. On chain the house is the block producer.", "Arbitrage: finding a few dollars on the floor, hundreds of times a second.", "If a price differs in two pools, a robot is already on its way.", "Flash loans: borrow a fortune, return it before anyone notices, keep the change.", "A backrun is just a very fast \"me too\".", "The mempool is a dark forest. Bring a flashlight and a tip.", "JIT liquidity: show up for one block, leave with the fees.", "On chain, the early bird is a bot and the worm is your slippage.", "Priority fee: what the bot paid to stand in front of you.", "Not financial advice. Barely advice.", "The chain remembers everything. We just draw the arrows.", "Two pools, one price gap, zero mercy.", "Searchers search. Builders build. Validators collect.", "Your swap moved the price. Someone moved it back and kept the difference.", "WAGMI, after fees.", "Top of block: the VIP lane of the blockchain.", "A losing cycle is a donation with extra steps.", "In MEV, the second-fastest bot is the first loser.", "Every arrow on this site is money that moved in under a second.", "The best trades last 200 milliseconds and are never tweeted.", "Slippage tolerance: the amount you agreed to lose in advance.", "Gas is the entry fee. The tip is the bribe. The gross is the prize.", "Liquidity is a buffet; arbitrage bots eat first.", "Blocks are built, not mined, and the builder knows your order before you do."];
  const lineOfTheDay = () => LINES[Math.floor(Date.now() / 86400000) % LINES.length];
  const link = (path, text) => `<a href="#/${state.chain}/${path}">${text}</a>`;
  const extLink = (address) => { const c = chainOf(); return address && c.explorer_address ? ` <a class="ext" href="${esc(c.explorer_address + address)}" target="_blank" rel="noopener" title="Open on ${esc(c.explorer_name || "the chain explorer")}">${esc(c.explorer_name || "Explorer")} ↗</a>` : ""; };

  // ---- what a row is ---------------------------------------------------------------------------------------------
  const TYPE = {
    atomic_backrun: ["Backrun", "arb"], atomic_top_of_block: ["Top of block", "arb"], atomic_mid_block: ["Mid block", "arb"],
    atomic_top_of_flashblock: ["Top of flashblock", "arb"], embedded_cycle: ["Embedded", "arb"], loss_cycle: ["Losing cycle", "lost"],
    sandwich_back: ["Sandwich", "sandwich"], liquidation: ["Liquidation", "other"], jit_liquidity: ["JIT liquidity", "other"], launch_snipe: ["Launch snipe", "other"],
    pool_exploit: ["Pool exploit", "other"], inventory_arbitrage: ["Inventory arbitrage", "other"],
  };
  // embedded cycles are held out of every total and list (arbkit/econ.py HELD_TYPES)
  const CYCLE_TYPES = "atomic_backrun,atomic_top_of_block,atomic_mid_block,atomic_top_of_flashblock";
  const listable = (r) => !!TYPE[r.mev_type];  // one row per sandwich (its back leg); fronts and victims live on the block page
  // Arbitrum has no flashblocks: its sub-block slots are the sequencer's fee rounds (chains/arbitrum/chain.toml)
  const typeLabel = (t) => t === "atomic_top_of_flashblock" && chainOf().family === "arbitrum" ? "Top of round" : (TYPE[t] || ["Other"])[0];
  const tag = (r) => { const [, cls] = TYPE[r.mev_type] || ["Other", "other"]; return `<span class="tag ${cls}"><i></i>${esc(typeLabel(r.mev_type))}</span>`; };
  const isCycle = (r) => r.kind ? r.kind === "arbitrage" : r.assessment === "trace_supported";
  const resultUsd = (r) => r.sandwich && r.sandwich.role === "back" ? r.sandwich.profit_usd : r.mev_type === "liquidation" && r.liquidations ? r.liquidations[0].bonus_usd
    : r.mev_type === "inventory_arbitrage" && r.inventory_arb ? r.inventory_arb.gain_usd : r.gross_usd;
  const toBlock = (r) => isCycle(r) && r.fee_usd != null ? r.fee_usd + (r.builder_tip_usd || 0) : null;
  const kept = (r) => r.sandwich && r.sandwich.role === "back" ? r.sandwich.net_usd : isCycle(r) ? r.net_usd : null;
  const signed = (v) => v == null ? `<td class="num muted">—</td>` : `<td class="num ${v >= 0 ? "" : "neg"}">${usd(v)}</td>`;

  const table = (head, rows, empty = "Nothing here yet.") => rows.length
    ? `<div class="table-card"><table class="table"><thead><tr>${head.map((h) => `<th${h.startsWith("#") ? ' class="num"' : ""}>${esc(h.replace(/^#/, ""))}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : `<p class="empty">${esc(empty)}</p>`;
  // a table whose header cells are given as HTML (sortable columns are links); see th()
  const tableH = (headCells, rows, empty = "Nothing here yet.") => rows.length
    ? `<div class="table-card"><table class="table"><thead><tr>${headCells.join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : `<p class="empty">${esc(empty)}</p>`;
  // a column header; with sorts [desc, asc] it is a link that sorts by it (again: the other direction)
  const th = (label, num, sorts, current, hrefFor) => {
    const cls = num ? ' class="num"' : "";
    if (!sorts) return `<th${cls}>${esc(label)}</th>`;
    const on = sorts.includes(current), next = current === sorts[0] ? sorts[1] : sorts[0];
    const arrow = on ? (current === sorts[0] ? " ↓" : " ↑") : "";
    return `<th${cls}><a class="sort${on ? " on" : ""}" href="${hrefFor(next)}" title="Sort by ${esc(label.toLowerCase())}">${esc(label)}${arrow}</a></th>`;
  };
  // numbered pages: first, previous, a few around the current one, next, last, and a page box for jumping
  const PAGE_SIZE = 50, MAX_ROWS = 2000000;
  const pager = (total, page, hrefFor, noun) => {
    const pages = Math.max(1, Math.min(Math.ceil(total / PAGE_SIZE), Math.floor(MAX_ROWS / PAGE_SIZE)));
    const from = total ? (page - 1) * PAGE_SIZE + 1 : 0, to = Math.min(total, page * PAGE_SIZE);
    const info = `<span class="pager-info">${total ? `${int(from)}–${int(to)} of ${int(total)} ${esc(noun)}` : `No ${esc(noun)}`}</span>`;
    if (pages <= 1) return `<nav class="pager" aria-label="Pages">${info}</nav>`;
    const nums = [...new Set([1, page - 2, page - 1, page, page + 1, page + 2, pages])].filter((p) => p >= 1 && p <= pages).sort((a, b) => a - b);
    const cells = []; let prev = 0;
    nums.forEach((p) => { if (p - prev > 1) cells.push(`<span class="gap">…</span>`); cells.push(p === page ? `<span class="pg on" aria-current="page">${int(p)}</span>` : `<a class="pg" href="${hrefFor(p)}">${int(p)}</a>`); prev = p; });
    const step = (p, label, rel) => p >= 1 && p <= pages && p !== page ? `<a class="pg step" rel="${rel}" href="${hrefFor(p)}">${label}</a>` : `<span class="pg step off">${label}</span>`;
    return `<nav class="pager" aria-label="Pages">${step(page - 1, "‹ Prev", "prev")}${cells.join("")}${step(page + 1, "Next ›", "next")}
      <form class="pager-jump" data-pages="${pages}"><label>Page <input type="number" min="1" max="${pages}" value="${page}" aria-label="Page number"></label><button type="submit">Go</button></form>${info}</nav>`;
  };
  const pageOf = (params) => Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
  const listHref = (page, params) => { const q = new URLSearchParams(); Object.entries(params).forEach(([k, v]) => { if (v && !(k === "page" && v === 1)) q.set(k, v); }); const qs = q.toString(); return `/${state.chain}/${page}${qs ? "?" + qs : ""}`; };
  const main = (html) => {
    $("#main").innerHTML = html;
    const head = $("#hero-head");
    head.innerHTML = "";
    $("#main").querySelectorAll(":scope > .crumbs, :scope > .page-head").forEach((n) => head.appendChild(n));
    window.scrollTo(0, 0);
  };
  // What the figures really cover: "Last 7 days" only when the live data reaches back that far with nothing missing;
  // otherwise when it starts. Blocks before the follower's start (test runs, samples) are not counted as coverage.
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const utc = (iso) => { const d = new Date(iso); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`; };
  const span = (h) => (h < 48 ? `${Math.round(h)} h` : `${(h / 24).toFixed(1)} days`);
  function coverageSpan(s, range) {
    const cov = s.coverage || {};
    const blocks = cov.blocks != null ? cov.blocks : s.blocks_covered;
    const missing = cov.missing_blocks ? ` · <b>${int(cov.missing_blocks)}</b> missing` : "";
    const win = WINDOWS.find((x) => x[0] === state.window);
    if (s.window_hours && cov.from && cov.complete && win) {
      return `<span title="${esc(`From ${utc(s.window_start || cov.from)} to now`)}">${esc(win[2])} · Blocks indexed <b>${int(blocks)}</b>${missing}</span>`;
    }
    if (s.period_days && cov.from && !cov.complete) {
      const tip = `Live data starts ${utc(cov.live_since)}; every block since then is classified${cov.missing_blocks ? `, except ${int(cov.missing_blocks)}` : ""}.`;
      return `<span title="${esc(tip)}">Since ${esc(utc(cov.from))} (${esc(span(cov.hours))}) · Blocks indexed <b>${int(blocks)}</b>${missing}</span>`;
    }
    return `<span>${s.period_days ? `Last ${int(s.period_days)} days · ` : ""}Blocks indexed <b>${int(blocks)}</b>${missing}${range ? ` · ${esc(range)}` : ""}</span>`;
  }
  const loading = () => main(`<div class="card"><p class="empty">Loading…</p></div>`);
  const failed = (e) => main(`<div class="page-head"><h1>Not found</h1><p>${esc(e.message || e)}</p></div>`);

  // ---- shell: chain picker, nav, stats strip ---------------------------------------------------------------------
  const ovKey = () => `${state.chain}:${state.window}`;
  async function overviewData() {
    const key = ovKey();
    if (!state.overview[key]) state.overview[key] = await api(`/api/${state.chain}/overview?window=${state.window}`);
    return state.overview[key];
  }
  const windowPicker = () => `<span class="win" role="group" aria-label="Time window">${WINDOWS.map(([k, label, long]) =>
    `<button type="button" class="win-btn${k === state.window ? " on" : ""}" data-window="${k}" aria-pressed="${k === state.window}" title="${esc(long)}">${esc(label)}</button>`).join("")}</span>`;
  function setWindow(w) {
    if (!WINDOWS.some((x) => x[0] === w) || w === state.window) return;
    state.window = w;
    try { localStorage.setItem("arb-window", w); } catch (e) { /* storage unavailable */ }
    route();
  }
  async function renderStrip() {
    const c = chainOf();
    if (!c.name) return;
    let o; try { o = await overviewData(); } catch (e) { return; }
    const s = o.summary, t = (o.economics || {}).totals || {}, days = (o.daily || {}).days || [];
    window.ArbHeadBlock = s.last_block;  // Kitty Run's ground starts at this chain's latest block
    const range = days.length ? (days[0].day === days[days.length - 1].day ? days[0].day : `${days[0].day} to ${days[days.length - 1].day}`) : "";
    const live = s.live;
    const liveBadge = !live ? "" : live.running
      ? `<span class="live-badge" title="Every new block is classified as it arrives; following block ${esc(int(live.cursor))}"><i class="live-dot"></i>Live${live.lag_seconds != null ? ` · ${esc(live.lag_seconds < 60 ? Math.round(live.lag_seconds) + " s" : Math.round(live.lag_seconds / 60) + " min")} behind` : ""}</span>`
      : `<span class="live-badge paused"><i class="live-dot"></i>Paused</span>`;
    $("#strip-inner").innerHTML = [
      liveBadge,
      `<span>${esc(title(c.name))}</span>`,
      coverageSpan(s, range),
      `<span>Arbitrage cycles <b>${int(s.cycles)}</b></span>`,
      `<span>Gross <b>${usd(t.gross)}</b></span>`,
      `<span>Kept by searchers <b>${usd(t.net)}</b></span>`,
      `<span>Bots <b>${int((o.economics || {}).bots)}</b></span>`,
      `<span>Sandwiches <b>${int(s.sandwiches)}</b></span>`,
      windowPicker(),
    ].join("");
  }
  function setNav(page) {
    document.querySelectorAll("#nav a").forEach((a) => {
      a.href = ["learn", "contact"].includes(a.dataset.page) ? `/${a.dataset.page}` : `/${state.chain}${a.dataset.page === "overview" ? "" : "/" + a.dataset.page}`;
      a.classList.toggle("active", a.dataset.page === page);
    });
    $("#home-link").href = "/";
  }

  // ---- how the numbers are checked (live store only) -------------------------------------------------------------
  function checksSection(live) {
    // internal evidence (audits, hand labels, review flags): the private view only, not the public site
    if (state.public || !live || !live.accuracy) return "";
    const a = live.accuracy, m = a.money_audit || {}, h = a.hand_labels, f = a.review_flags || {}, p = a.pricing || {}, tr = a.tracing || {};
    const tile = (label, value, sub) => `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${value}${sub ? ` <small>${esc(sub)}</small>` : ""}</div></div>`;
    const ratio = (part, whole) => whole ? `${int(part)} / ${int(whole)}` : "—";
    return `<div class="section"><h2>${ico("position")}How the numbers are checked</h2></div>
      <div class="stats">
        ${tile("Fees recomputed independently", m.fees_checked ? pct((m.fees_checked - m.fee_mismatches) / m.fees_checked, 2) : "—", m.fees_checked ? `match · ${int(m.fees_checked)} txs` : "")}
        ${tile("Cycle gross recomputed independently", m.cycles_checked ? pct((m.cycles_checked - m.gross_mismatches) / m.cycles_checked, 2) : "—", m.cycles_checked ? `match · ${int(m.cycles_checked)} cycles` : "")}
        ${m.gross_audited_usd ? tile("Dollars that disagree", usd(m.gross_mismatch_usd), `of ${usd(m.gross_audited_usd)} audited`) : ""}
        ${h ? tile("Hand-labelled transactions", ratio(h.agree, h.found), "agree") : ""}
        ${tile("Cycles flagged for review", f.counted_cycles ? share(f.flagged / f.counted_cycles) : "—", f.counted_cycles ? `of ${int(f.counted_cycles)}` : "")}
        ${tile("Cycles priced in USD", p.priced_pct != null ? share(p.priced_pct / 100) : "—", "")}
        ${tile("Candidates traced", tr.trace_ok_pct != null ? share(tr.trace_ok_pct / 100) : "—", "")}
      </div>`;
  }

  // ---- overview --------------------------------------------------------------------------------------------------
  async function pageOverview() {
    loading();
    const [o, largest, blocksData, liveData] = await Promise.all([overviewData(), api(`/api/${state.chain}/txs?type=${CYCLE_TYPES}&sort=usd&limit=5&window=${state.window}`), api(`/api/${state.chain}/blocks?limit=60`),
      state.public ? null : api(`/api/${state.chain}/live`).catch(() => null)]);  // the checks section is private
    const blocks = blocksData.blocks || [], busiest = blocks.slice().sort((a, b) => b.cycles - a.cycles || b.gross_usd - a.gross_usd)[0];
    const c = chainOf(), s = o.summary, e = o.economics || {}, t = e.totals || {}, days = (o.daily || {}).days || [];
    const ff = c.fee_flow || {};
    const producer = (t.priority || 0) + (t.tip || 0), network = (t.chain_fee || 0) + (t.l1_fee || 0), orderflow = t.kickback || 0;
    const ofGross = (v) => t.gross ? pct(v / t.gross) + " of gross" : "";
    const positions = {}; days.forEach((d) => Object.entries(d.positions || {}).forEach(([k, v]) => { positions[k] = (positions[k] || 0) + v; }));
    const posTotal = Object.values(positions).reduce((a, b) => a + b, 0);
    const POS = [["backrun", "Backrun, same block"], ["backrun_next_block", "Backrun, a later block"], ["top_of_block", "Top of block"], ["top_of_flashblock", chainOf().family === "arbitrum" ? "Top of round" : "Top of flashblock"], ["mid_block", "Mid block"]];
    const fam = {}; days.forEach((d) => Object.entries(d.families || {}).forEach(([k, v]) => { fam[k] = (fam[k] || 0) + v; }));
    const losing = days.reduce((a, d) => a + (d.loss_cycles || 0), 0);
    const lossUsd = days.reduce((a, d) => a + (d.loss_usd || 0), 0), beaten = days.reduce((a, d) => a + (d.losers_beaten || 0), 0);
    const STAT_ICON = { "Backrun, same block": "backrun", "Backrun, a later block": "backrun", "Top of block": "top", "Top of flashblock": "top", "Mid block": "mid", "Sandwiches": "sandwich", "Liquidations": "liquidation", "JIT liquidity": "jit", "Launch snipes": "snipe", "Pool exploits": "liquidation", "Inventory arbitrage": "backrun", "Losing cycles": "losing" };
    const stat = (label, value, sub = "", filter = "") => { const inner = `<div class="label">${ico(STAT_ICON[label] || "")}${esc(label)}</div><div class="value">${value}${sub ? ` <small>${esc(sub)}</small>` : ""}</div>`;
      return filter ? `<a class="stat" href="#/${state.chain}/txs?type=${filter}">${inner}</a>` : `<div class="stat">${inner}</div>`; };
    const POS_FILTER = { backrun: "backrun", backrun_next_block: "backrun", top_of_block: "top", top_of_flashblock: "top", mid_block: "mid" };
    const mini = (rows, empty) => rows.length ? `<ul class="mini">${rows.join("")}</ul>` : `<p class="empty">${esc(empty)}</p>`;

    main(`
      <div class="page-head">
        <h1>${esc(title(c.name))} MEV &amp; Arbitrage</h1>
        <p class="hand motd">${esc(lineOfTheDay())}</p>
      </div>
      <section class="card">
        <div class="figures">
          <div class="figure"><div class="label">${ico("gross")}Gross arbitrage</div><div class="value">${usd(t.gross)}</div><div class="hint">${plural(e.cycles, "cycle")}${s.cycles > e.cycles ? ` priced · ${int(s.cycles - e.cycles)} without a price` : ""}</div></div>
          <div class="figure"><div class="label">${ico("producer")}<i class="dot" style="background:var(--producer)"></i>${esc(ff.producer_label || "Paid to block producers")}</div><div class="value">${usd(producer)}</div><div class="hint">${ofGross(producer)} · ${esc(ff.producer_hint || "priority fees and tips")}</div></div>
          <div class="figure"><div class="label">${ico("network")}<i class="dot" style="background:var(--network)"></i>${esc(ff.network_label || "Network fees")}</div><div class="value">${usd(network)}</div><div class="hint">${ofGross(network)} · ${esc(ff.network_hint || (e.chain_fee_label || "chain fee").toLowerCase())}</div></div>
          ${orderflow > 0.0049 ? `<div class="figure"><div class="label">${ico("network")}<i class="dot" style="background:var(--orderflow)"></i>Order-flow payments</div><div class="value">${usd(orderflow)}</div><div class="hint">${ofGross(orderflow)} · paid back to the traders' wallets (Blink)</div></div>` : ""}
          <div class="figure"><div class="label">${ico("kept")}<i class="dot" style="background:var(--kept)"></i>Kept by searchers</div><div class="value">${usd(t.net)}</div><div class="hint">${ofGross(t.net)} · winners only</div></div>
        </div>
        <div id="costbar"></div>
      </section>

      <div class="section"><h2>${ico("losing")}Losing bots</h2>${link("txs?type=losing", "View all")}</div>
      <section class="card">
        <div class="figures">
          <div class="figure"><div class="label">${ico("losing")}Lost on executed cycles</div><div class="value">${usd(lossUsd)}</div><div class="hint">${plural(losing, "losing cycle")} · ${int(beaten)} after another bot won the same pool earlier in the block</div></div>
          ${e.failed_attempts ? `<div class="figure"><div class="label">${ico("losing")}Spent on failed attempts</div><div class="value">${usd(e.failed_attempts.usd)}</div><div class="hint">${plural(e.failed_attempts.transactions, "reverted bot transaction")} since ${esc(e.failed_attempts.since_day)} · winners kept ${usd(e.failed_attempts.kept_after_usd)} after them</div></div>` : ""}
        </div>
      </section>

      <div class="section"><h2>At a glance</h2></div>
      <div class="cards3">
        <div class="card"><div class="card-title"><h3>${ico("largest")}Largest arbitrages</h3>${link("txs?sort=usd", "View all")}</div>
          ${mini(largest.rows.map((r) => `<li class="go" data-href="#/${state.chain}/tx/${r.tx_hash}"><span><a href="#/${state.chain}/tx/${r.tx_hash}">Block ${int(r.block_number)} · #${r.tx_index}</a><span class="sub">${esc(TYPE[r.mev_type] ? typeLabel(r.mev_type) : "Arbitrage")}</span></span><span>${usd(r.gross_usd)}</span></li>`), "No cycles yet.")}</div>
        <div class="card"><div class="card-title"><h3>${ico("bots")}Top bots by gross</h3>${link("bots", "View all")}</div>
          ${mini((o.operators || []).slice(0, 5).map((b) => `<li class="go" data-href="#/${state.chain}/bot/${b.address}"><span><a class="addr" href="#/${state.chain}/bot/${b.address}">${esc(short(b.address))}</a><span class="sub">${plural(b.cycles, "cycle")} · kept ${b.gross_usd ? pct(b.net_usd / b.gross_usd) : "—"}</span></span><span>${usd(b.gross_usd)}</span></li>`), "No bots yet.")}</div>
        <div class="card"><div class="card-title"><h3>${ico("latest")}Latest arbitrage</h3>${link("txs", "View all")}</div>
          ${mini((o.recent || []).filter(isCycle).slice(0, 5).map((r) => `<li class="go" data-href="#/${state.chain}/tx/${r.tx_hash}"><span><a href="#/${state.chain}/tx/${r.tx_hash}">Block ${int(r.block_number)} · #${r.tx_index}</a><span class="sub">${esc(TYPE[r.mev_type] ? typeLabel(r.mev_type) : "Arbitrage")} · ${esc(ago(r.timestamp))}</span></span><span>${usd(r.gross_usd)}</span></li>`), "No cycles yet.")}</div>
      </div>

      <div class="section"><h2>${ico("block")}Blocks</h2></div>
      <div class="cards2">
        <div class="card"><div class="card-title"><h3>${ico("block")}Latest blocks</h3>${link("blocks", "View all")}</div>
          ${mini(blocks.slice(0, 6).map((b) => `<li class="go" data-href="#/${state.chain}/block/${b.number}"><span><a href="#/${state.chain}/block/${b.number}">Block ${int(b.number)}</a><span class="sub">${int(b.cycles)} ${b.cycles === 1 ? "cycle" : "cycles"} · ${int(b.sandwiches)} ${b.sandwiches === 1 ? "sandwich" : "sandwiches"} · ${esc(ago(b.timestamp))}</span></span><span>${b.gross_usd ? usd(b.gross_usd) : "—"}</span></li>`), "No blocks yet.")}</div>
        <div class="card"><div class="card-title"><h3>${ico("latest")}See the arbitrage in a block</h3></div>
          <form id="blockjump" class="jump"><input id="blockjump-input" type="text" inputmode="numeric" autocomplete="off" placeholder="Block number${blocks[0] ? ", e.g. " + int(blocks[0].number) : ""}"><button type="submit">Open block</button></form>
          <p class="jump-hint">Every transaction of the block in execution order: the cycles, the sandwiches, and what each bot paid and kept.</p>
          ${busiest && busiest.cycles ? `<p class="jump-hint">Busiest indexed block: <a href="#/${state.chain}/block/${busiest.number}">${int(busiest.number)}</a> with ${int(busiest.cycles)} cycles${busiest.gross_usd ? " and " + usd(busiest.gross_usd) + " gross" : ""}.</p>` : ""}
        </div>
      </div>

      <div class="section"><h2>${ico("position")}Where the arbitrage sat in the block</h2></div>
      <div class="stats">${POS.filter(([k]) => positions[k]).map(([k, label]) => stat(label, int(positions[k]), pct(positions[k] / posTotal), POS_FILTER[k])).join("") || `<p class="empty">No cycles yet.</p>`}</div>

      <div class="section"><h2>${ico("mev")}Other MEV in the same blocks</h2></div>
      ${(() => { if (state.public) return "";  // the combined total includes families not yet verified: private view only
        const obs = (k) => days.reduce((a, d) => a + ((d.observed || {})[k] || 0), 0); const all = days.reduce((a, d) => a + ((d.observed || {}).searcher_mev_usd || 0), 0);
        return all ? `<p class="jump-hint">Searcher MEV with a measured result: <b>${usd(all)}</b> (arbitrage ${usd(obs("atomic_gross_usd") + obs("embedded_gross_usd"))}, sandwiches ${usd(obs("sandwich_profit_usd"))}, JIT liquidity ${usd(fam.jit_profit_usd || 0)}, liquidation bonuses ${usd(fam.liquidation_bonus_usd || 0)}, inventory arbitrage ${usd(fam.inventory_gain_usd || 0)}). CEX-DEX candidates are estimates and stay out: ${usd(fam.cex_dex_edge_usd || 0)} of edge against Binance.</p>` : ""; })()}
      <div class="stats">${stat("Sandwiches", int(s.sandwiches), "", "sandwich")}${stat("Liquidations", int(fam.liquidation || 0), fam.liquidation_bonus_usd ? usd(fam.liquidation_bonus_usd) + " bonus" : "", "liquidation")}${stat("JIT liquidity", int(fam.jit_bundles || 0), fam.jit_profit_usd && !state.public ? usd(fam.jit_profit_usd) : "")}${stat("Launch snipes", int(fam.launch_snipe || 0))}${state.public ? "" : stat("Inventory arbitrage", int(fam.inventory_arbitrage || 0), fam.inventory_gain_usd ? usd(fam.inventory_gain_usd) : "", "inventory_arbitrage")}${stat("Pool exploits", int(fam.pool_exploit || 0), "", "pool_exploit")}</div>

      ${checksSection(liveData)}

      <div class="section"><h2>${ico("size")}By cycle size</h2></div>
      ${table(["Cycle size", "#Cycles", "#Gross", "#" + (ff.producer_label || "Paid to block producers"), "#" + (ff.network_label || "Network fees"), "#Order-flow payments", "#Kept by searchers", "#Kept"],
        (e.by_size || []).map((b) => `<tr><td class="strong">${esc(b.label)}</td><td class="num">${int(b.cycles)}</td><td class="num">${usd(b.gross)}</td><td class="num">${usd(b.priority + b.tip)}</td><td class="num">${usd(b.chain_fee + b.l1_fee)}</td><td class="num">${usd(b.kickback || 0)}</td>${signed(b.net)}<td class="num">${b.gross ? pct(b.net / b.gross) : "—"}</td></tr>`))}

      <div class="section"><h2>${ico("day")}By day</h2></div>
      ${table(["Day (UTC)", "#Blocks indexed", "#Share of the day", "#Cycles", "#Gross", "#Kept by searchers"],
        days.slice().reverse().map((d) => `<tr><td class="strong">${esc(d.day)}${d.window_from ? ` <span class="muted">from ${esc(d.window_from)}</span>` : ""}</td><td class="num">${int(d.blocks_covered)}</td><td class="num">${d.coverage != null ? pct(d.coverage, 2) : "—"}</td><td class="num">${int(d.cycles)}</td><td class="num">${usd(d.observed.total_gross_usd)}</td>${signed(d.observed.total_net_usd)}</tr>`))}
    `);
    UI.renderCostBar($("#costbar"), t, ff);
    $("#blockjump").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const input = $("#blockjump-input"), v = input.value.trim().replace(/,/g, "");
      if (/^\d+$/.test(v)) location.hash = `#/${state.chain}/block/${v}`;
      else input.setCustomValidity("Enter a block number"), input.reportValidity(), input.setCustomValidity("");
    });
    window.ArbBuddyFact = t.gross ? `On ${title(c.name)}, searchers kept ${pct(e.keep_share)} of ${usd(t.gross)} across ${plural(e.cycles, "cycle")}.` : null;
  }

  // ---- transactions ----------------------------------------------------------------------------------------------
  const FILTERS = [["all", "All", ""], ["backrun", "Backruns", "atomic_backrun"], ["top", "Top of block", "atomic_top_of_block,atomic_top_of_flashblock"], ["mid", "Mid block", "atomic_mid_block"],
                   ["sandwich", "Sandwiches", "sandwich_back"], ["liquidation", "Liquidations", "liquidation"], ["losing", "Losing cycles", "loss_cycle"]];
  function txRows(rows) {
    return rows.filter(listable).map((r) => `<tr class="go" data-href="#/${state.chain}/tx/${r.tx_hash}">
      <td class="mono"><a href="#/${state.chain}/tx/${r.tx_hash}">${esc(short(r.tx_hash))}</a></td>
      <td><a href="#/${state.chain}/block/${r.block_number}">${int(r.block_number)}</a> <span class="muted">#${r.tx_index}</span></td>
      <td>${tag(r)}</td><td class="num strong">${usd(resultUsd(r))}</td><td class="num">${usd(toBlock(r))}</td>${signed(kept(r))}
      <td class="mono muted">${r.tx_to ? `<a href="#/${state.chain}/bot/${r.tx_to}">${esc(short(r.tx_to))}</a>` : ""}</td><td class="muted">${esc(ago(r.timestamp))}</td></tr>`);
  }
  const TX_SORTS = [["latest", "Newest"], ["oldest", "Oldest"], ["usd", "Largest result"], ["usd_asc", "Smallest result"], ["net", "Most kept"], ["net_asc", "Least kept"]];
  async function pageTxs(params) {
    const filter = FILTERS.find((f) => f[0] === params.get("type")) || FILTERS[0];
    const sort = TX_SORTS.some((x) => x[0] === params.get("sort")) ? params.get("sort") : "latest", min = params.get("min") || "";
    const page = pageOf(params);
    loading();
    const q = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE), window: state.window, sort });
    if (filter[2]) q.set("type", filter[2]);
    if (min) q.set("min_usd", min);
    const data = await api(`/api/${state.chain}/txs?${q}`);
    const href = (o) => listHref("txs", { type: filter[0] === "all" ? "" : filter[0], sort: sort === "latest" ? "" : sort, min, ...o });
    const bySort = (v) => href({ sort: v === "latest" ? "" : v, page: 1 });
    const win = WINDOWS.find((x) => x[0] === state.window);
    main(`
      <div class="page-head"><h1>${esc(title(state.chain))} MEV Transactions</h1><p>Arbitrage, sandwiches and liquidations, ${esc(win[2].toLowerCase())}. A sandwich is one row: its profit is measured over the front and back legs.</p></div>
      <div class="panel"><div class="pills">${FILTERS.map((f) => `<a class="pill${f === filter ? " on" : ""}" href="${href({ type: f[0] === "all" ? "" : f[0], page: 1 })}">${esc(f[1])}</a>`).join("")}
        <span class="field">Sort <select class="select" id="sort">${TX_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
        Min $ <input id="min" type="number" min="0" step="1" value="${esc(min)}" placeholder="0"></span></div>
      ${tableH([th("Transaction"), th("Block"), th("Type"), th("Result", true, ["usd", "usd_asc"], sort, bySort), th("Paid to block", true),
                th("Kept", true, ["net", "net_asc"], sort, bySort), th("Bot"), th("Age", false, ["latest", "oldest"], sort, bySort)],
        txRows(data.rows), "Nothing matches these filters in this window.")}
      ${pager(data.total || 0, page, (p) => href({ page: p }), "transactions")}</div>`);
    $("#sort").addEventListener("change", (ev) => go(bySort(ev.target.value)));
    $("#min").addEventListener("change", (ev) => go(href({ min: ev.target.value, page: 1 })));
  }

  // ---- blocks ----------------------------------------------------------------------------------------------------
  const BLOCK_SORTS = [["latest", "Newest"], ["oldest", "Oldest"], ["gross", "Most gross"], ["gross_asc", "Least gross"],
                       ["cycles", "Most cycles"], ["cycles_asc", "Fewest cycles"]];
  async function pageBlocks(params) {
    const sort = BLOCK_SORTS.some((x) => x[0] === params.get("sort")) ? params.get("sort") : "latest", page = pageOf(params);
    loading();
    const data = await api(`/api/${state.chain}/blocks?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}&sort=${sort}&window=${state.window}`);
    const href = (o) => listHref("blocks", { sort: sort === "latest" ? "" : sort, ...o });
    const bySort = (v) => href({ sort: v === "latest" ? "" : v, page: 1 });
    const win = WINDOWS.find((x) => x[0] === state.window);
    main(`
      <div class="page-head"><h1>${esc(title(state.chain))} Blocks with MEV</h1><p>Indexed blocks, ${esc(win[2].toLowerCase())}. Open one to see every transaction in execution order.</p></div>
      <div class="panel"><div class="pills"><span class="field">Sort <select class="select" id="sort">${BLOCK_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></span></div>
      ${tableH([th("Block", false, ["latest", "oldest"], sort, bySort), th("Time (UTC)"), th("Transactions", true), th("Arbitrage cycles", true, ["cycles", "cycles_asc"], sort, bySort),
                th("Sandwiches", true), th("Gross", true, ["gross", "gross_asc"], sort, bySort)],
        data.blocks.map((b) => `<tr class="go" data-href="#/${state.chain}/block/${b.number}"><td class="strong"><a href="#/${state.chain}/block/${b.number}">${int(b.number)}</a></td><td class="muted">${esc(time(b.timestamp))}</td><td class="num">${int(b.transactions)}</td><td class="num">${int(b.cycles)}</td><td class="num">${int(b.sandwiches)}</td><td class="num">${b.gross_usd ? usd(b.gross_usd) : "—"}</td></tr>`),
        "No blocks in this window yet.")}
      ${pager(data.total || 0, page, (p) => href({ page: p }), "blocks")}</div>`);
    $("#sort").addEventListener("change", (ev) => go(bySort(ev.target.value)));
  }

  const BLOCK_TX_SORTS = [["position", "Position"], ["usd", "Largest result"], ["usd_asc", "Smallest result"], ["net", "Most kept"], ["net_asc", "Least kept"]];
  async function pageBlock(n, params) {
    const sort = BLOCK_TX_SORTS.some((x) => x[0] === params.get("sort")) ? params.get("sort") : "position";
    loading();
    const b = await api(`/api/${state.chain}/block/${n}`);
    const rows = b.rows, cycles = rows.filter(isCycle);
    const sum = (f, list) => list.reduce((a, r) => a + (f(r) || 0), 0);
    const bundles = {}; rows.forEach((r) => { if (r.sandwich) (bundles[r.sandwich.bundle_id] = bundles[r.sandwich.bundle_id] || {})[r.sandwich.role] = r; });
    const backs = rows.filter((r) => r.sandwich && r.sandwich.role === "back");
    const by = { usd: resultUsd, usd_asc: resultUsd, net: kept, net_asc: kept }[sort], asc = sort.endsWith("_asc");
    // nulls last whatever the direction, ties in execution order (like the server's list sorts)
    const ordered = rows.filter(listable).sort((a, b) => {
      if (!by) return a.tx_index - b.tx_index;
      const x = by(a), y = by(b);
      if (x == null || y == null) return x == null && y == null ? a.tx_index - b.tx_index : x == null ? 1 : -1;
      return (asc ? x - y : y - x) || a.tx_index - b.tx_index;
    });
    const bySort = (v) => `/${state.chain}/block/${n}${v === "position" ? "" : "?sort=" + v}`;
    const mevRows = ordered.map((r) => {
      let detail = "";
      if (r.trigger_tx_index != null) detail = `backruns #${r.trigger_tx_index}`;
      else if (r.sandwich) { const bd = bundles[r.sandwich.bundle_id] || {}; detail = `front #${bd.front ? bd.front.tx_index : "?"} · victim #${bd.victim ? bd.victim.tx_index : "?"}`; }
      return `<tr class="go" data-href="#/${state.chain}/tx/${r.tx_hash}"><td class="num strong">#${r.tx_index}</td><td>${tag(r)} <span class="muted">${esc(detail)}</span></td>
        <td class="mono"><a href="#/${state.chain}/tx/${r.tx_hash}">${esc(short(r.tx_hash))}</a></td><td class="num strong">${usd(resultUsd(r))}</td><td class="num">${usd(toBlock(r))}</td>${signed(kept(r))}</tr>`;
    });
    const kv = (pairs) => pairs.filter(Boolean).map(([k, v]) => `<div class="k">${esc(k)}</div><div>${v}</div>`).join("");
    main(`
      <div class="crumbs">${link("blocks", "Blocks")} / ${int(n)}</div>
      <div class="page-head" style="display:flex;justify-content:space-between;align-items:end;gap:12px;flex-wrap:wrap">
        <h1>Block ${int(n)}</h1>
        <div class="pills" style="margin:0">${link(`block/${n - 1}`, "← Previous")}<span class="muted">·</span>${link(`block/${n + 1}`, "Next →")}</div>
      </div>
      <section class="card"><div class="kv">${kv([
        ["Time", `${esc(time(b.timestamp))} <span class="muted">(${esc(ago(b.timestamp))})</span>`],
        ["Built by", b.builder ? esc(b.builder) : b.producer ? `<span class="mono">${esc(short(b.producer))}</span>` : "—"],
        ["Transactions", int(b.transactions)],
        ["Arbitrage", cycles.length ? `${int(cycles.length)} cycles · gross ${usd(sum((r) => r.gross_usd, cycles))} · paid to the block ${usd(sum(toBlock, cycles))} · kept ${usd(sum((r) => r.net_usd, cycles))}` : "None"],
        ["Sandwiches", backs.length ? `${int(backs.length)} · result ${usd(sum((r) => r.sandwich.profit_usd, backs))}` : "None"],
        ["On the chain explorer", ext("block", n)],
      ])}</div></section>
      <div class="section"><h2>${ico("block")}Transactions in execution order</h2><button class="play" id="play" type="button">Play the block</button></div>
      <section class="card"><div id="txmap"></div><div class="caption" id="caption"><span class="muted">Hover a square to see what it is; click to open it.</span></div></section>
      <div class="section"><h2>${ico("mev")}MEV in this block</h2></div>
      ${mevRows.length ? `<div class="panel"><div class="pills"><span class="field">Sort <select class="select" id="sort">${BLOCK_TX_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></span></div>` : ""}
      ${tableH([th("Position", true, ["position", "position"], sort, bySort), th("Type"), th("Transaction"), th("Result", true, ["usd", "usd_asc"], sort, bySort),
                th("Paid to block", true), th("Kept", true, ["net", "net_asc"], sort, bySort)], mevRows, "No arbitrage, sandwich or liquidation in this block.")}${mevRows.length ? "</div>" : ""}`);
    if ($("#sort")) $("#sort").addEventListener("change", (ev) => go(bySort(ev.target.value)));
    UI.renderMap($("#txmap"), rows, (r) => { location.hash = `#/${state.chain}/tx/${r.tx_hash}`; });
    window.ArbBuddyFact = `Block ${int(n)}: ${plural(cycles.length, "arbitrage cycle")}${cycles.length ? `, searchers kept ${usd(sum((r) => r.net_usd, cycles))}` : ""}. Press play!`;
    const button = $("#play");
    let playing = false;
    const idle = () => { playing = false; button.textContent = "Play the block"; button.classList.remove("stop"); };
    button.addEventListener("click", () => {
      if (playing) { stopPlayer(); idle(); return; }
      playing = true; button.textContent = "Stop"; button.classList.add("stop");
      stopPlayer = UI.playBlock($("#txmap"), $("#caption"), { onDone: idle });
    });
  }

  // ---- one transaction -------------------------------------------------------------------------------------------
  async function pageTx(hash) {
    loading();
    const tx = await api(`/api/${state.chain}/tx/${hash}`);
    const c = chainOf(), cycle = isCycle(tx), label = (TYPE[tx.mev_type] || [tx.kind === "trigger" ? "Trigger" : "Transaction"])[0];
    const heading = cycle ? `${label} arbitrage` : label;
    const profit = tx.gross_profit != null ? `${amount(tx.gross_profit)} ${tx.profit_symbol || ""}`.trim() : "";
    const fee = tx.fees || {};
    const kv = (pairs) => pairs.filter(Boolean).map(([k, v]) => `<div class="k">${esc(k)}</div><div>${v}</div>`).join("");
    const sw = tx.sandwich;
    main(`
      <div class="crumbs">${link(`block/${tx.block_number}`, `Block ${int(tx.block_number)}`)} / #${tx.tx_index}</div>
      <div class="page-head"><h1>${esc(heading)}</h1><p class="mono">${esc(tx.tx_hash)}</p></div>
      <section class="card"><div class="kv">${kv([
        ["Block and position", `${link(`block/${tx.block_number}`, int(tx.block_number))} · position #${tx.tx_index}`],
        ["Time", `${esc(time(tx.timestamp))} <span class="muted">(${esc(ago(tx.timestamp))})</span>`],
        ["Bot contract", tx.tx_to ? `<a class="mono" href="#/${state.chain}/bot/${tx.tx_to}">${esc(tx.tx_to)}</a> <span class="muted">· its other arbitrages</span>${extLink(tx.tx_to)}` : "—"],
        ["Sent by", `<span class="mono">${esc(tx.tx_from || "")}</span>${extLink(tx.tx_from)}`],
        cycle || sw ? ["Result", sw && sw.role === "back" ? `${usd(sw.profit_usd)} over the bundle` : `${esc(profit)} <span class="muted">≈ ${usd(tx.gross_usd)}</span>`] : null,
        ["Network fee", `${esc(fee.total_fee_eth ?? tx.fee_native ?? "—")} ${esc(c.native_symbol || "")}${tx.fee_usd != null ? ` <span class="muted">≈ ${usd(tx.fee_usd)}</span>` : ""}`],
        tx.builder_tip_usd ? ["Tip to the block producer", usd(tx.builder_tip_usd)] : null,
        ["Built by", tx.block_builder_tag ? esc(tx.block_builder_tag) : `<span class="mono">${esc(short(tx.block_producer))}</span>`],
        ["On the chain explorer", (c.explorer_tx || tx.explorer_url) ? `<a href="${esc(c.explorer_tx ? c.explorer_tx + tx.tx_hash : tx.explorer_url)}" target="_blank" rel="noopener">${esc(c.explorer_name || "Explorer")} ↗</a>` : "—"],
      ])}</div></section>
      ${cycle ? `<div class="section"><h2>${ico("route")}Route</h2>${(tx.legs || []).length ? `<button class="play" id="replay" type="button">Replay</button>` : ""}</div><section class="card" id="route">${UI.orderChain(tx, state.chain)}${UI.cycleDiagram(tx.legs, profit) || `<p class="empty">The route is not available for this cycle.</p>`}</section>` : ""}
      ${tx.costs ? `<div class="section"><h2>${ico("gross")}Where the gross went</h2></div><section class="card">${UI.waterfall(tx.costs, c.fee_flow || {})}</section>` : ""}
      ${!state.public && (tx.reasons || tx.trace) ? `<div class="section"><h2>Classifier detail</h2><span class="muted">private view only</span></div><section class="card"><details><summary>Show the verdict, reasons and trace notes</summary>
        <p class="mono">${esc(tx.assessment)} · ${esc(tx.arb_class)} · ${esc((tx.trace || {}).verdict || "")}</p><p class="mono">${esc((tx.reasons || []).join(", "))}</p><p class="mono">${esc(((tx.trace || {}).notes || []).join(", "))}</p>
        ${(tx.route || []).length ? `<pre class="path">${esc(tx.route.join("\n"))}</pre>` : ""}</details></section>` : ""}
      <div class="section"><h2>Raw logs</h2><span class="muted">For those who want the receipt as it is on chain</span></div>
      <section class="card"><div class="raw-actions"><button class="btn" id="raw-load" type="button">Show the raw logs</button></div><div id="raw-body"></div></section>`);
    $("#raw-load").addEventListener("click", async () => {
      const box = $("#raw-body");
      box.innerHTML = `<p class="empty">Loading…</p>`;
      let raw; try { raw = await api(`/api/${state.chain}/tx/${hash}/logs`); } catch (e) { box.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
      if (!raw.available) { box.innerHTML = `<p class="empty">The receipt for this transaction is not stored here. Open it on the chain explorer.</p>`; return; }
      $("#raw-load").hidden = true;
      box.innerHTML = `<div class="raw-actions"><span>Status <b>${raw.status === 1 ? "success" : "reverted"}</b> · gas used <b>${int(raw.gas_used)}</b> · method <span class="mono">${esc(raw.input)}</span> · ${plural(raw.logs.length, "log")}</span>
          <button class="btn" id="raw-copy" type="button">Copy as JSON</button></div>
        <div class="table-card"><table class="raw"><thead><tr><th>#</th><th>Event</th><th>Contract</th><th>Topics</th><th>Data</th></tr></thead><tbody>${raw.logs.map((lg) => `<tr>
          <td>${lg.index}</td><td class="ev">${esc(lg.event || "—")}</td><td>${esc(lg.address)}</td><td>${lg.topics.map(esc).join("<br>")}</td><td>${esc(lg.data.length > 266 ? lg.data.slice(0, 266) + "…" : lg.data)}</td></tr>`).join("")}</tbody></table></div>`;
      $("#raw-copy").addEventListener("click", async (ev) => {
        try { await navigator.clipboard.writeText(JSON.stringify(raw, null, 2)); ev.target.textContent = "Copied"; } catch (e) { ev.target.textContent = "Copy failed"; }
      });
    });
    window.ArbBuddyFact = cycle && tx.gross_usd != null ? `This bot made ${usd(tx.gross_usd)} and kept ${usd(tx.net_usd)} of it.` : `Transaction #${tx.tx_index} in block ${int(tx.block_number)}.`;
    if (cycle && $("#replay")) {
      stopPlayer = UI.animateRoute($("#route"));
      $("#replay").addEventListener("click", () => { stopPlayer(); stopPlayer = UI.animateRoute($("#route")); });
    }
  }

  // ---- bots ------------------------------------------------------------------------------------------------------
  const BOT_SORTS = [["gross", "Most gross"], ["gross_asc", "Least gross"], ["net", "Most kept"], ["net_asc", "Least kept"],
                     ["cycles", "Most cycles"], ["cycles_asc", "Fewest cycles"]];
  async function pageBots(params) {
    const sort = BOT_SORTS.some((x) => x[0] === params.get("sort")) ? params.get("sort") : "gross", page = pageOf(params);
    const offset = (page - 1) * PAGE_SIZE;
    loading();
    const data = await api(`/api/${state.chain}/operators?limit=${PAGE_SIZE}&offset=${offset}&sort=${sort}&window=${state.window}`);
    const href = (o) => listHref("bots", { sort: sort === "gross" ? "" : sort, ...o });
    const bySort = (v) => href({ sort: v === "gross" ? "" : v, page: 1 });
    const win = WINDOWS.find((x) => x[0] === state.window);
    main(`
      <div class="page-head"><h1>${esc(title(state.chain))} MEV Bots</h1><p>Contracts that ran arbitrage cycles, ${esc(win[2].toLowerCase())}. "Kept" is what was left after gas, priority fees, tips and kickbacks.</p></div>
      <div class="panel"><div class="pills"><span class="field">Sort <select class="select" id="sort">${BOT_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></span></div>
      ${tableH([th(""), th("Bot contract"), th("Cycles", true, ["cycles", "cycles_asc"], sort, bySort), th("Backruns", true), th("Sending wallets", true),
                th("Gross", true, ["gross", "gross_asc"], sort, bySort), th("Kept", true, ["net", "net_asc"], sort, bySort), th("Kept share", true)],
        data.operators.map((b, i) => `<tr><td class="muted">${int(offset + i + 1)}</td><td class="mono"><a href="#/${state.chain}/bot/${b.address}">${esc(b.address)}</a></td><td class="num">${int(b.cycles)}</td><td class="num">${int(b.backruns)}</td><td class="num">${int(b.wallets)}</td><td class="num strong">${usd(b.gross_usd)}</td>${signed(b.net_usd)}<td class="num">${b.gross_usd ? pct(b.net_usd / b.gross_usd) : "—"}</td></tr>`),
        "No bots in this window yet.")}
      ${pager(data.total != null ? data.total : data.operators.length, page, (p) => href({ page: p }), "bots")}</div>`);
    $("#sort").addEventListener("change", (ev) => go(bySort(ev.target.value)));
  }

  // ---- learn -----------------------------------------------------------------------------------------------------
  async function pageBot(address, params) {
    const page = pageOf(params), sort = TX_SORTS.some((x) => x[0] === params.get("sort")) ? params.get("sort") : "latest";
    loading();
    const b = await api(`/api/${state.chain}/bot/${address}?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}&sort=${sort}`);
    const href = (o) => listHref(`bot/${address}`, { sort: sort === "latest" ? "" : sort, ...o });
    const bySort = (v) => href({ sort: v === "latest" ? "" : v, page: 1 });
    const stat = (label, value) => `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${value}</div></div>`;
    main(`
      <div class="crumbs">${link("bots", "Bots")} / ${esc(short(address))}</div>
      <div class="page-head"><h1>Bot ${esc(short(address))}</h1><p class="mono">${esc(address)}${extLink(address)}</p></div>
      <section class="card"><div class="stats">${stat("Arbitrages", int(b.cycles))}${stat("Gross", usd(b.gross_usd))}${stat("Kept", usd(b.net_usd))}${stat("Kept share", b.gross_usd ? pct(b.net_usd / b.gross_usd) : "—")}${stat("Backruns", int(b.backruns))}${stat("Top of block", int(b.top_of_block))}${stat("Sending wallets", int(b.wallets))}${stat("First seen", b.first_block ? link(`block/${b.first_block}`, int(b.first_block)) : "—")}${stat("Last seen", b.last_block ? link(`block/${b.last_block}`, int(b.last_block)) : "—")}</div></section>
      <div class="section"><h2>${ico("bot")}Its transactions</h2><span class="muted">indexed blocks only</span></div>
      <div class="panel"><div class="pills"><span class="field">Sort <select class="select" id="sort">${TX_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></span></div>
      ${tableH([th("Transaction"), th("Block"), th("Type"), th("Result", true, ["usd", "usd_asc"], sort, bySort), th("Paid to block", true),
                th("Kept", true, ["net", "net_asc"], sort, bySort), th("Bot"), th("Age", false, ["latest", "oldest"], sort, bySort)],
        txRows(b.rows), "No indexed transactions for this address.")}
      ${pager(b.transactions || 0, page, (p) => href({ page: p }), "transactions")}</div>`);
    $("#sort").addEventListener("change", (ev) => go(bySort(ev.target.value)));
  }

  async function pageLearn() {
    loading();
    const data = await api("/api/learn");
    main(`<div class="page-head"><h1>Learn</h1><p>How MEV works, explained with real transactions from the explorer.</p></div>
      ${data.articles.length ? `<div class="articles">${data.articles.map((a) => `<a class="article-card" href="#/learn/${esc(a.slug)}"><span class="date">${esc(a.date)}</span><h3>${esc(a.title)}</h3><p>${esc(a.summary)}</p></a>`).join("")}</div>`
        : `<div class="paper"><p class="empty hand">${state.public ? "Articles are on the way." : "No articles yet. Add a Markdown file to content/learn/ (title, date and summary at the top) and it appears here."}</p></div>`}`);
  }
  async function pageArticle(slug) {
    loading();
    const a = await api(`/api/learn/${encodeURIComponent(slug)}`);
    main(`<div class="crumbs"><a href="#/learn">Learn</a> · ${esc(a.date)}</div><div class="page-head"><h1>${esc(a.title)}</h1>${a.summary ? `<p>${esc(a.summary)}</p>` : ""}</div><div class="paper"><article class="prose">${window.ArbMarkdown.render(a.markdown)}</article></div>`);
    document.querySelectorAll(".embed[data-tx]").forEach(async (el) => {
      try {
        const tx = await api(`/api/${el.dataset.chain}/tx/${el.dataset.tx}`);
        const profit = tx.gross_profit != null ? `${amount(tx.gross_profit)} ${tx.profit_symbol || ""}`.trim() : "";
        el.innerHTML = `<div class="card-head"><b>Block ${int(tx.block_number)} · #${tx.tx_index}</b><button class="play" type="button">Replay</button></div>` +
          UI.orderChain(tx, el.dataset.chain) + (UI.cycleDiagram(tx.legs, profit) || "") +
          `<a class="open" href="#/${esc(el.dataset.chain)}/tx/${esc(el.dataset.tx)}">Open this transaction →</a>`;
        let stop = UI.animateRoute(el);
        el.querySelector(".play").addEventListener("click", () => { stop(); stop = UI.animateRoute(el); });
      } catch (e) { el.innerHTML = `<p class="muted">This transaction is not in the indexed blocks.</p>`; }
    });
  }

  // ---- contact ---------------------------------------------------------------------------------------------------
  function pageContact() {
    main(`<div class="page-head"><h1>Contact</h1><p>Data and API access, research, partnerships, corrections.</p></div>
      <section class="card contact"><h2>Write to us</h2><p><a class="contact-mail" href="mailto:info@mevfinder.com">info@mevfinder.com</a></p><p class="muted">For any collaboration, write to us. For any transaction, include its hash and chain so we can read it on chain.</p></section>`);
  }

  // ---- routing ---------------------------------------------------------------------------------------------------
  async function route() {
    // real paths (/base, /base/txs) are what search engines index; #/ routes from older links still work
    const fromHash = location.hash.length > 2;
    const raw = fromHash ? location.hash.replace(/^#\/?/, "") : location.pathname.replace(/^\/+/, "") + location.search;
    const [path, query] = raw.split("?");
    const parts = path.split("/").filter(Boolean), params = new URLSearchParams(query || "");
    $("#tooltip").hidden = true;
    stopPlayer(); stopPlayer = () => {};
    window.ArbBuddyFact = null;
    try {
      $("#strip").hidden = parts[0] === "learn" || parts[0] === "contact";
      if (parts[0] === "learn") { setNav("learn"); return parts[1] ? await pageArticle(parts[1]) : await pageLearn(); }
      if (parts[0] === "contact") { setNav("contact"); document.title = "Contact MEV Finder"; return pageContact(); }
      const chain = state.chains.some((c) => c.name === parts[0]) ? parts[0] : state.chain;
      const isHome = !parts.length;
      if (!parts.length) parts.push(chain);  // the home page shows the visitor's chain without changing the URL
      else if (chain !== parts[0]) { history.replaceState(null, "", fromHash ? `#/${chain}` : `/${chain}`); parts.splice(0, parts.length, chain); }
      if (chain !== state.chain) { state.chain = chain; state.limit = 100; }
      $("#chain").value = chain;
      try { localStorage.setItem("arb-chain", chain); } catch (e) { /* storage unavailable */ }
      const PAGE_TITLES = { overview: "MEV & Arbitrage Tracker", txs: "MEV Transactions", blocks: "Blocks", bots: "MEV Bots" };
      // the home page keeps its own title (search engines read the title after the app has run)
      document.title = isHome ? "MEV Finder: Live MEV & Arbitrage Tracker on 6 Chains"
        : `${title(chain)} ${PAGE_TITLES[parts[1] || "overview"] || "MEV"} | MEV Finder`;
      renderStrip();
      const page = parts[1] || "overview";
      setNav(page === "tx" ? "txs" : page === "block" ? "blocks" : page === "bot" ? "bots" : page);
      if (page === "txs") await pageTxs(params);
      else if (page === "blocks") await pageBlocks(params);
      else if (page === "bots") await pageBots(params);
      else if (page === "bot" && /^0x[0-9a-fA-F]{40}$/.test(parts[2] || "")) await pageBot(parts[2].toLowerCase(), params);
      else if (page === "block" && /^\d+$/.test(parts[2] || "")) await pageBlock(+parts[2], params);
      else if (page === "tx" && /^0x[0-9a-fA-F]{64}$/.test(parts[2] || "")) await pageTx(parts[2].toLowerCase());
      else await pageOverview();
    } catch (e) { failed(e); }
  }

  document.addEventListener("click", (ev) => {
    const row = ev.target.closest("tr.go[data-href], li.go[data-href]");
    if (row && !ev.target.closest("a")) location.hash = row.dataset.href;
  });
  $("#search").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = $("#search-input").value.trim().replace(/,/g, "");
    if (/^0x[0-9a-fA-F]{64}$/.test(v)) location.hash = `#/${state.chain}/tx/${v.toLowerCase()}`;
    else if (/^\d+$/.test(v)) location.hash = `#/${state.chain}/block/${v}`;
    else $("#search-input").setCustomValidity("Enter a block number or a 66-character transaction hash"), $("#search-input").reportValidity(), $("#search-input").setCustomValidity("");
  });
  $("#chain").addEventListener("change", (ev) => { go(`/${ev.target.value}`); });
  // in-page navigation for the site's own paths: no reload, real URLs in the address bar and for crawlers
  function go(href) { history.pushState(null, "", href); route(); window.scrollTo(0, 0); }
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[href^='/']");
    if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || a.target) return;
    const href = a.getAttribute("href");
    if (href.startsWith("/api/") || href.startsWith("/explorer")) return;
    ev.preventDefault();
    go(href);
  });
  window.addEventListener("popstate", route);
  // Live stores grow every few seconds: refresh the strip (and the cached overview) without re-rendering the page.
  setInterval(() => { if (document.hidden || !state.chain) return; delete state.overview[ovKey()]; renderStrip(); }, 15000);
  document.addEventListener("click", (ev) => { const b = ev.target.closest("[data-window]"); if (b) setWindow(b.dataset.window); });
  document.addEventListener("submit", (ev) => {
    const f = ev.target.closest(".pager-jump"); if (!f) return;
    ev.preventDefault();
    const n = Math.min(Math.max(1, parseInt(f.querySelector("input").value, 10) || 1), +f.dataset.pages);
    const url = new URL(location.pathname + location.search, location.origin); url.searchParams.set("page", String(n));
    go(url.pathname + url.search);
  });
  window.addEventListener("hashchange", route);

  (async () => {
    try {
      const [config, data] = await Promise.all([api("/api/config").catch(() => ({ public: false })), api("/api/chains")]);
      state.public = !!config.public;
      state.chains = data.chains;
      let saved = null; try { saved = localStorage.getItem("arb-chain"); } catch (e) { /* storage unavailable */ }
      const known = (n) => state.chains.some((c) => c.name === n);
      state.chain = [saved, data.default, (state.chains.find((c) => c.cycles) || {}).name, (state.chains[0] || {}).name].find(known);
      $("#chain").innerHTML = state.chains.map((c) => `<option value="${esc(c.name)}">${esc(title(c.name))}</option>`).join("");
      $("#chain").value = state.chain;
      $("#footer").innerHTML = `<span class="muted">MEV Finder (beta): live and checked against raw receipts, still being improved</span><a href="/contact">Contact</a><a href="mailto:info@mevfinder.com">info@mevfinder.com</a>${state.public ? "" : `<a href="/explorer">Ops explorer (scans, proofs)</a>`}`;
      await route();
    } catch (e) { failed(`The API is not reachable: ${e.message}`); }
  })();
})();
