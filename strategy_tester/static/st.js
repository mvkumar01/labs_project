/* Strategy Tester frontend.
   Mounts into <div id="st-root" data-base="<api prefix>" data-css="<stylesheet url>"> inside a
   shadow root, so the host page's CSS and ours never interfere. The theme follows the host
   page's <html data-theme> attribute. */
(() => {
  "use strict";
  const host = document.getElementById("st-root");
  if (!host) return;
  const BASE = (host.dataset.base || "").replace(/\/$/, "");
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <link rel="stylesheet" href="${host.dataset.css || "st.css"}">
    <div class="st" style="visibility:hidden">
      <div class="bar">
        <div class="statusline" id="statusLine"><span class="dot"></span><span>Connecting…</span></div>
        <button class="new-btn" id="newBtn" type="button"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>New test</button>
      </div>
      <form class="composer" id="composer" autocomplete="off">
        <textarea id="prompt" rows="1" placeholder="e.g. Backtest a 50/200 SMA crossover on gold since 2015 with a 2% stop loss" aria-label="Describe a backtest or ask about an instrument"></textarea>
        <button class="send" id="sendBtn" type="submit" aria-label="Run"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
      </form>
      <div class="home">
        <section class="examples" id="examples"></section>
        <section class="chip-section"><div class="section-title">Strategy library</div><div class="chip-row" id="library"></div></section>
        <section class="chip-section" id="recentWrap" hidden><div class="section-title">Recent</div><div class="chip-row" id="history"></div></section>
      </div>
      <section id="results" class="results" aria-live="polite"></section>
      <footer class="disclaimer">Results are hypothetical and based on historical Yahoo Finance data. They include estimated costs but not taxes, financing or survivorship effects. Nothing here is financial advice.</footer>
    </div>`;
  const $ = (s) => shadow.querySelector(s);
  const stEl = $(".st");
  shadow.querySelector("link").addEventListener("load", () => { stEl.style.visibility = ""; });
  setTimeout(() => { stEl.style.visibility = ""; }, 1500);
  const LWC = window.LightweightCharts;

  // ------------------------------------------------------------------ state
  const state = { charts: [], rerender: null, compare: null, busy: false };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
  };

  // ------------------------------------------------------------------ theme (follows the host page)
  window.labsThemeLive = true; // tell the Labs theme toggle not to reload the page; we re-colour in place
  const pageTheme = () => document.documentElement.getAttribute("data-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  function syncTheme() {
    const t = pageTheme();
    if (host.getAttribute("data-theme") === t) return;
    host.setAttribute("data-theme", t);
    if (state.rerender) state.rerender();
  }
  host.setAttribute("data-theme", pageTheme());
  new MutationObserver(syncTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const css = (name) => getComputedStyle(stEl).getPropertyValue(name).trim();
  const SERIES = () => [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--s${i}`));

  // ------------------------------------------------------------------ formatting
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const pct = (v, d = 1, sign = false) => !isNum(v) ? "–" : `${sign && v > 0 ? "+" : ""}${v.toFixed(d)}%`;
  const num = (v, d = 2) => !isNum(v) ? "–" : v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const int = (v) => !isNum(v) ? "–" : Math.round(v).toLocaleString();
  const cls = (v) => !isNum(v) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function currency(sym) {
    if (/\.(NS|BO)$/.test(sym) || /^\^(NSEI|NSEBANK|BSESN|CNX)/.test(sym)) return "₹";
    if (/\.L$/.test(sym) || sym === "^FTSE") return "£";
    if (/\.(DE|PA|AS|MI|MC)$/.test(sym) || ["^GDAXI", "^FCHI", "^STOXX50E"].includes(sym)) return "€";
    if (/\.T$/.test(sym) || sym === "^N225") return "¥";
    return "$";
  }
  const money = (v, sym, d = 0) => !isNum(v) ? "–" : `${v < 0 ? "−" : ""}${currency(sym)}${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })}`;
  function priceFmt(v) {
    if (!isNum(v)) return "–";
    const a = Math.abs(v);
    const d = a >= 10 || a === 0 ? 2 : a >= 1 ? 4 : 5;
    return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  const ASSET = {
    equity: ["Equity", "--s1"], index: ["Index", "--s7"], commodity: ["Commodity", "--s4"],
    forex: ["Forex", "--s3"], crypto: ["Crypto", "--s2"], etf: ["ETF", "--s5"],
  };
  const assetPill = (ac) => {
    const [label, v] = ASSET[ac] || [ac, "--muted"];
    return `<span class="pill"><span class="sw" style="background:var(${v})"></span>${label}</span>`;
  };
  const INTERVALS = { "1d": "Daily bars", "1wk": "Weekly bars", "1mo": "Monthly bars", "1h": "Hourly bars" };

  // ------------------------------------------------------------------ colour helpers
  function rgb(c) {
    c = c.trim();
    if (c.startsWith("#")) {
      const h = c.length === 4 ? c.slice(1).split("").map((x) => x + x).join("") : c.slice(1);
      return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    }
    const m = c.match(/[\d.]+/g);
    return m ? m.slice(0, 3).map(Number) : [128, 128, 128];
  }
  const mix = (a, b, t) => { const A = rgb(a), B = rgb(b); return `rgb(${A.map((x, i) => Math.round(x + (B[i] - x) * t)).join(",")})`; };
  const alpha = (c, a) => `rgba(${rgb(c).join(",")},${a})`;
  function divColor(v, max) {
    if (!isNum(v)) return { bg: "transparent", fg: css("--muted") };
    const t = Math.max(-1, Math.min(1, v / (max || 1)));
    const bg = mix(css("--div-mid"), t >= 0 ? css("--div-pos") : css("--div-neg"), Math.pow(Math.abs(t), 0.8));
    return { bg, fg: Math.abs(t) > 0.55 ? "#ffffff" : css("--text") };
  }

  // ------------------------------------------------------------------ icons
  const ICON = {
    good: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5v.01"/></svg>',
    bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>',
    back: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>',
    spark: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M12 2l2.2 6.8L21 11l-6.8 2.2L12 20l-2.2-6.8L3 11l6.8-2.2z"/></svg>',
  };
  const VERDICT = {
    Promising: ["v-good", "good"], Favorable: ["v-good", "good"],
    Mixed: ["v-warn", "warn"], Neutral: ["v-warn", "warn"],
    Weak: ["v-bad", "bad"], Unfavorable: ["v-bad", "bad"],
  };
  const vpill = (label) => {
    const k = { Promising: "g", Favorable: "g", Mixed: "w", Neutral: "w", Weak: "b", Unfavorable: "b" }[label];
    return k ? `<span class="vpill ${k}">${esc(label)}</span>` : "";
  };

  // ------------------------------------------------------------------ charts
  function clearCharts() { state.charts.forEach((c) => { try { c.remove(); } catch { /* */ } }); state.charts = []; }

  function makeChart(el, { intraday = false, percent = false, money: moneySym = null } = {}) {
    const muted = css("--muted");
    const chart = LWC.createChart(el, {
      autoSize: true,
      layout: { background: { type: "solid", color: "transparent" }, textColor: muted, fontFamily: css("--font"), fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: css("--grid"), style: LWC.LineStyle.Solid } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.06 } },
      timeScale: { borderColor: css("--axis"), timeVisible: intraday, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: {
        mode: LWC.CrosshairMode.Magnet,
        vertLine: { color: alpha(css("--text-2"), 0.35), width: 1, style: LWC.LineStyle.Dashed, labelBackgroundColor: css("--text-2") },
        horzLine: { color: alpha(css("--text-2"), 0.35), width: 1, style: LWC.LineStyle.Dashed, labelBackgroundColor: css("--text-2") },
      },
      localization: {
        priceFormatter: percent ? (v) => `${v.toFixed(1)}%` : moneySym ? (v) => `${currency(moneySym)}${Math.round(v).toLocaleString()}` : priceFmt,
      },
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    });
    state.charts.push(chart);
    return chart;
  }
  const lineOpts = (color, extra = {}) => ({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 4, crosshairMarkerBorderWidth: 2, crosshairMarkerBorderColor: css("--surface"), ...extra });

  /** Hover legend: shows the value of every series under the crosshair (or the last value). */
  function hoverLegend(chart, el, items, fmt) {
    const render = (vals, time) => {
      el.innerHTML = (time ? `<span class="hover-date">${esc(fmtTime(time))}</span>` : `<span class="hover-date">Latest</span>`) +
        items.map((it, i) => `<span class="item"><span class="key ${it.dash ? "dash" : ""}" style="background:${it.color}"></span>${esc(it.label)} <span class="val">${vals[i] == null ? "–" : (it.fmt || fmt)(vals[i])}</span></span>`).join("");
    };
    const last = items.map((it) => { const d = it.data[it.data.length - 1]; return d ? (d.value ?? d.close) : null; });
    render(last, null);
    chart.subscribeCrosshairMove((p) => {
      if (!p || !p.time || !p.seriesData) return render(last, null);
      render(items.map((it) => { const d = p.seriesData.get(it.series); return d ? (d.value ?? d.close) : null; }), p.time);
    });
  }
  function fmtTime(t) {
    if (typeof t === "number") return new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ");
    if (typeof t === "string") return t;
    if (t && t.year) return `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`;
    return "";
  }
  /** Log scale reads long, compounding histories far better; pick it when the range is wide. */
  function wideRange(...datasets) {
    const vals = datasets.flat().map((d) => d.low ?? d.value).filter((v) => isNum(v) && v > 0);
    const highs = datasets.flat().map((d) => d.high ?? d.value).filter(isNum);
    return vals.length && Math.max(...highs) / Math.min(...vals) > 6;
  }
  function scaleToggle(segEl, chart, startLog) {
    const btns = [...segEl.children];
    const set = (mode) => { btns.forEach((x) => x.classList.toggle("on", Number(x.dataset.mode) === mode)); chart.priceScale("right").applyOptions({ mode }); };
    set(startLog ? 1 : 0);
    segEl.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) set(Number(b.dataset.mode)); });
  }
  function syncCharts(a, b) {
    let lock = false;
    const link = (src, dst) => src.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (lock || !r) return; lock = true; dst.timeScale().setVisibleLogicalRange(r); lock = false;
    });
    link(a, b); link(b, a);
  }

  // ------------------------------------------------------------------ API
  async function api(path, body) {
    const res = await fetch(BASE + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    let data = null;
    try { data = await res.json(); } catch { /* */ }
    if (!res.ok) throw new Error((data && (data.detail || data.message)) || `Request failed (${res.status})`);
    return data;
  }

  // ------------------------------------------------------------------ sidebar + examples
  const EXAMPLES = [
    ["Intraday · NIFTY / BANKNIFTY / SENSEX (1-minute)", "--s2", [
      "Nifty 15 min opening range breakout both sides, stop 40 points, target 80 points, exit at 3:15 pm",
      "Buy Nifty at 9:20 on gap up more than 0.3%, stop 30 points, target 60 points",
      "Sell Bank Nifty on a break of the previous day low, trail 60 points, max 2 trades",
    ]],
    ["Equities", "--s1", [
      "Backtest a 50/200 SMA crossover on AAPL since 2012 with a 10% trailing stop",
      "RSI(2) below 10, exit above 70 on MSFT over the last 8 years",
    ]],
    ["Indexes", "--s7", [
      "Which strategy works best on Nifty 50?",
      "Supertrend on Bank Nifty long/short since 2018",
    ]],
    ["Commodities", "--s4", [
      "Donchian 55/20 turtle breakout on gold, silver and crude oil",
      "Compare MACD vs EMA 20/50 crossover on natural gas",
    ]],
    ["Forex", "--s3", [
      "Bollinger 20, 2 reversion on EURUSD with 1 bps commission over 5 years",
      "12-month momentum on USDJPY long/short since 2010",
    ]],
    ["Should I invest?", "--s2", [
      "Is it good to invest in Reliance right now?",
      "Is the golden cross a good strategy for the S&P 500?",
    ]],
  ];
  function renderExamples() {
    $("#examples").innerHTML = EXAMPLES.map(([g, c, list]) => `
      <div class="ex-group"><div class="ex-head"><span class="sw" style="background:var(${c})"></span>${g}</div>
      ${list.map((p) => `<button type="button" data-prompt="${esc(p)}">${esc(p)}</button>`).join("")}</div>`).join("");
  }
  function renderHistory() {
    const h = store.get("st-history", []);
    $("#recentWrap").hidden = !h.length;
    $("#history").innerHTML = h.slice(0, 8).map((x) => `<button class="chip-btn" type="button" data-prompt="${esc(x)}" title="${esc(x)}">${esc(x)}</button>`).join("");
  }
  function pushHistory(p) {
    const h = store.get("st-history", []).filter((x) => x !== p);
    h.unshift(p); store.set("st-history", h.slice(0, 15)); renderHistory();
  }
  const LIB_PROMPT = {
    buy_hold: "Buy and hold the S&P 500 since 2000", sma_cross: "SMA 50/200 crossover on the S&P 500 since 2000",
    ema_cross: "EMA 20/50 crossover on gold over the last 10 years", price_sma: "Price above 200-day SMA on Nifty 50 since 2010",
    rsi: "RSI 14 below 30 exit above 70 on AAPL since 2015", macd: "MACD on EURUSD over the last 10 years",
    bollinger_reversion: "Bollinger 20, 2 reversion on SPY since 2012", bollinger_breakout: "Bollinger breakout on crude oil since 2015",
    donchian: "Donchian 20/10 turtle breakout on gold since 2010", momentum: "12-month momentum on Bank Nifty since 2012",
    supertrend: "Supertrend 10, 3 on Nifty 50 since 2015", stochastic: "Stochastic 14, 3 on GBPUSD over the last 8 years",
  };
  async function loadSidebar() {
    const el = $("#statusLine");
    try {
      const s = await api("/api/status");
      const src = "Data: Yahoo Finance";
      if (s.claude.enabled) { el.className = "statusline on"; el.innerHTML = `<span class="dot"></span><span>Claude · ${esc(s.claude.model)} · ${src}</span>`; }
      else { el.className = "statusline off"; el.title = "Set ANTHROPIC_API_KEY on the server to add Claude for prompt understanding and written commentary"; el.innerHTML = `<span class="dot"></span><span>Built-in parser · ${src}</span>`; }
    } catch { el.innerHTML = `<span class="dot"></span><span>Server unreachable</span>`; }
    try {
      const lib = await api("/api/strategies");
      $("#library").innerHTML = lib.map((s) => `<button class="chip-btn" type="button" data-prompt="${esc(LIB_PROMPT[s.key] || s.label)}" title="${esc(s.description)}">${esc(s.label)}<span class="fam">${esc(s.family)}</span></button>`).join("");
    } catch { /* */ }
  }

  // ------------------------------------------------------------------ composer
  const ta = $("#prompt");
  const autosize = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; };
  ta.addEventListener("input", autosize);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); run(ta.value); });
  shadow.addEventListener("click", (e) => {
    const b = e.target.closest("[data-prompt]");
    if (!b) return;
    ta.value = b.dataset.prompt; autosize(); run(ta.value);
  });
  $("#newBtn").addEventListener("click", () => {
    clearCharts(); state.rerender = null; state.compare = null;
    $("#results").innerHTML = ""; stEl.classList.remove("has-results"); ta.value = ""; autosize(); ta.focus();
  });

  // ------------------------------------------------------------------ run
  const STEPS = ["Understanding your request…", "Fetching market data from Yahoo Finance…", "Running the simulation…", "Computing risk metrics…", "Checking robustness…"];
  function showLoading() {
    clearCharts();
    stEl.classList.add("has-results");
    $("#results").innerHTML = `
      <div class="loading fade-in">
        <div class="card"><div class="loader-line"><span class="spinner"></span><span id="step">${STEPS[0]}</span></div></div>
        <div class="kpis">${"<div class='skel' style='height:92px'></div>".repeat(6)}</div>
        <div class="skel" style="height:380px"></div>
      </div>`;
    let i = 0;
    const t = setInterval(() => { const s = $("#step"); if (!s) return clearInterval(t); i = Math.min(i + 1, STEPS.length - 1); s.textContent = STEPS[i]; }, 1300);
    return () => clearInterval(t);
  }
  function showError(msg) {
    $("#results").innerHTML = `<div class="card error-card fade-in"><h3>Couldn't run that</h3><p>${esc(msg)}</p></div>`;
  }
  async function run(prompt) {
    prompt = (prompt || "").trim();
    if (!prompt || state.busy) return;
    state.busy = true; $("#sendBtn").disabled = true;
    pushHistory(prompt);
    const stop = showLoading();
    try {
      const resp = await api("/api/query", { prompt });
      stop();
      state.compare = resp.intent === "compare" ? resp : null;
      render(resp);
    } catch (e) { stop(); showError(e.message); }
    finally { state.busy = false; $("#sendBtn").disabled = false; }
  }
  async function drill(r) {
    const stop = showLoading();
    try {
      const resp = await api("/api/backtest", { symbol: r.symbol, strategy: r.strategy, params: r.params, start: r.period.start, end: r.period.end, interval: r.interval, config: r.config });
      stop(); render(resp, { back: true });
      host.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { stop(); showError(e.message); }
  }

  function render(resp, opts = {}) {
    const go = () => {
      clearCharts();
      const out = $("#results");
      out.innerHTML = "";
      if (resp.intent === "analyze") resp.results.forEach((r, i) => renderAnalysis(out, r, resp, i));
      else if (resp.intent === "compare") renderCompare(out, resp);
      else renderBacktest(out, resp.results[0], resp, opts);
    };
    state.rerender = go;
    go();
  }

  // ------------------------------------------------------------------ shared pieces
  function specChips(spec, r) {
    const chips = [];
    const p = r?.params || {};
    Object.entries(p).forEach(([k, v]) => chips.push(`<span class="chip">${esc(k.replace("_", " "))} <b>${esc(v)}</b></span>`));
    const c = r?.config || spec.config || {};
    if (isNum(c.capital)) chips.push(`<span class="chip">Capital <b>${money(c.capital, r?.symbol || "")}</b></span>`);
    chips.push(`<span class="chip">${c.allow_short ? "<b>Long / short</b>" : "<b>Long only</b>"}</span>`);
    if (isNum(c.stop_loss_pct)) chips.push(`<span class="chip">Stop loss <b>${c.stop_loss_pct}%</b></span>`);
    if (isNum(c.take_profit_pct)) chips.push(`<span class="chip">Take profit <b>${c.take_profit_pct}%</b></span>`);
    if (isNum(c.trailing_stop_pct)) chips.push(`<span class="chip">Trailing stop <b>${c.trailing_stop_pct}%</b></span>`);
    if (isNum(c.commission_bps)) chips.push(`<span class="chip">Costs <b>${c.commission_bps} + ${c.slippage_bps ?? 0} bps</b> per side</span>`);
    if (isNum(c.position_size) && c.position_size < 1) chips.push(`<span class="chip">Position <b>${Math.round(c.position_size * 100)}%</b></span>`);
    return `<div class="chips">${chips.join("")}</div>` + ((spec.notes || []).length ? `<div class="notes">${spec.notes.map((n) => `<div>${esc(n)}</div>`).join("")}</div>` : "");
  }
  const parserPill = (spec) => spec.parser === "claude" ? `<span class="ai-badge">${ICON.spark} Parsed by Claude</span>` : "";

  function markdown(md) {
    const lines = esc(md).split(/\r?\n/);
    let html = "", inList = false;
    const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|\W)\*(.+?)\*(?=\W|$)/g, "$1<em>$2</em>");
    for (const raw of lines) {
      const l = raw.trim();
      if (/^[-*•]\s+/.test(l)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(l.replace(/^[-*•]\s+/, ""))}</li>`; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      if (/^#{1,6}\s/.test(l)) html += `<h3>${inline(l.replace(/^#+\s*/, ""))}</h3>`;
      else if (l) html += `<p>${inline(l)}</p>`;
    }
    if (inList) html += "</ul>";
    return html;
  }
  function commentaryCard(text) {
    return text ? `<div class="card fade-in"><div class="card-title"><span>Analyst commentary</span><span class="ai-badge">${ICON.spark} Written by Claude</span></div><div class="commentary">${markdown(text)}</div></div>` : "";
  }
  function verdictBlock(v, { label, score, summary, pros, cons, prosTitle = "Strengths", consTitle = "Weaknesses" }) {
    const [vc, ic] = VERDICT[label] || ["v-neutral", "warn"];
    return `
      <div class="card fade-in ${vc}">
        <div class="verdict">
          <div class="ring" style="--p:${score ?? 0}"><div class="ring-inner"><div class="ring-score">${score ?? "–"}</div><div class="ring-of">/ 100</div></div></div>
          <div>
            <div class="v-label">${ICON[ic]}${esc(label)}</div>
            <p class="v-summary">${esc(summary)}</p>
          </div>
        </div>
        ${(pros?.length || cons?.length) ? `<div class="proscons">
          <div class="pc pro"><h4>${ICON.good.replace("<svg", '<svg width="14" height="14"')}${prosTitle}</h4><ul>${(pros || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>None identified.</li>"}</ul></div>
          <div class="pc con"><h4>${ICON.bad.replace("<svg", '<svg width="14" height="14"')}${consTitle}</h4><ul>${(cons || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>None identified.</li>"}</ul></div>
        </div>` : ""}
      </div>`;
  }

  // ------------------------------------------------------------------ BACKTEST view
  function renderBacktest(out, r, resp, opts) {
    const m = r.metrics, b = r.benchmark, sym = r.symbol, intraday = !["1d", "1wk", "1mo"].includes(r.interval);
    const html = [];
    html.push(`
      <div class="card fade-in">
        <div class="r-head">
          <div>
            <div class="r-kicker">${assetPill(r.asset_class)}<span>${INTERVALS[r.interval] || r.interval}</span><span>·</span><span class="num">${r.period.start} → ${r.period.end}</span><span>·</span><span>${num(m.years, 1)} yrs</span>${parserPill(resp.spec)}</div>
            <h2 class="r-title">${esc(r.strategy_name)} <span class="sym">${esc(r.name)} · ${esc(sym)}</span></h2>
            <div class="r-sub">${esc(r.strategy_description)}</div>
          </div>
          ${opts.back && state.compare ? `<button class="back-btn" id="backBtn">${ICON.back} Back to comparison</button>` : ""}
        </div>
        ${specChips(resp.spec, r)}
        ${(r.notes || []).length ? `<div class="notes">${r.notes.map((n) => `<div>${esc(n)}</div>`).join("")}</div>` : ""}
      </div>`);
    if (r.verdict && r.verdict.label !== "Benchmark") {
      html.push(verdictBlock(null, { label: r.verdict.label, score: r.verdict.score, summary: r.verdict.summary, pros: r.verdict.pros, cons: r.verdict.cons }));
    }
    const d = (a, bb, better = "higher") => {
      if (!isNum(a) || !isNum(bb)) return "";
      const diff = a - bb; const good = better === "higher" ? diff >= 0 : diff <= 0;
      return `<span class="delta ${good ? "up" : "down"}">${diff >= 0 ? "+" : ""}${diff.toFixed(Math.abs(diff) < 10 ? 2 : 1)}</span>`;
    };
    const kpi = (label, value, sub, cl = "") => `<div class="kpi fade-in"><div class="kpi-label">${label}</div><div class="kpi-value ${cl}">${value}</div><div class="kpi-sub">${sub}</div></div>`;
    html.push(`<div class="kpis">
      ${kpi("Total return", pct(m.total_return, 1, true), `B&amp;H ${pct(b.total_return, 1, true)}`, cls(m.total_return))}
      ${kpi("CAGR", pct(m.cagr, 2, true), `B&amp;H ${pct(b.cagr, 2, true)} ${d(m.cagr, b.cagr)}`)}
      ${kpi("Sharpe ratio", num(m.sharpe), `B&amp;H ${num(b.sharpe)} ${d(m.sharpe, b.sharpe)}`)}
      ${kpi("Max drawdown", pct(m.max_drawdown, 1), `B&amp;H ${pct(b.max_drawdown, 1)} ${d(m.max_drawdown, b.max_drawdown)}`, "neg")}
      ${kpi("Win rate", pct(m.win_rate, 1), `Avg trade ${pct(m.avg_trade, 2, true)}`)}
      ${kpi("Trades", int(m.trades), `In market ${pct(m.exposure, 0)} of the time`)}
    </div>`);
    html.push(`
      <div class="card fade-in">
        <div class="card-title"><span>Equity curve <span class="hint">· ${money(r.config.capital, sym)} starting capital</span></span>
          <div class="seg" id="scaleSeg"><button class="on" data-mode="0">Linear</button><button data-mode="1">Log</button></div></div>
        <div class="legend" id="eqLegend"></div>
        <div class="chart" id="eqChart"></div>
        <div class="card-title" style="margin:18px 0 8px"><span>Drawdown</span></div>
        <div class="legend" id="ddLegend"></div>
        <div class="chart sm" id="ddChart"></div>
      </div>
      <div class="card fade-in">
        <div class="card-title"><span>Price &amp; trades <span class="hint">· ▲ entries ▼ exits · scroll to zoom, drag to pan</span></span>
          <div class="seg" id="pxSeg"><button data-mode="0">Linear</button><button data-mode="1">Log</button></div></div>
        <div class="legend" id="pxLegend"></div>
        <div class="chart lg" id="pxChart"></div>
      </div>`);
    html.push(`<div class="grid-3-2">
      <div class="card fade-in"><div class="card-title"><span>Monthly returns</span><span class="hint">strategy, %</span></div><div id="monthly"></div></div>
      <div class="card fade-in"><div class="card-title"><span>Performance summary</span></div><div class="table-wrap">${metricsTable(m, b, sym)}</div></div>
    </div>`);
    if ((r.robustness || r.validation) && r.strategy !== "buy_hold") html.push(robustnessCard(r));
    html.push(commentaryCard(resp.commentary));
    html.push(tradesCard(r));
    out.insertAdjacentHTML("beforeend", html.join(""));

    // --- charts
    const s1 = css("--s1"), bench = css("--bench");
    const eq = makeChart($("#eqChart"), { intraday, money: sym });
    const sEq = eq.addLineSeries(lineOpts(s1)); sEq.setData(r.series.equity);
    const sBh = eq.addLineSeries(lineOpts(bench, { lineWidth: 1.5, lineStyle: LWC.LineStyle.Dashed })); sBh.setData(r.series.benchmark);
    eq.timeScale().fitContent();
    const mfmt = (v) => money(v, sym);
    hoverLegend(eq, $("#eqLegend"), [
      { label: "Strategy", color: s1, series: sEq, data: r.series.equity },
      { label: "Buy & hold", color: bench, series: sBh, data: r.series.benchmark, dash: true },
    ], mfmt);
    scaleToggle($("#scaleSeg"), eq, wideRange(r.series.equity, r.series.benchmark));

    const dd =makeChart($("#ddChart"), { intraday, percent: true });
    const sDd = dd.addAreaSeries({ lineColor: s1, topColor: alpha(s1, 0.04), bottomColor: alpha(s1, 0.28), lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, invertFilledArea: true });
    sDd.setData(r.series.drawdown);
    const sBdd = dd.addLineSeries(lineOpts(bench, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed }));
    sBdd.setData(r.series.benchmark_drawdown);
    dd.timeScale().fitContent();
    hoverLegend(dd, $("#ddLegend"), [
      { label: "Strategy", color: s1, series: sDd, data: r.series.drawdown },
      { label: "Buy & hold", color: bench, series: sBdd, data: r.series.benchmark_drawdown, dash: true },
    ], (v) => pct(v, 1));
    syncCharts(eq, dd);

    const px = makeChart($("#pxChart"), { intraday });
    const candles = px.addCandlestickSeries({ upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down"), priceLineVisible: false });
    candles.setData(r.series.price);
    const colors = SERIES();
    const ovItems = [{ label: "Close", color: css("--text-2"), series: candles, data: r.series.price }];
    Object.entries(r.series.overlays || {}).forEach(([name, data], i) => {
      const c = colors[(i + 1) % colors.length];
      const s = px.addLineSeries(lineOpts(c, { lineWidth: 1.5, crosshairMarkerVisible: false })); s.setData(data);
      ovItems.push({ label: name, color: c, series: s, data });
    });
    const buyC = css("--s1"), sellC = css("--s8");
    candles.setMarkers((r.markers || []).map((mk) => ({ time: mk.time, position: mk.position, shape: mk.shape, color: mk.text === "Buy" || mk.text === "Cover" ? buyC : sellC, text: (r.markers.length > 60 ? "" : mk.text), size: r.markers.length > 60 ? 0.6 : 1 })));
    px.timeScale().fitContent();
    scaleToggle($("#pxSeg"), px, wideRange(r.series.price));
    hoverLegend(px, $("#pxLegend"), ovItems, priceFmt);

    $("#monthly").innerHTML = monthlyHeat(r.monthly);
    if (r.robustness?.sensitivity) $("#sensHeat").innerHTML = sensHeat(r.robustness.sensitivity);
    const back = $("#backBtn");
    if (back) back.addEventListener("click", () => { render(state.compare); host.scrollIntoView({ block: "start" }); });
    const csvBtn = $("#csvBtn");
    if (csvBtn) csvBtn.addEventListener("click", () => downloadCSV(r));
  }

  function metricsTable(m, b, sym) {
    const rows = [
      ["Final equity", money(m.final_equity, sym), money(b.final_equity, sym)],
      ["Total return", pct(m.total_return, 1, true), pct(b.total_return, 1, true)],
      ["CAGR", pct(m.cagr, 2, true), pct(b.cagr, 2, true)],
      ["Volatility (ann.)", pct(m.volatility, 1), pct(b.volatility, 1)],
      ["Sharpe", num(m.sharpe), num(b.sharpe)],
      ["Sortino", num(m.sortino), num(b.sortino)],
      ["Calmar", num(m.calmar), num(b.calmar)],
      ["Max drawdown", pct(m.max_drawdown, 1), pct(b.max_drawdown, 1)],
      ["Longest drawdown", `${int(m.max_dd_days)} days`, `${int(b.max_dd_days)} days`],
      ["Profit factor", num(m.profit_factor), ""],
      ["Avg win / loss", `${pct(m.avg_win, 2, true)} / ${pct(m.avg_loss, 2)}`, ""],
      ["Best / worst trade", `${pct(m.best_trade, 1, true)} / ${pct(m.worst_trade, 1)}`, ""],
      ["Avg holding", `${num(m.avg_bars, 0)} bars`, ""],
      ["Trades per year", num(m.trades_per_year, 1), ""],
      ["Beta to B&H", num(m.beta), "1.00"],
      ["Alpha (ann.)", pct(m.alpha, 2, true), ""],
    ];
    return `<table class="metric-table"><thead><tr><th></th><th class="n">Strategy</th><th class="n">Buy &amp; hold</th></tr></thead><tbody>
      ${rows.map(([k, a, c]) => `<tr><td>${k}</td><td class="n">${a}</td><td class="n" style="color:var(--text-2)">${c}</td></tr>`).join("")}</tbody></table>`;
  }

  function monthlyHeat(monthly) {
    if (!monthly?.rows?.length) return "<div class='hint'>Not enough data.</div>";
    const vals = monthly.rows.flatMap((r) => r.months).filter(isNum).map(Math.abs).sort((a, b) => a - b);
    const max = vals.length ? vals[Math.floor(vals.length * 0.92)] || vals[vals.length - 1] : 1;
    const yv = monthly.rows.map((r) => r.total).filter(isNum).map(Math.abs);
    const ymax = Math.max(...yv, 1);
    const M = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
    const MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let h = `<div class="table-wrap"><div class="heat" style="grid-template-columns:44px repeat(12,minmax(30px,1fr)) minmax(48px,1.3fr);min-width:520px"><div></div>${M.map((x) => `<div class="hh">${x}</div>`).join("")}<div class="hh">Year</div>`;
    monthly.rows.slice().reverse().forEach((row) => {
      h += `<div class="hy">${row.year}</div>`;
      row.months.forEach((v, i) => {
        if (!isNum(v)) { h += `<div class="hc empty"></div>`; return; }
        if (v === 0) { h += `<div class="hc flat" title="${MN[i]} ${row.year}: no position">·</div>`; return; }
        const c = divColor(v, max);
        h += `<div class="hc" style="background:${c.bg};color:${c.fg}" title="${MN[i]} ${row.year}: ${pct(v, 2, true)}">${v.toFixed(1)}</div>`;
      });
      const c = divColor(row.total, ymax);
      h += `<div class="hc tot" style="background:${c.bg};color:${c.fg}" title="${row.year}: ${pct(row.total, 2, true)}">${isNum(row.total) ? row.total.toFixed(1) : "–"}</div>`;
    });
    h += `</div></div><div class="heat-legend"><span>Loss</span><span class="bar" style="background:linear-gradient(90deg,${css("--div-neg")},${css("--div-mid")},${css("--div-pos")})"></span><span>Gain</span></div>`;
    return h;
  }

  function robustnessCard(r) {
    const sp = r.robustness?.split, s = r.robustness?.sensitivity, v = r.validation || {};
    const wf = v.walk_forward, rnd = v.random, conf = v.confidence, mon = v.monthly, bs = v.bootstrap;
    const box = (lbl, value, sub, bad) => `<div class="split-box"><div class="lbl">${lbl}</div><div class="v ${bad ? "neg" : ""}">${value}<small>${sub}</small></div></div>`;
    const parts = [];
    if (wf) {
      const rows = wf.folds.map((f) => `<tr><td class="num">${esc(f.test_start)} → ${esc(f.test_end)}</td><td>${esc(Object.entries(f.params).map(([k, x]) => `${k} ${x}`).join(", "))}</td>
        <td class="n">${num(f.train_sharpe)}</td><td class="n ${cls(f.test_return)}">${pct(f.test_return, 1, true)}</td><td class="n ${cls(f.bh_return)}">${pct(f.bh_return, 1, true)}</td></tr>`).join("");
      parts.push(`<div class="card-title" style="margin-bottom:8px"><span>Walk-forward <span class="hint">· settings picked on the previous ${isNum(wf.train_bars) ? wf.train_bars + " bars" : esc(wf.train_bars)} only, then traded on the next ${isNum(wf.test_bars) ? wf.test_bars + " unseen bars" : "unseen " + esc(wf.test_bars)}</span></span></div>
        <div class="split-row">${box("Unseen periods, chained", `${pct(wf.oos.cagr, 1, true)}`, ` a year · Sharpe ${num(wf.oos.sharpe)} · max DD ${pct(wf.oos.max_drawdown, 1)}`, !(wf.oos.sharpe > 0))}
        ${box("Buy &amp; hold, same periods", `${pct(wf.oos_benchmark.cagr, 1, true)}`, ` a year · Sharpe ${num(wf.oos_benchmark.sharpe)}`, false)}</div>
        <div class="hint" style="margin:-6px 0 8px">${wf.folds_positive}/${wf.folds.length} periods positive · ${wf.folds_beat_bh}/${wf.folds.length} beat buy &amp; hold · the same settings were picked ${Math.round(wf.param_stability)}% of the time</div>
        <div class="table-wrap"><table><thead><tr><th>Unseen period</th><th>Settings picked</th><th class="n">Train Sharpe</th><th class="n">Result</th><th class="n">B&amp;H</th></tr></thead><tbody>${rows}</tbody></table></div>`);
    }
    const tiles = [];
    if (rnd) tiles.push(box("Timing vs random entries", `beat ${Math.round(rnd.percentile)}%`,
      ` of ${rnd.samples} random sets · avg trade ${pct(rnd.actual_avg_trade, 2, true)} vs ${pct(rnd.random_median, 2, true)}`, rnd.p_value > 0.2));
    if (conf) tiles.push(box(conf.trials > 1 ? `Likely real (best of ${conf.trials})` : "Likely real", `${Math.round(conf.probability)}%`,
      ` chance the true Sharpe is above ${conf.trials > 1 ? num(conf.hurdle_sharpe) + " (luck hurdle)" : "zero"}`, conf.probability < 80));
    if (mon && mon.positive_months != null) tiles.push(box("Monthly stability", `${Math.round(mon.positive_months)}%`,
      ` of ${mon.months} months positive · worst ${pct(mon.worst_month, 1)} · ${mon.positive_years}/${mon.years} years up`, mon.positive_months < 45));
    if (bs) tiles.push(box("90% range (resampled months)", `${pct(bs.cagr_p05, 1, true)} … ${pct(bs.cagr_p95, 1, true)}`,
      ` a year · Sharpe ${num(bs.sharpe_p05)} … ${num(bs.sharpe_p95)}`, bs.cagr_p05 < 0));
    if (sp) tiles.push(box(`Most recent 30% · after ${sp.split_date}`, num(sp.out_of_sample.sharpe), ` Sharpe · earlier 70%: ${num(sp.in_sample.sharpe)}`, !(sp.out_of_sample.sharpe > 0)));
    if (tiles.length) parts.push(`<div class="card-title" style="margin:14px 0 8px"><span>Is the edge real?</span></div><div class="split-row">${tiles.join("")}</div>`);
    if (s) parts.push(`<div class="card-title" style="margin-bottom:8px"><span>Parameter sensitivity <span class="hint">· Sharpe ratio across nearby settings${isNum(s.stability) ? ` · ${Math.round(s.stability)}% hold up` : ""}</span></span></div><div id="sensHeat"></div>`);
    return `<div class="card fade-in">
      <div class="card-title"><span>Validation</span><span class="hint">Would it have worked without hindsight?</span></div>
      ${parts.join("")}
    </div>`;
  }

  function sensHeat(s) {
    const flat = s.sharpe.flat().filter(isNum);
    const max = Math.max(...flat.map(Math.abs), 0.5);
    const cols = s.x_values.length;
    let h = `<div class="table-wrap"><div class="heat" style="grid-template-columns:${s.y_param ? "72px " : ""}repeat(${cols},minmax(54px,1fr));min-width:${cols * 60 + 80}px">`;
    if (s.y_param) h += `<div class="hh" style="text-align:left">${esc(s.y_param)} ↓</div>`;
    h += s.x_values.map((x) => `<div class="hh">${esc(x)}</div>`).join("");
    s.sharpe.forEach((row, yi) => {
      const yv = s.y_values[yi];
      if (s.y_param) h += `<div class="hy">${esc(yv)}</div>`;
      row.forEach((v, xi) => {
        const base = s.x_values[xi] === s.base.x && (!s.y_param || yv === s.base.y);
        if (!isNum(v)) { h += `<div class="hc empty" style="color:var(--muted)">n/a</div>`; return; }
        const c = divColor(v, max);
        h += `<div class="hc ${base ? "base" : ""}" style="background:${c.bg};color:${c.fg}" title="${esc(s.x_param)} ${s.x_values[xi]}${s.y_param ? `, ${esc(s.y_param)} ${yv}` : ""}: Sharpe ${num(v)}${base ? " (tested)" : ""}">${v.toFixed(2)}</div>`;
      });
    });
    h += `</div></div><div class="heat-legend"><span>${esc(s.x_param)} →</span><span style="margin-left:auto">Negative</span><span class="bar" style="background:linear-gradient(90deg,${css("--div-neg")},${css("--div-mid")},${css("--div-pos")})"></span><span>Positive</span><span>· outlined = tested setting</span></div>`;
    return h;
  }

  function tradesCard(r) {
    const t = r.trades || [];
    if (!t.length) return `<div class="card fade-in"><div class="card-title"><span>Trades</span></div><div class="hint">No trades were triggered in this period.</div></div>`;
    const hasC = t.some((x) => x.contract);
    const rows = t.slice().reverse().map((x, i) => `<tr>
      <td class="n" style="color:var(--muted)">${t.length - i}</td>
      <td><span class="tag ${x.side === "Long" ? "long" : "short"}">${x.side}</span></td>${hasC ? `<td class="num">${esc(x.contract || "")}</td>` : ""}
      <td class="num">${x.entry_time}</td><td class="n">${priceFmt(x.entry_price)}</td>
      <td class="num">${x.open ? '<span class="tag">Open</span>' : x.exit_time}</td><td class="n">${priceFmt(x.exit_price)}</td>
      <td class="n ${cls(x.return_pct)}">${pct(x.return_pct, 2, true)}</td>
      <td class="n ${cls(x.pnl)}">${money(x.pnl, r.symbol)}</td>
      <td class="n">${x.bars}</td><td style="color:var(--text-2)">${esc(x.exit_reason)}</td></tr>`).join("");
    return `<div class="card fade-in"><div class="card-title"><span>Trades <span class="hint">· ${t.length} total, newest first</span></span><button class="btn-sm" id="csvBtn" type="button">Export CSV</button></div>
      <div class="table-wrap scroll-y"><table><thead><tr><th class="n">#</th><th>Side</th>${hasC ? "<th>Contract</th>" : ""}<th>Entry</th><th class="n">Price</th><th>Exit</th><th class="n">Price</th><th class="n">Return</th><th class="n">P&amp;L</th><th class="n">Bars</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }
  function downloadCSV(r) {
    const head = ["side"].concat(r.trades.some((t) => t.contract) ? ["contract"] : []).concat(["entry_time", "entry_price", "exit_time", "exit_price", "return_pct", "pnl", "bars", "exit_reason"]);
    const csv = [head.join(",")].concat(r.trades.map((t) => head.map((k) => JSON.stringify(t[k] ?? "")).join(","))).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `${r.symbol}_${r.strategy}_trades.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ------------------------------------------------------------------ COMPARE view
  function renderCompare(out, resp) {
    const rs = resp.results, spec = resp.spec;
    const symbols = [...new Set(rs.map((r) => r.symbol))];
    const strategies = [...new Set(rs.map((r) => r.strategy_name))];
    const bySymbol = symbols.length > 1 && strategies.length === 1;
    const oneSymbol = symbols.length === 1;
    const first = rs[0];
    const colors = SERIES();
    const title = oneSymbol ? `${strategies.length} strategies on ${first.name}` : bySymbol ? `${first.strategy_name} across ${symbols.length} markets` : `${rs.length} strategy × market combinations`;

    const best = rs[0];
    const bhBeat = rs.filter((r) => (r.metrics.sharpe ?? -9) > (r.benchmark.sharpe ?? -9)).length;
    const html = [];
    html.push(`<div class="card fade-in">
      <div class="r-kicker">${[...new Set(rs.map((r) => r.asset_class))].map(assetPill).join("")}<span>${INTERVALS[first.interval] || ""}</span><span>·</span><span class="num">${first.period.start} → ${first.period.end}</span>${parserPill(spec)}</div>
      <h2 class="r-title">${esc(title)}${oneSymbol ? ` <span class="sym">${esc(first.symbol)}</span>` : ""}</h2>
      <div class="r-sub">Ranked by Sharpe ratio over the whole period - the top row is partly luck when many strategies are tried; "Likely real" allows for that. <b>${esc(best.strategy_name)}</b>${oneSymbol ? "" : ` on <b>${esc(best.name)}</b>`} came out on top, and ${bhBeat} of ${rs.length} beat buy &amp; hold on risk-adjusted return. Click any row for the full report.</div>
      ${specChips(spec, { config: first.config, symbol: first.symbol })}
      ${(resp.errors || []).length ? `<div class="notes">${resp.errors.map((e) => `<div>Skipped: ${esc(e)}</div>`).join("")}</div>` : ""}
    </div>`);

    const shown = rs.slice(0, 8);
    const rows = rs.map((r, i) => {
      const col = i < 8 ? colors[i] : "transparent";
      const vs = (r.metrics.cagr ?? 0) - (r.benchmark.cagr ?? 0);
      const main = bySymbol ? r.name : r.strategy_name;
      const sub = bySymbol ? r.symbol : oneSymbol ? r.family : `${r.name} · ${r.symbol}`;
      return `<tr class="click" data-i="${i}">
        <td><span class="rank ${i === 0 ? "r1" : ""}">${i + 1}</span></td>
        <td><div class="name-cell"><span class="sw" style="background:${col}"></span><div><div class="t">${esc(main)}</div><div class="s">${esc(sub)}</div></div></div></td>
        <td class="n ${cls(r.metrics.cagr)}">${pct(r.metrics.cagr, 2, true)}</td>
        <td class="n"><b>${num(r.metrics.sharpe)}</b></td>
        <td class="n neg">${pct(r.metrics.max_drawdown, 1)}</td>
        <td class="n">${pct(r.metrics.win_rate, 0)}</td>
        <td class="n">${int(r.metrics.trades)}</td>
        <td class="n">${pct(r.metrics.exposure, 0)}</td>
        <td class="n ${cls(vs)}">${pct(vs, 2, true)}</td>
        <td class="n">${isNum(r.validation?.confidence?.probability) ? Math.round(r.validation.confidence.probability) + "%" : "–"}</td>
        <td>${vpill(r.verdict?.label)}</td></tr>`;
    }).join("");
    const bhRow = oneSymbol ? `<tr><td></td><td><div class="name-cell"><span class="sw" style="height:3px;background:repeating-linear-gradient(90deg,var(--bench) 0 4px,transparent 4px 6px)"></span><div><div class="t">Buy &amp; hold</div><div class="s">Benchmark</div></div></div></td>
      <td class="n ${cls(first.benchmark.cagr)}">${pct(first.benchmark.cagr, 2, true)}</td><td class="n"><b>${num(first.benchmark.sharpe)}</b></td><td class="n neg">${pct(first.benchmark.max_drawdown, 1)}</td><td class="n">–</td><td class="n">1</td><td class="n">100%</td><td class="n">–</td><td class="n">–</td><td></td></tr>` : "";
    html.push(`<div class="card fade-in"><div class="card-title"><span>Leaderboard</span><span class="hint">vs B&amp;H = CAGR difference to buying and holding</span></div>
      <div class="table-wrap"><table><thead><tr><th>#</th><th>${bySymbol ? "Market" : "Strategy"}</th><th class="n">CAGR</th><th class="n">Sharpe</th><th class="n">Max DD</th><th class="n">Win rate</th><th class="n">Trades</th><th class="n">Exposure</th><th class="n">vs B&amp;H</th><th class="n" title="Chance the true Sharpe is above what the best of these strategies would reach by luck">Likely real</th><th>Verdict</th></tr></thead>
      <tbody>${rows}${bhRow}</tbody></table></div></div>`);
    html.push(`<div class="card fade-in"><div class="card-title"><span>Equity curves <span class="hint">· top ${shown.length}, same starting capital</span></span>
      <div class="seg" id="scaleSeg"><button class="on" data-mode="0">Linear</button><button data-mode="1">Log</button></div></div>
      <div class="legend" id="cmpLegend"></div><div class="chart lg" id="cmpChart"></div></div>`);
    html.push(commentaryCard(resp.commentary));
    out.insertAdjacentHTML("beforeend", html.join(""));

    const intraday = !["1d", "1wk", "1mo"].includes(first.interval);
    const ch = makeChart($("#cmpChart"), { intraday, money: oneSymbol ? first.symbol : "" });
    const items = shown.map((r, i) => {
      const s = ch.addLineSeries(lineOpts(colors[i], { lineWidth: i === 0 ? 2.5 : 1.75 }));
      s.setData(r.series.equity);
      return { label: bySymbol ? r.name : (oneSymbol ? r.strategy_name : `${r.strategy_name} · ${r.symbol}`), color: colors[i], series: s, data: r.series.equity };
    });
    if (oneSymbol && first.series.benchmark) {
      const s = ch.addLineSeries(lineOpts(css("--bench"), { lineWidth: 1.5, lineStyle: LWC.LineStyle.Dashed }));
      s.setData(first.series.benchmark);
      items.push({ label: "Buy & hold", color: css("--bench"), series: s, data: first.series.benchmark, dash: true });
    }
    ch.timeScale().fitContent();
    hoverLegend(ch, $("#cmpLegend"), items, (v) => money(v, oneSymbol ? first.symbol : ""));
    scaleToggle($("#scaleSeg"), ch, wideRange(...items.map((x) => x.data)));
    out.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => drill(rs[Number(tr.dataset.i)])));
  }

  // ------------------------------------------------------------------ ANALYSIS view
  function renderAnalysis(out, r, resp, idx) {
    const st = r.stats, f = r.fundamentals || {}, sym = r.symbol, id = `a${idx}`;
    const scoreColor = (v) => v >= 62 ? css("--good") : v >= 45 ? css("--warn") : css("--bad");
    const html = [];
    html.push(`<div class="card fade-in"><div class="r-head">
      <div>
        <div class="r-kicker">${assetPill(r.asset_class)}${f.sector ? `<span>${esc(f.sector)}</span><span>·</span><span>${esc(f.industry || "")}</span>` : ""}${parserPill(resp.spec)}</div>
        <h2 class="r-title">${esc(r.name)} <span class="sym">${esc(sym)}</span></h2>
        <div class="r-sub">Investment scorecard based on trend, momentum, risk and track record${r.asset_class === "equity" ? ", plus valuation and business quality" : ""}. Data to ${st.date}.</div>
        ${idx === 0 && (resp.spec?.notes || []).length ? `<div class="notes">${resp.spec.notes.map((n) => `<div>${esc(n)}</div>`).join("")}</div>` : ""}
      </div>
      <div class="price-head"><div class="price-big">${currency(sym)}${priceFmt(st.price)}</div><div class="${cls(st.change_1d)}" style="font-weight:600">${pct(st.change_1d, 2, true)} today</div></div>
    </div></div>`);
    html.push(verdictBlock(null, { label: r.label, score: r.score, summary: r.summary, pros: r.bull_points, cons: r.bear_points, prosTitle: "Supporting evidence", consTitle: "Reasons for caution" }));
    const stat = (l, v, c = "") => `<div class="stat"><div class="l">${l}</div><div class="v ${c}">${v}</div></div>`;
    const rp = isNum(st.range_position_52w) ? Math.max(0, Math.min(100, st.range_position_52w)) : null;
    html.push(`<div class="grid-2">
      <div class="card fade-in"><div class="card-title"><span>Factor scores</span><span class="hint">0–100, higher is better</span></div>
        ${r.factors.map((x) => `<div class="factor"><div class="factor-name">${esc(x.name)}</div><div class="bar-track"><div class="bar-fill" style="width:${x.score}%;background:${scoreColor(x.score)}"></div></div><div class="factor-score">${x.score}</div><div class="factor-detail">${esc(x.detail)}</div></div>`).join("")}
      </div>
      <div class="card fade-in"><div class="card-title"><span>Performance &amp; risk</span></div>
        <div class="stat-grid">
          ${stat("1 month", pct(st.ret_1m, 1, true), cls(st.ret_1m))}${stat("3 months", pct(st.ret_3m, 1, true), cls(st.ret_3m))}
          ${stat("6 months", pct(st.ret_6m, 1, true), cls(st.ret_6m))}${stat("1 year", pct(st.ret_1y, 1, true), cls(st.ret_1y))}
          ${stat("3y CAGR", pct(st.cagr_3y, 1, true), cls(st.cagr_3y))}${stat("5y CAGR", pct(st.cagr_5y, 1, true), cls(st.cagr_5y))}
          ${stat("Volatility (1y)", pct(st.volatility_1y, 1))}${stat("Sharpe (3y)", num(st.sharpe_3y))}
          ${stat("Max DD (5y)", pct(st.max_drawdown_5y, 1), "neg")}${stat("From all-time high", pct(st.from_ath, 1), cls(st.from_ath))}
          ${stat("RSI (14)", num(st.rsi14, 0))}${stat("vs 200-day avg", st.sma200 ? pct((st.price / st.sma200 - 1) * 100, 1, true) : "–", st.sma200 ? cls(st.price - st.sma200) : "")}
        </div>
        ${rp != null ? `<div class="range52"><div class="kpi-label">52-week range · ${num(rp, 0)}% of the way up</div><div class="track"><div class="mark" style="left:${rp}%"></div></div><div class="ends"><span>${priceFmt(st.low_52w)}</span><span>${priceFmt(st.high_52w)}</span></div></div>` : ""}
      </div></div>`);
    html.push(`<div class="card fade-in"><div class="card-title"><span>Price · last 5 years</span></div><div class="legend" id="${id}pxL"></div><div class="chart lg" id="${id}px"></div></div>`);
    const fit = r.strategy_fit || [];
    html.push(`<div class="grid-3-2">
      <div class="card fade-in"><div class="card-title"><span>Which approach has worked here?</span><span class="hint">last 10 years, ranked by Sharpe</span></div>
        <div class="table-wrap"><table><thead><tr><th>Approach</th><th class="n">CAGR</th><th class="n">Sharpe</th><th class="n">Max DD</th><th class="n">In market</th></tr></thead><tbody>
        ${fit.map((x, i) => `<tr><td><div class="name-cell"><span class="sw" style="background:${SERIES()[i % 8]}"></span><span class="t">${esc(x.name)}</span></div></td><td class="n ${cls(x.cagr)}">${pct(x.cagr, 1, true)}</td><td class="n"><b>${num(x.sharpe)}</b></td><td class="n neg">${pct(x.max_drawdown, 1)}</td><td class="n">${pct(x.exposure, 0)}</td></tr>`).join("")}
        </tbody></table></div></div>
      <div class="card fade-in"><div class="card-title"><span>Growth of ${money(100000, sym)}</span></div><div class="legend" id="${id}fitL"></div><div class="chart" id="${id}fit"></div></div>
    </div>`);
    if (r.asset_class === "equity" && Object.keys(f).length) {
      const fs = [
        ["Market cap", isNum(f.marketCap) ? `${currency(sym)}${f.marketCap >= 1e12 ? (f.marketCap / 1e12).toFixed(2) + "T" : (f.marketCap / 1e9).toFixed(1) + "B"}` : "–"],
        ["Trailing P/E", num(f.trailingPE, 1)], ["Forward P/E", num(f.forwardPE, 1)], ["Price / book", num(f.priceToBook, 1)],
        ["PEG", num(f.trailingPegRatio ?? f.pegRatio, 2)], ["Profit margin", isNum(f.profitMargins) ? pct(f.profitMargins * 100, 1) : "–"],
        ["Revenue growth", isNum(f.revenueGrowth) ? pct(f.revenueGrowth * 100, 1, true) : "–"], ["ROE", isNum(f.returnOnEquity) ? pct(f.returnOnEquity * 100, 1) : "–"],
        ["Debt / equity", isNum(f.debtToEquity) ? `${f.debtToEquity.toFixed(0)}%` : "–"], ["Dividend yield", isNum(f.dividendYield) ? pct(f.dividendYield, 2) : "–"],
        ["Beta", num(f.beta, 2)], ["Analyst target", isNum(f.targetMeanPrice) ? `${currency(sym)}${priceFmt(f.targetMeanPrice)}` : "–"],
      ];
      html.push(`<div class="card fade-in"><div class="card-title"><span>Fundamentals</span>${f.recommendationKey ? `<span class="hint">Analyst consensus: <b>${esc(String(f.recommendationKey).replace("_", " "))}</b>${f.numberOfAnalystOpinions ? ` (${f.numberOfAnalystOpinions} analysts)` : ""}</span>` : ""}</div>
        <div class="stat-grid">${fs.map(([l, v]) => stat(l, v)).join("")}</div></div>`);
    }
    if (idx === resp.results.length - 1) html.push(commentaryCard(resp.commentary));
    out.insertAdjacentHTML("beforeend", html.join(""));

    const px = makeChart($(`#${id}px`));
    const c = px.addCandlestickSeries({ upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down"), priceLineVisible: false });
    c.setData(r.series.price);
    const colors = SERIES();
    const s50 = px.addLineSeries(lineOpts(colors[1], { lineWidth: 1.5, crosshairMarkerVisible: false })); s50.setData(r.series.sma50);
    const s200 = px.addLineSeries(lineOpts(colors[6], { lineWidth: 1.5, crosshairMarkerVisible: false })); s200.setData(r.series.sma200);
    px.timeScale().fitContent();
    hoverLegend(px, $(`#${id}pxL`), [
      { label: "Close", color: css("--text-2"), series: c, data: r.series.price },
      { label: "50-day", color: colors[1], series: s50, data: r.series.sma50 },
      { label: "200-day", color: colors[6], series: s200, data: r.series.sma200 },
    ], priceFmt);

    const fc = makeChart($(`#${id}fit`), { money: sym });
    const items = fit.slice(0, 8).map((x, i) => {
      const data = r.strategy_curves[x.name] || [];
      const s = fc.addLineSeries(lineOpts(colors[i], { lineWidth: i === 0 ? 2.25 : 1.5 })); s.setData(data);
      return { label: x.name, color: colors[i], series: s, data };
    });
    fc.timeScale().fitContent();
    hoverLegend(fc, $(`#${id}fitL`), items, (v) => money(v, sym));
  }

  // ------------------------------------------------------------------ init
  renderExamples();
  renderHistory();
  loadSidebar();
  ta.focus();
  // Shareable links: /?q=your+prompt runs the prompt on load
  const q = new URLSearchParams(location.search).get("q");
  if (q) { ta.value = q; autosize(); run(q); }
})();
