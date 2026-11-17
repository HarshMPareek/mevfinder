/* A small, safe Markdown renderer for the Learn articles: headings, paragraphs, lists, quotes, code, links, images,
   bold/italic/inline code, rules, and `{{tx <chain> <hash>}}` lines that become a live route diagram. HTML in the
   source is escaped, never passed through. */
(() => {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => /^(https?:\/\/|\/|#|\.\/)/i.test(u) ? u : "#";
  function inline(text) {
    let s = esc(text);
    s = s.replace(/`([^`]+)`/g, (m, c) => `<code>${c}</code>`);
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => `<img alt="${alt}" src="${esc(safeUrl(url))}">`);
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => { const u = safeUrl(url); return `<a href="${esc(u)}"${/^https?:/i.test(u) ? ' target="_blank" rel="noopener"' : ""}>${label}</a>`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    return s;
  }
  function render(markdown) {
    const lines = String(markdown || "").replace(/\r/g, "").split("\n");
    const out = [];
    let para = [], list = null, quote = [], code = null;
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
    const flushQuote = () => { if (quote.length) { out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`); quote = []; } };
    const flushAll = () => { flushPara(); flushList(); flushQuote(); };
    for (const line of lines) {
      if (code !== null) { if (/^```/.test(line)) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; } else code.push(line); continue; }
      if (/^```/.test(line)) { flushAll(); code = []; continue; }
      const embed = line.trim().match(/^\{\{\s*tx\s+([a-z0-9_-]+)\s+(0x[0-9a-fA-F]{64})\s*\}\}$/);
      if (embed) { flushAll(); out.push(`<div class="embed" data-chain="${esc(embed[1])}" data-tx="${esc(embed[2].toLowerCase())}"><p class="muted">Loading the route…</p></div>`); continue; }
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { flushAll(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
      if (/^\s*(---|\*\*\*)\s*$/.test(line)) { flushAll(); out.push("<hr>"); continue; }
      const q = line.match(/^>\s?(.*)$/);
      if (q) { flushPara(); flushList(); quote.push(q[1]); continue; }
      const ul = line.match(/^\s*[-*]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara(); flushQuote();
        const tag = ul ? "ul" : "ol";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push((ul || ol)[1]);
        continue;
      }
      if (!line.trim()) { flushAll(); continue; }
      flushList(); flushQuote();
      para.push(line.trim());
    }
    if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
    flushAll();
    return out.join("\n");
  }
  window.ArbMarkdown = { render };
})();
