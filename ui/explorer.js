/* Etherscan-style explorer over the classifier: per chain, scans only what you ask for, shows the proof. */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const state = { chains: [], chain: null, page: "home", block: null, blockData: null, txData: null, evidence: null, jobTimer: null, blockTab: "mev", txTab: "transfers", public: false };
  const isLabelled = (r) => r.kind ? r.kind !== "other" : (r.mev_type && r.mev_type !== "none");
  const isCycle = (r) => r.kind ? r.kind === "arbitrage" : r.assessment === "trace_supported";
  const HINT = () => state.public ? "Search a block, a transaction hash or a UTC time (2026-10-07 12:34) among the indexed blocks."
    : "A block that was never scanned is downloaded and classified on request (up to 60 blocks per request). Nothing runs in the background.";
  const TYPE_LABEL = { atomic_backrun: "backrun", atomic_top_of_block: "top of block", atomic_mid_block: "mid block", atomic_top_of_flashblock: "top of flashblock",
                       embedded_cycle: "embedded cycle", sandwich_back: "sandwich (back)", sandwich_front: "sandwich (front)", sandwich_victim: "sandwich victim",
                       loss_cycle: "losing cycle", candidate_cycle: "candidate (unverified)", liquidation: "liquidation", cex_dex_candidate: "CEX-DEX candidate",
                       launch_snipe: "launch snipe", jit_liquidity: "JIT liquidity", none: "—" };
  const POSITION_LABEL = { backrun: "backrun (same block)", backrun_next_block: "backrun (earlier block)", top_of_block: "top of block", mid_block: "mid block", top_of_flashblock: "top of flashblock" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = (h, n = 8) => h ? h.slice(0, n + 2) + "…" + h.slice(-4) : "";
  const fmtUsd = (v) => v == null ? "—" : (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const fmtNum = (v, d = 6) => v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
  const money = (v) => v == null ? "—" : Math.abs(v) < 0.005 ? "$0.00" : (v < 0 ? "−$" : "$") + fmtUsd(Math.abs(v));
  const native = (wei, d = 6) => wei == null ? "—" : (Number(wei) / 1e18).toFixed(d);
  const ts = (t) => t ? new Date(t * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "";
  const age = (t) => { if (!t) return ""; const s = Math.max(0, Math.floor(Date.now() / 1000 - t)); if (s < 90) return `${s}s ago`; if (s < 5400) return `${Math.round(s / 60)} min ago`; if (s < 172800) return `${Math.round(s / 3600)} h ago`; return `${Math.round(s / 86400)} days ago`; };
  const chip = (type) => `<span class="chip ${esc(type)}">${esc(TYPE_LABEL[type] || type)}</span>`;
  const chainOf = () => state.chains.find((x) => x.name === state.chain) || {};
  const explorer = (kind, value) => { const base = chainOf()[`explorer_${kind}`] || ""; return base ? base + value : "#"; };
  const TOKEN = (document.querySelector('meta[name="mf-token"]') || {}).content || "";
  const api = async (path) => { const r = await fetch(path, { headers: { "X-MF-Token": TOKEN } }); let body; try { body = await r.json(); } catch (e) { body = {}; } if (!r.ok && r.status !== 202) { const err = new Error(body.error || r.statusText); err.status = r.status; throw err; } return body; };
  const kv = (pairs) => pairs.filter((p) => p && p[1] !== undefined && p[1] !== null && p[1] !== "").map(([k, v]) => `<div class="k">${esc(k)}</div><div>${v}</div>`).join("");
  const table = (headers, rows) => rows.length ? `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th${h.startsWith("#") ? ' class="num"' : ""}>${esc(h.replace(/^#/, ""))}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>` : `<p class="empty-note">Nothing here.</p>`;
  const link = (hash, kind = "tx", text) => `<a class="hash" href="#/${state.chain}/${kind}/${hash}">${text || short(hash)}</a>`;
  const addr = (a) => a ? `<a class="hash" href="${explorer("address", a)}" target="_blank" rel="noopener" title="${esc(a)}">${short(a)}</a>` : "—";

  // ---- theme ------------------------------------------------------------------
  try { const t = localStorage.getItem("arb-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
  $("#theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("arb-theme", next); } catch (e) { /* ignore */ }
  });

  // ---- result helpers ----------------------------------------------------------
  function resultOf(r) {
    const sw = r.sandwich;
    if (sw && sw.role !== "back") return { text: sw.role === "victim" ? "sandwiched" : "bundle leg", usd: null, net: null };
    if (sw && sw.role === "back") return { text: `${fmtNum(sw.profit)} ${esc(sw.profit_symbol || "")} (bundle)`, usd: sw.profit_usd, net: sw.net_usd, source: sw.price_source };
    if (r.mev_type === "cex_dex_candidate" && r.cex_dex) return { text: `edge ${r.cex_dex.edge_bps} bps on $${fmtUsd(r.cex_dex.notional_usd)}`, usd: r.cex_dex.edge_usd, net: null };
    if (r.mev_type === "liquidation" && r.liquidations && r.liquidations.length) { const l = r.liquidations[0]; return { text: `${esc(l.protocol)} · debt $${fmtUsd(l.debt_usd)} · collateral $${fmtUsd(l.collateral_usd)}`, usd: l.bonus_usd, net: null }; }
    if (r.mev_type === "launch_snipe" && r.snipe) return { text: `bought on ${short(r.snipe.pool)} created at idx ${r.snipe.created_in_tx_index}`, usd: null, net: null };
    if (r.mev_type === "jit_liquidity" && r.jit) return { text: `${esc(r.jit.role)} liquidity · ${r.jit.victims.length} victim swap(s)`, usd: null, net: null };
    if (r.gross_profit != null) return { text: `${fmtNum(r.gross_profit)} ${esc(r.profit_symbol || "")}`, usd: r.gross_usd, net: r.net_usd };
    return { text: r.gross_profit_raw ? r.gross_profit_raw + " raw" : "—", usd: r.gross_usd, net: r.net_usd };
  }
  const positionOf = (r) => {
    if (!isCycle(r)) return "";
    const key = r.trigger_type === "backrun" && r.trigger_distance_blocks ? "backrun_next_block" : r.trigger_type;
    let pos = POSITION_LABEL[key] || r.trigger_type || "";
    if (r.trigger_distance_blocks) pos += ` (−${r.trigger_distance_blocks} blk)`;
    return pos;
  };

  // ---- pages ---------------------------------------------------------------------
  function showPage(name) { state.page = name; document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name)); }

  async function renderHome() {
    showPage("home");
    const [blocks, txs, jobs] = await Promise.all([api(`/api/${state.chain}/blocks?limit=30`), api(`/api/${state.chain}/txs?limit=25`), state.public ? { jobs: [] } : api(`/api/${state.chain}/jobs`)]);
    $("#home-blocks").innerHTML = table(["Block", "When", "#Txs", "#Cycles", "#Sandw.", "#Gross USD"],
      blocks.blocks.map((b) => `<tr><td>${link(b.number, "block", String(b.number))}</td><td class="muted">${age(b.timestamp)}</td><td class="num">${b.transactions}</td><td class="num">${b.cycles}</td><td class="num">${b.sandwiches}</td><td class="num">${fmtUsd(b.gross_usd)}</td></tr>`));
    $("#home-txs").innerHTML = table(["Txn hash", "Type", "Block", "Result", "#USD"],
      txs.rows.map((r) => { const res = resultOf(r); return `<tr><td>${link(r.tx_hash)}</td><td>${chip(r.mev_type)}</td><td>${link(r.block_number, "block", String(r.block_number))}</td><td>${res.text}</td><td class="num">${fmtUsd(res.usd)}</td></tr>`; }));
    renderJobsTable(jobs.jobs);
  }
  function renderJobsTable(jobs) {
    $("#home-jobs").innerHTML = table(["Request", "Status", "Blocks", "Result", "Started"],
      jobs.map((j) => `<tr><td><a href="#/${state.chain}/job/${j.id}">${esc(j.label || j.transaction || "")}</a> <span class="muted">${esc(j.trace_mode)}</span></td><td><span class="badge ${j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "warn"}">${esc(j.status)}</span></td><td>${j.scanned_blocks.length ? `${j.scanned_blocks[0]}${j.scanned_blocks.length > 1 ? "–" + j.scanned_blocks[j.scanned_blocks.length - 1] : ""}` : esc((j.blocks || []).length ? `${j.blocks[0]}–${j.blocks[j.blocks.length - 1]}` : "")}</td><td>${j.summary ? `${j.summary.cycles} cycles · ${j.summary.sandwiches} sandwiches · $${fmtUsd(j.summary.gross_usd)}` : esc(j.error || "")}</td><td class="muted">${esc(j.created_at.slice(11, 19))}</td></tr>`));
  }

  async function renderBlock(n) {
    showPage("block");
    state.block = n;
    $("#block-h").textContent = `Block ${n}`;
    $("#block-proof").href = `/api/${state.chain}/block/${n}/proof`;
    let block;
    try { block = await api(`/api/${state.chain}/block/${n}`); } catch (e) {
      state.blockData = null;
      $("#block-map").innerHTML = "";
      if (state.public) { $("#block-overview").innerHTML = kv([["Block height", String(n)], ["Status", `<span class="badge warn">not indexed yet</span>`]]); $("#block-txs").innerHTML = ""; return; }
      $("#block-overview").innerHTML = kv([["Block height", String(n)], ["Status", `<span class="badge warn">not scanned</span> this block was never downloaded and classified`],
        ["Scan it", `<button class="ghost" id="scan-this" type="button">Scan block ${n} now</button> <span class="muted">downloads the block, receipts and the candidate traces, then classifies it (seconds to a minute)</span>`]]);
      $("#block-txs").innerHTML = "";
      $("#scan-this").addEventListener("click", () => startScan({ blocks: String(n) }, { kind: "block", block: n }));
      return;
    }
    state.blockData = block;
    const rows = block.rows, first = rows[0] || {};
    const positives = rows.filter(isCycle);
    const labelled = rows.filter(isLabelled);
    const paid = positives.reduce((a, r) => a + (r.fee_usd || 0) + (r.builder_tip_usd || 0), 0);
    const fam = {}; labelled.forEach((r) => { if (r.mev_type && r.kind !== "trigger") fam[r.mev_type] = (fam[r.mev_type] || 0) + 1; });
    const gross = positives.reduce((a, r) => a + (r.gross_usd || 0), 0), net = positives.reduce((a, r) => a + (r.net_usd || 0), 0);
    const sandw = rows.filter((r) => r.sandwich && r.sandwich.role === "back");
    const c = chainOf();
    $("#block-overview").innerHTML = kv([
      ["Block height", `<strong>${n}</strong> <a class="muted" href="${explorer("block", n)}" target="_blank" rel="noopener">view on ${esc(c.explorer_name || "explorer")} ↗</a>`],
      ["Status", `<span class="badge ok">${state.public ? "indexed" : "scanned"}</span> ${rows.length} transactions classified; ${labelled.filter((r) => r.kind !== "trigger").length} carry an MEV label`],
      ["Timestamp", `${ts(block.timestamp)} <span class="muted">(${age(block.timestamp)})</span>`],
      ["Built by", (block.builder || first.block_builder_tag) ? `${esc(block.builder || first.block_builder_tag)} · fee recipient ${addr(block.producer || first.block_producer)}` : ((block.producer || first.block_producer) ? `${c.ordering_policy === "builder" ? "fee recipient" : "sequencer"} ${addr(block.producer || first.block_producer)}` : "—")],
      ["Ordering", `${esc(c.ordering_policy)} · ${c.block_time_ms} ms blocks${c.name === "base" ? " · flashblock index estimated from fee jumps" : ""}`],
      ["Atomic cycles", `<strong>${positives.length}</strong> · gross ${money(gross)} · paid to the block ${money(paid)} · searchers kept <strong>${money(net)}</strong>`],
      ["Sandwiches", sandw.length ? `${sandw.length} bundle(s) · operator result ${money(sandw.reduce((a, r) => a + (r.sandwich.profit_usd || 0), 0))} · net ${money(sandw.reduce((a, r) => a + (r.sandwich.net_usd || 0), 0))}` : "0"],
      ["Other families", Object.entries(fam).filter(([k]) => !k.startsWith("atomic") && !k.startsWith("sandwich")).map(([k, v]) => `${chip(k)} ${v}`).join(" ") || "none"],
      state.public ? null : ["Proof", `<a href="/api/${state.chain}/block/${n}/proof">block_${n}_proof.json</a> <span class="muted">header, sha256 of the cached block and trace files, every labelled row with its transfers, internal calls, route and verdict; re-verify with ./arb -c ${esc(state.chain)} scan ${n}</span>`],
    ]);
    window.ArbUI.renderMap($("#block-map"), rows, (r) => { location.hash = `#/${state.chain}/tx/${r.tx_hash}`; });
    renderBlockTxs();
  }
  function renderBlockTxs() {
    const block = state.blockData; if (!block) return;
    const rows = state.blockTab === "mev" ? block.rows.filter(isLabelled) : block.rows;
    const byHash = {}; block.rows.forEach((r) => { byHash[r.tx_hash] = r; });
    $("#block-txs").innerHTML = table(["#", "Txn hash", "Type", "From", "To", "Result", "#USD", "#Fee", "Notes"],
      rows.map((r) => {
        const res = resultOf(r);
        const notes = [];
        const pos = positionOf(r); if (pos) notes.push(esc(pos));
        if (r.trigger_tx_hash && r.trigger_type === "backrun") { const t = byHash[r.trigger_tx_hash]; notes.push(`trigger ${t ? "idx " + t.tx_index : link(r.trigger_tx_hash)}`); }
        if (r.quote_venues && r.quote_venues.length) notes.push(`<span class="chip quote">RFQ leg</span>`);
        if (r.sandwich) notes.push(`bundle ${esc(r.sandwich.bundle_id)}`);
        if (r.kind === "trigger") notes.push("trigger of a backrun");
        if (!state.public && r.assessment === "needs_review") notes.push(`<span class="badge warn">needs review</span>`);
        if (r.slot_index_est != null) notes.push(`flashblock ~${r.slot_index_est}`);
        return `<tr><td class="num">${r.tx_index}</td><td>${link(r.tx_hash)}</td><td>${isLabelled(r) && r.kind !== "trigger" ? chip(r.mev_type) : `<span class="muted">${esc(r.kind === "trigger" ? "trigger" : state.public ? "" : r.arb_class)}</span>`}</td><td>${addr(r.tx_from)}</td><td>${addr(r.tx_to)}</td><td>${isLabelled(r) && r.kind !== "trigger" ? res.text : ""}</td><td class="num">${fmtUsd(res.usd)}</td><td class="num">${r.fee_native != null ? Number(r.fee_native).toPrecision(3) : ""}</td><td class="muted">${notes.join(" · ")}</td></tr>`;
      }));
  }
  $("#block-tabs").addEventListener("click", (ev) => { const b = ev.target.closest(".tab"); if (!b) return; state.blockTab = b.dataset.tab; $("#block-tabs").querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b)); renderBlockTxs(); });
  $("#block-prev").addEventListener("click", () => { if (state.block != null) location.hash = `#/${state.chain}/block/${state.block - 1}`; });
  $("#block-next").addEventListener("click", () => { if (state.block != null) location.hash = `#/${state.chain}/block/${state.block + 1}`; });
  $("#block-rescan").addEventListener("click", () => { if (state.block != null) startScan({ blocks: String(state.block), label: `rescan ${state.block}` }, { kind: "block", block: state.block }); });

  async function renderTx(hash) {
    showPage("tx");
    let tx;
    try { tx = await api(`/api/${state.chain}/tx/${hash}`); } catch (e) {
      $("#tx-costs").innerHTML = ""; $("#tx-story-card").hidden = true;
      if (state.public) { $("#tx-overview").innerHTML = kv([["Transaction hash", `<span class="hash">${esc(hash)}</span>`], ["Status", `<span class="badge warn">not in an indexed block</span>`]]); return; }
      $("#tx-overview").innerHTML = kv([["Transaction hash", `<span class="hash">${esc(hash)}</span>`], ["Status", `<span class="badge warn">not in any scanned block</span>`],
        ["Scan it", `<button class="ghost" id="scan-tx" type="button">Find and scan its block</button> <span class="muted">looks the transaction up on the RPC, downloads its whole block and classifies it</span>`]]);
      $("#tx-proof").innerHTML = ""; $("#tx-tab-body").innerHTML = "";
      $("#scan-tx").addEventListener("click", () => startScan({ tx: hash, label: short(hash) }, { kind: "tx", hash }));
      return;
    }
    const ev = state.public ? {} : await api(`/api/${state.chain}/tx/${hash}/evidence`).catch(() => ({}));
    state.txData = tx; state.evidence = ev;
    const c = chainOf(), fee = tx.fees || {}, trace = tx.trace || {}, res = resultOf(tx);
    $("#tx-explorer").href = tx.explorer_url || "#"; $("#tx-raw").href = `/api/${state.chain}/tx/${hash}`;
    const status = ev.status == null ? "" : (ev.status === 1 ? `<span class="badge ok">success</span>` : `<span class="badge bad">reverted</span>`);
    $("#tx-overview").innerHTML = kv([
      ["Transaction hash", `<span class="hash">${esc(tx.tx_hash)}</span>`],
      ["Status", status || (tx.arb_class === "reverted" ? `<span class="badge bad">reverted</span>` : `<span class="badge ok">success</span>`)],
      ["Block", `${link(tx.block_number, "block", String(tx.block_number))} · position ${tx.tx_index}${tx.user_index_in_block != null ? ` (user tx #${tx.user_index_in_block})` : ""}${tx.slot_index_est != null ? ` · estimated flashblock ${tx.slot_index_est}` : ""}`],
      ["Timestamp", `${ts(tx.timestamp)} <span class="muted">(${age(tx.timestamp)})</span>`],
      ["From", addr(tx.tx_from)], ["To", addr(tx.tx_to)],
      ["Value", ev.value_wei != null ? `${native(ev.value_wei)} ${esc(c.native_symbol)}` : null],
      ["Transaction fee", `${esc(fee.total_fee_eth ?? tx.fee_native ?? "")} ${esc(c.native_symbol)}${tx.fee_usd != null ? ` ($${fmtUsd(tx.fee_usd)})` : ""}${fee.gas_used != null ? ` · gas ${fee.gas_used}` : ""} · priority ${esc(fee.priority_fee_per_gas_gwei ?? tx.priority_gwei ?? "—")} gwei/gas${fee.l1_fee_wei ? ` · L1 fee ${native(fee.l1_fee_wei, 8)}` : ""}`],
      ["Block producer", `${tx.block_builder_tag ? esc(tx.block_builder_tag) + " · " : ""}${addr(tx.block_producer)}${tx.builder_tip_wei != null ? ` · tip paid inside the tx ${native(tx.builder_tip_wei || 0, 8)} ${esc(c.native_symbol)}${tx.builder_tip_usd ? ` ($${fmtUsd(tx.builder_tip_usd)})` : ""}` : tx.builder_tip_usd ? ` · tip paid inside the tx $${fmtUsd(tx.builder_tip_usd)}` : ""}`],
      ["Kickbacks", Number(tx.kickback_wei || 0) ? `${native(tx.kickback_wei, 8)} ${esc(c.native_symbol)} to known order-flow recipients` : null],
      tx.dexes && tx.dexes.length && isCycle(tx) ? ["Route", `${esc(tx.dexes.map(window.ArbUI.dexName).join(", "))} · ${tx.n_legs || tx.dexes.length} swaps`] : null,
      tx.beaten_by ? ["Beaten by", `${link(tx.beaten_by.tx_hash, "tx", "#" + tx.beaten_by.tx_index)} · ${addr(tx.beaten_by.operator)} <span class="muted">took the same pool earlier in the block</span>`] : null,
      tx.beat && tx.beat.length ? ["Beat", tx.beat.map((h) => link(h)).join(", ")] : null,
      ["MEV label", `${chip(tx.mev_type)} ${positionOf(tx) ? `<span class="muted">${esc(positionOf(tx))}</span>` : ""} ${tx.quote_venues && tx.quote_venues.length ? `<span class="chip quote">RFQ leg: ${esc(tx.quote_venues.join(", "))}</span>` : ""}${tx.trigger_tx_hash ? ` · trigger ${link(tx.trigger_tx_hash)}${tx.trigger_distance_blocks ? ` (${tx.trigger_distance_blocks} block(s) earlier)` : " (same block)"}` : ""}`],
    ]);
    const cycleLegs = isCycle(tx) ? (tx.legs || []) : [];
    $("#tx-story-card").hidden = !isCycle(tx);
    $("#tx-story").innerHTML = isCycle(tx) ? window.ArbUI.orderChain(tx) + (window.ArbUI.cycleDiagram(cycleLegs, tx.gross_profit != null ? `${fmtNum(tx.gross_profit)} ${tx.profit_symbol || ""}`.trim() : "") || '<p class="empty-note">Route not available for this cycle.</p>') : "";
    $("#tx-costs-card").hidden = !tx.costs;
    $("#tx-costs").innerHTML = tx.costs ? window.ArbUI.waterfall(tx.costs, c.fee_flow || {}) : "";
    if (state.public) return;
    const payouts = Object.entries(trace.payouts_by_token || {}).flatMap(([token, by]) => Object.entries(by).map(([to, raw]) => `${raw} raw ${short(token)} → ${addr(to)}`));
    const copy = ev.explorer_copy;
    $("#tx-proof").innerHTML = kv([
      ["Assessment", `<strong>${esc(tx.assessment)}</strong> · ${esc(tx.arb_class)} <span class="muted">${tx.assessment === "trace_supported" ? "closed positive cycle in the call trace, amounts from settled receipts" : tx.assessment === "trace_supported_loss" ? "executed cycle with a negative result" : tx.assessment === "needs_review" ? "closed gain without a route the method accepts: reported, not counted" : "not an arbitrage under the method"}</span>`],
      ["Trace verdict", `${esc(trace.verdict || trace.status)}${trace.notes && trace.notes.length ? ` <span class="muted">· ${esc(trace.notes.slice(0, 6).join(", "))}</span>` : ""}`],
      ["Result", `${res.text}${res.usd != null ? ` ≈ <strong>${money(res.usd)}</strong>` : ""} <span class="muted">[${esc(res.source || tx.price_source || "")}]</span>${res.net != null ? ` · net <span class="${res.net >= -0.005 ? "ok" : "bad"}">${money(res.net)}</span> after fee, tip and kickbacks` : ""}`],
      ["Operator group", (trace.group || []).map(addr).join(", ") || "—"],
      ["Venues", (trace.venues || tx.venues || []).map((v) => `${addr(String(v).split(":")[0])}`).join(", ") || "—"],
      ["Payouts", payouts.length ? payouts.join("<br>") : null],
      ["Reasons", (tx.reasons || []).length ? esc(tx.reasons.join(", ")) : null],
      tx.sandwich_rejected ? ["Sandwich check", `bracket ${esc(tx.sandwich_rejected.bundle_id)} with front ${addr(tx.sandwich_rejected.front)} rejected: ${esc(tx.sandwich_rejected.reason)} <span class="muted">(not a round trip: a token's net change exceeds ${Math.round(0.25 * 100)} % of its volume through the operator)</span>`] : null,
      tx.sandwich ? ["Sandwich bundle", `${esc(tx.sandwich.bundle_id)} · role ${esc(tx.sandwich.role)} on ${addr(tx.sandwich.pool)} · operator ${addr(tx.sandwich.operator)} · victims ${(tx.sandwich.victims || []).map(addr).join(", ")}${tx.sandwich.role === "back" ? ` · result ${esc(tx.sandwich.profit)} ${esc(tx.sandwich.profit_symbol || "")}${Object.keys(tx.sandwich.unpriced_tokens || {}).length ? ` <span class="muted">+ unpriced: ${Object.entries(tx.sandwich.unpriced_tokens).map(([t, raw]) => `${esc(raw)} raw ${addr(t)}`).join(", ")}</span>` : ""}` : ""}`] : null,
      tx.cex_dex ? ["CEX-DEX candidate", `${esc(tx.cex_dex.position.replace(/_/g, " "))} · $${fmtUsd(tx.cex_dex.notional_usd)} in, $${fmtUsd(tx.cex_dex.received_usd)} out at Binance 1m prices · edge ${tx.cex_dex.edge_bps} bps ($${fmtUsd(tx.cex_dex.edge_usd)}) <span class="muted">${esc(tx.cex_dex.note)}</span>`] : null,
      ...(tx.liquidations || []).map((l) => ["Liquidation", `${esc(l.protocol)} · liquidator ${addr(l.liquidator)} · borrower ${addr(l.borrower)} · debt ${esc(l.debt_repaid_raw)} raw ${short(l.debt_asset)}${l.debt_usd != null ? ` ($${fmtUsd(l.debt_usd)})` : ""} · collateral ${esc(l.collateral_seized_raw)} raw ${short(l.collateral_asset)}${l.collateral_usd != null ? ` ($${fmtUsd(l.collateral_usd)})` : ""}`]),
      tx.snipe ? ["Launch snipe", `pool ${addr(tx.snipe.pool)} created at tx index ${tx.snipe.created_in_tx_index} by ${addr(tx.snipe.creator)}`] : null,
      tx.jit ? ["JIT liquidity", `${esc(tx.jit.bundle_id)} · ${esc(tx.jit.role)} on ${addr(tx.jit.pool)} · add idx ${tx.jit.add_tx_index}, remove idx ${tx.jit.remove_tx_index} · victims ${tx.jit.victims.map(addr).join(", ")}`] : null,
      ["Raw data", `block file ${ev.block_file ? `<span class="hash">${esc(ev.block_file)}</span> sha256 ${esc((ev.block_file_sha256 || "").slice(0, 16))}…` : "not cached"}${ev.trace_file ? `<br>trace file <span class="hash">${esc(ev.trace_file)}</span> sha256 ${esc((ev.trace_file_sha256 || "").slice(0, 16))}… · ${ev.trace_root ? ev.trace_root.calls + " call frames" : ""}` : "<br>no call trace cached (not a candidate; scan with traces=all to trace everything)"}`],
      ["Explorer copy", copy ? `${copy.verdict ? `<span class="badge ${copy.verdict === "exact" ? "ok" : copy.verdict === "partial" ? "warn" : "bad"}">${esc(copy.verdict)}</span>` : `<span class="badge">saved, not yet reconciled</span>`} <span class="hash">${esc(copy.file)}</span>${copy.notes && copy.notes.length ? ` <span class="muted">· ${esc(copy.notes.join("; "))}</span>` : ""}` : `<span class="muted">none saved — open the chain explorer link above and compare the token transfers and internal transactions in the tabs below</span>`],
      ["Classifier", `${esc(ev.classifier_version || "")} · docs/METHOD.md`],
    ]);
    renderTxTab();
  }
  function renderTxTab() {
    const tx = state.txData, ev = state.evidence || {}; if (!tx) return;
    const c = chainOf();
    let html = "";
    if (state.txTab === "transfers") {
      html = table(["#", "From", "To", "Amount", "Token", "Kind"], (ev.token_transfers || []).map((t) => `<tr><td class="num">${t.log_index}</td><td>${addr(t.from)}</td><td>${addr(t.to)}</td><td class="num">${t.amount != null ? esc(t.amount) : esc(t.raw) + " raw"}</td><td>${t.symbol ? esc(t.symbol) : addr(t.token)}</td><td class="muted">${esc(t.kind)}</td></tr>`));
      if (!(ev.token_transfers || []).length) html = `<p class="empty-note">${ev.block_file ? "No ERC-20 transfers in this transaction." : "Block body not cached."}</p>`;
    } else if (state.txTab === "internal") {
      html = (ev.native_calls || []).length ? table(["From", "To", `Value (${esc(c.native_symbol)})`], ev.native_calls.map((n) => `<tr><td>${addr(n.from)}</td><td>${addr(n.to)}</td><td class="num">${esc(n.value)}</td></tr>`))
        : `<p class="empty-note">${ev.trace_file ? "No internal value transfers." : "No call trace cached for this transaction."}</p>`;
    } else if (state.txTab === "route") {
      const route = tx.route && tx.route.length ? tx.route.join("\n") : "";
      html = route ? `<div class="path">${esc(route)}</div><p class="muted">Each leg: amount in → amount out at the venue (DEX family from the registry). The cycle closes in the profit token; payouts leave the group afterwards.</p>` : `<p class="empty-note">No route legs recorded (single swap, settlement, or no trace).</p>`;
    } else {
      const copy = ev.explorer_copy;
      html = copy ? `<div class="kv">${kv([["File", `<span class="hash">chains/${esc(state.chain)}/validation/explorer/${esc(copy.file)}</span>`], ["sha256", `<span class="hash">${esc(copy.sha256)}</span>`], ["Verdict", esc(copy.verdict || "not reconciled yet")], ["Checked", esc(copy.checked_at || "")], ["Checks", copy.checks ? esc(JSON.stringify(copy.checks)) : ""], ["Notes", esc((copy.notes || []).join("; "))]])}</div>`
        : `<p class="empty-note">No explorer copy saved for this transaction. Open <a href="${esc(tx.explorer_url)}" target="_blank" rel="noopener">${esc(tx.explorer_url)}</a>, compare the token transfers and internal transactions with the tabs here, and save the page under chains/${esc(state.chain)}/validation/explorer/ to let <code>./arb -c ${esc(state.chain)} explorer-check</code> reconcile it.</p>`;
    }
    $("#tx-tab-body").innerHTML = html;
  }
  $("#tx-tabs").addEventListener("click", (ev) => { const b = ev.target.closest(".tab"); if (!b) return; state.txTab = b.dataset.tab; $("#tx-tabs").querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b)); renderTxTab(); });

  // ---- scanning --------------------------------------------------------------------
  async function startScan(params, target) {
    const q = new URLSearchParams({ ...params, traces: $("#search-traces").value });
    let job;
    try { job = await api(`/api/${state.chain}/scan?${q}`); } catch (e) { alert(e.message); return; }
    sessionStorage.setItem(`job-target-${job.id}`, JSON.stringify(target || {}));
    location.hash = `#/${state.chain}/job/${job.id}`;
  }
  async function renderJob(id) {
    showPage("job");
    if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
    const target = JSON.parse(sessionStorage.getItem(`job-target-${id}`) || "{}");
    const tick = async () => {
      let job;
      try { job = await api(`/api/${state.chain}/job/${id}`); } catch (e) { $("#job-overview").innerHTML = kv([["Error", esc(e.message)]]); clearInterval(state.jobTimer); return; }
      const total = (job.blocks || []).length || 1, done = job.scanned_blocks.length || job.progress.filter((l) => /^Block \d+: \d+ transactions/.test(l)).length;
      $("#job-h").textContent = job.status === "running" ? "Scanning…" : job.status === "queued" ? "Queued" : job.status === "done" ? "Scan finished" : "Scan failed";
      $("#job-overview").innerHTML = kv([["Request", `${esc(job.label || job.transaction || "")} <span class="muted">traces: ${esc(job.trace_mode)}</span>`],
        ["Status", `<span class="badge ${job.status === "done" ? "ok" : job.status === "failed" ? "bad" : "warn"}">${esc(job.status)}</span> ${job.status === "running" ? `${Math.min(done, total)} / ${total} blocks` : ""}`],
        ["Progress", `<div class="progress"><div style="width:${Math.round(100 * Math.min(done, total) / total)}%"></div></div>`],
        job.error ? ["Error", `<span class="bad">${esc(job.error)}</span>`] : null,
        job.summary ? ["Result", `${job.summary.transactions} transactions · <strong>${job.summary.cycles}</strong> cycles · ${job.summary.losing} losing · ${job.summary.review} under review · ${job.summary.sandwiches} sandwiches · gross $${fmtUsd(job.summary.gross_usd)} · net $${fmtUsd(job.summary.net_usd)}`] : null,
        job.report ? ["Report", `<span class="hash">${esc(job.report.split("/").slice(-2).join("/"))}</span>`] : null]);
      $("#job-log").textContent = job.progress.join("\n"); $("#job-log").hidden = !job.progress.length;
      if (job.status === "done" || job.status === "failed") {
        clearInterval(state.jobTimer); state.jobTimer = null;
        const links = job.scanned_blocks.map((b) => link(b, "block", String(b))).join(" ");
        $("#job-result").innerHTML = job.scanned_blocks.length ? `<p>Open: ${links}</p>` : "";
        if (job.status === "done") {
          if (target.kind === "block" && job.scanned_blocks.includes(target.block)) location.hash = `#/${state.chain}/block/${target.block}`;
          else if (target.kind === "tx" && target.hash) location.hash = `#/${state.chain}/tx/${target.hash}`;
          else if (job.scanned_blocks.length) location.hash = `#/${state.chain}/block/${job.scanned_blocks[0]}`;
        }
      }
    };
    await tick();
    if (!state.jobTimer) state.jobTimer = setInterval(tick, 1500);
  }

  // ---- search --------------------------------------------------------------------------
  $("#search").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("#search-input").value.trim();
    if (!q) return;
    $("#search-hint").textContent = "Looking it up…";
    let found;
    try { found = await api(`/api/${state.chain}/search?q=${encodeURIComponent(q)}`); } catch (e) { $("#search-hint").textContent = e.message; return; }
    if (state.public) {
      const known = found.kind === "transaction" ? found.known : found.kind === "time" ? found.scanned : (found.scanned || []).length > 0;
      if (!known) { $("#search-hint").textContent = "Not indexed yet."; return; }
      $("#search-hint").textContent = HINT();
      location.hash = found.kind === "transaction" ? `#/${state.chain}/tx/${found.hash}` : `#/${state.chain}/block/${found.kind === "time" ? found.block : found.scanned[0]}`;
      return;
    }
    if (found.kind === "transaction") {
      $("#search-hint").textContent = found.known ? "" : "Not in any scanned block: its block will be found on the RPC and scanned.";
      if (found.known) location.hash = `#/${state.chain}/tx/${found.hash}`; else startScan({ tx: found.hash, label: short(found.hash) }, { kind: "tx", hash: found.hash });
    } else if (found.kind === "blocks") {
      const missing = found.blocks.filter((b) => !found.scanned.includes(b));
      if (!missing.length) { $("#search-hint").textContent = `${found.blocks.length} block(s) already scanned.`; location.hash = `#/${state.chain}/block/${found.blocks[0]}`; }
      else { $("#search-hint").textContent = `Scanning ${missing.length} block(s) on request…`; startScan({ blocks: missing.length === 1 ? String(missing[0]) : `${missing[0]}-${missing[missing.length - 1]}`, label: q }, { kind: "block", block: found.blocks[0] }); }
    } else if (found.kind === "time") {
      $("#search-hint").textContent = `${ts(found.time)} → block ${found.block} (sealed ${ts(found.block_timestamp)})${found.note ? "; " + found.note : ""}.`;
      if (found.scanned) location.hash = `#/${state.chain}/block/${found.block}`; else startScan({ blocks: String(found.block), label: `${q} → ${found.block}` }, { kind: "block", block: found.block });
    }
  });

  // ---- routing ----------------------------------------------------------------------------
  // The chain is the first segment of the URL hash (#/<chain>/...); the last pick is remembered, else the CLI default chain.
  const CHAIN_KEY = "arb-chain";
  const knownChain = (name) => name && state.chains.some((c) => c.name === name) ? name : null;
  async function loadChains() {
    const config = await api("/api/config").catch(() => ({ public: false }));
    state.public = !!config.public;
    document.body.classList.toggle("public", state.public);
    $("#search-hint").textContent = HINT();
    const data = await api("/api/chains");
    state.chains = data.chains;
    state.defaultChain = data.default;
  }
  function renderTabs() {
    $("#chain-tabs").innerHTML = state.chains.map((c) => `<button type="button" data-chain="${c.name}" class="${c.name === state.chain ? "active" : ""}" aria-pressed="${c.name === state.chain}">${esc(c.name)}<small>${c.blocks_covered ? c.blocks_covered.toLocaleString() + " blocks" : "no scans"}</small></button>`).join("");
    $("#chain-tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { location.hash = `#/${b.dataset.chain}`; }));
  }
  function applyChain(name) {
    state.chain = name;
    document.title = `MEV Finder · Ops · ${name}`;
    document.querySelectorAll("a[data-dashboard]").forEach((a) => { a.href = `/#/${name}`; });
    try { localStorage.setItem(CHAIN_KEY, name); } catch (e) { /* ignore */ }
    renderTabs();
  }
  async function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    let saved = null; try { saved = localStorage.getItem(CHAIN_KEY); } catch (e) { /* storage unavailable */ }
    const chain = knownChain(parts[0]) || state.chain || knownChain(saved) || knownChain(state.defaultChain) || (state.chains.find((c) => c.reports > 0) || state.chains[0] || {}).name;
    if (chain !== state.chain) applyChain(chain);
    if (!knownChain(parts[0])) { history.replaceState(null, "", `#/${chain}`); parts.splice(0, parts.length, chain); }
    if (state.jobTimer && parts[1] !== "job") { clearInterval(state.jobTimer); state.jobTimer = null; }
    try {
      if (parts[1] === "block" && /^\d+$/.test(parts[2] || "")) await renderBlock(+parts[2]);
      else if (parts[1] === "tx" && /^0x[0-9a-f]{64}$/i.test(parts[2] || "")) await renderTx(parts[2].toLowerCase());
      else if (parts[1] === "job" && parts[2]) await renderJob(parts[2]);
      else await renderHome();
    } catch (e) { console.error(e); $("#search-hint").textContent = e.message; }
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  loadChains().then(route).catch((e) => { $("#main").innerHTML = `<p class="empty-note">Cannot reach the API: ${esc(e.message)}. Start it with ./arb ui</p>`; });
})();
