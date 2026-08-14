(() => {
  "use strict";

  const API = "/labs/simulation/api";
  const SESSION_KEY = "labsSimulationSessionId";
  const colors = ["#28d7c0", "#f4ad45", "#7c9cff", "#f26b8a", "#9d7cff", "#67d68a", "#d6cf67"];
  const overlayPrefixes = ["SMA", "EMA", "VWAP", "BB", "Supertrend"];
  let sessionId = localStorage.getItem(SESSION_KEY);
  let bootstrap = null;
  let snapshot = null;
  let orderSide = "BUY";
  let timer = null;
  let seenNotes = new Set();
  let indicatorSpecs = [];
  let mainIndicatorSeries = [];
  let oscillatorSeries = [];
  let priceLines = [];

  const $ = id => document.getElementById(id);
  const money = value => `Rs ${Number(value || 0).toLocaleString("en-IN", {maximumFractionDigits: 2})}`;
  const number = value => value === null || value === undefined ? "--" : Number(value).toLocaleString("en-IN", {maximumFractionDigits: 2});
  const field = id => $(id).value === "" ? null : Number($(id).value);
  const timeText = iso => iso ? new Date(iso).toLocaleTimeString("en-IN", {timeZone: "Asia/Kolkata", hour12: false}) : "--";

  const chart = LightweightCharts.createChart($("chart"), chartOptions());
  const candleSeries = chart.addCandlestickSeries({upColor: "#20c77a", downColor: "#f05252", borderVisible: false, wickUpColor: "#20c77a", wickDownColor: "#f05252", priceLineVisible: true});
  const oscillatorChart = LightweightCharts.createChart($("oscillator-chart"), {...chartOptions(), rightPriceScale: {borderColor: "#253141"}, timeScale: {visible: false}});

  function chartOptions() {
    return {
      autoSize: true,
      layout: {background: {color: "#101720"}, textColor: "#7890a8", fontFamily: "IBM Plex Mono"},
      grid: {vertLines: {color: "#182331"}, horzLines: {color: "#182331"}},
      crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
      rightPriceScale: {borderColor: "#253141"},
      timeScale: {borderColor: "#253141", timeVisible: true, secondsVisible: false, rightOffset: 4},
      localization: {locale: "en-IN"},
    };
  }

  async function api(path, options = {}) {
    const response = await fetch(API + path, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Simulation request failed");
    return payload;
  }

  async function initialize() {
    try {
      bootstrap = (await api("/bootstrap")).bootstrap;
      populateInstruments();
      if (!sessionId) await createSession();
      else {
        try { render(await api(`/sessions/${sessionId}`)); }
        catch (_) { await createSession(); }
      }
      bindEvents();
    } catch (error) { showError(error); }
  }

  async function createSession() {
    const result = await api("/sessions", {method: "POST", body: JSON.stringify({starting_capital: bootstrap.starting_capital})});
    sessionId = result.session_id;
    localStorage.setItem(SESSION_KEY, sessionId);
    render(result);
  }

  function populateInstruments() {
    const grouped = {};
    bootstrap.instruments.forEach(item => (grouped[item.group] ||= []).push(item));
    $("instrument").innerHTML = Object.entries(grouped).map(([group, items]) =>
      `<optgroup label="${escapeHtml(group)}">${items.map(i => `<option value="${i.symbol}">${escapeHtml(i.name)} (${i.symbol})</option>`).join("")}</optgroup>`
    ).join("");
  }

  async function configure(extra = {}) {
    const mode = $("simulation-mode").value;
    const body = {
      instrument: $("instrument").value,
      mode,
      trade_date: mode === "HISTORICAL" ? ($("trade-date").value || null) : null,
      speed: Number($("speed").value),
      timeframe: activeTimeframe(),
      chart_type: $("chart-type").value,
      indicators: indicatorSpecs,
      slippage: {mode: $("slippage-mode").value, value: Number($("slippage-value").value || 0)},
      ...extra,
    };
    render(await api(`/sessions/${sessionId}/configure`, {method: "POST", body: JSON.stringify(body)}));
  }

  function bindEvents() {
    $("simulation-mode").addEventListener("change", modeChanged);
    $("instrument").addEventListener("change", async () => { await configure(); await loadDates(); });
    $("trade-date").addEventListener("change", () => configure().catch(showError));
    $("fetch-data").addEventListener("click", fetchData);
    $("start").addEventListener("click", startReplay);
    $("play").addEventListener("click", playReplay);
    $("pause").addEventListener("click", pauseReplay);
    $("restart").addEventListener("click", restartReplay);
    $("speed").addEventListener("change", () => configure().catch(showError));
    $("chart-type").addEventListener("change", () => configure().catch(showError));
    $("timeframes").addEventListener("click", event => {
      const button = event.target.closest("button[data-value]"); if (!button) return;
      $("timeframes").querySelectorAll("button").forEach(b => b.classList.toggle("active", b === button));
      configure().catch(showError);
    });
    $("indicator-button").addEventListener("click", () => $("indicator-drawer").classList.toggle("hidden"));
    $("indicator-drawer").addEventListener("click", indicatorClick);
    $("buy-side").addEventListener("click", () => setSide("BUY"));
    $("sell-side").addEventListener("click", () => setSide("SELL"));
    $("order-form").addEventListener("submit", placeOrder);
    document.querySelectorAll("input[name=order-type]").forEach(input => input.addEventListener("change", updateOrderFields));
    document.querySelector(".tabbar").addEventListener("click", switchTab);
    $("positions-body").addEventListener("click", positionAction);
    $("orders-body").addEventListener("click", orderAction);
    window.addEventListener("resize", () => { chart.timeScale().scrollToRealTime(); });
    loadDates().catch(() => {});
  }

  async function fetchData() {
    try {
      const instrument = $("instrument").value, tradeDate = $("trade-date").value;
      if (!tradeDate) throw new Error("Select a historical day first");
      $("fetch-data").disabled = true; $("fetch-data").textContent = "FETCHING...";
      const result = await api("/data/fetch", {method: "POST", body: JSON.stringify({instrument, trade_date: tradeDate})});
      toast(`${result.result.candles} one-minute candles ready`, "success");
      await loadDates();
    } catch (error) { showError(error); }
    finally { $("fetch-data").disabled = false; $("fetch-data").textContent = "Fetch / verify data"; }
  }

  async function loadDates() {
    if ($("simulation-mode").value === "LIVE_PAPER") return;
    const result = await api(`/dates?instrument=${encodeURIComponent($("instrument").value)}`);
    if (!$("trade-date").value && result.dates.length) $("trade-date").value = result.dates[0];
  }

  async function startReplay() {
    try { await configure(); render(await api(`/sessions/${sessionId}/start`, {method: "POST", body: "{}"})); startTimer(); }
    catch (error) { showError(error); }
  }
  async function playReplay() {
    try { render(await api(`/sessions/${sessionId}/status`, {method: "POST", body: JSON.stringify({status: "PLAYING"})})); startTimer(); }
    catch (error) { showError(error); }
  }
  async function pauseReplay() {
    stopTimer();
    try { render(await api(`/sessions/${sessionId}/status`, {method: "POST", body: JSON.stringify({status: "PAUSED"})})); }
    catch (error) { showError(error); }
  }
  async function restartReplay() {
    if (!confirm("Restart this replay? Orders, positions and trades will be cleared.")) return;
    stopTimer();
    try {
      const mode = $("simulation-mode").value;
      const body = {instrument: $("instrument").value, mode, trade_date: mode === "HISTORICAL" ? $("trade-date").value : null, starting_capital: snapshot.state.starting_capital};
      render(await api(`/sessions/${sessionId}/reset`, {method: "POST", body: JSON.stringify(body)}));
      await startReplay();
    } catch (error) { showError(error); }
  }
  function startTimer() { stopTimer(); timer = setInterval(stepReplay, $("simulation-mode").value === "LIVE_PAPER" ? 15000 : 1000); }
  function stopTimer() { if (timer) clearInterval(timer); timer = null; }
  async function stepReplay() {
    if (!snapshot || snapshot.state.replay_status !== "PLAYING") return stopTimer();
    try {
      const count = $("simulation-mode").value === "LIVE_PAPER" ? 1 : Number($("speed").value);
      render(await api(`/sessions/${sessionId}/step`, {method: "POST", body: JSON.stringify({count})}));
      if (snapshot.state.replay_status === "COMPLETE") { stopTimer(); toast("Replay complete. End-of-day summary is ready.", "success"); }
    } catch (error) { stopTimer(); showError(error); }
  }

  async function placeOrder(event) {
    event.preventDefault();
    try {
      const orderType = document.querySelector("input[name=order-type]:checked").value;
      const body = {side: orderSide, qty: Number($("quantity").value), order_type: orderType,
        limit_price: orderType === "LIMIT" ? field("limit-price") : null,
        trigger_price: orderType === "STOP" ? field("trigger-price") : null,
        stop_loss: field("stop-loss"), target: field("target")};
      render(await api(`/sessions/${sessionId}/orders`, {method: "POST", body: JSON.stringify(body)}));
    } catch (error) { showError(error); }
  }

  async function positionAction(event) {
    const button = event.target.closest("button[data-exit], button[data-manage]"); if (!button) return;
    const symbol = button.dataset.exit || button.dataset.manage;
    const position = snapshot.state.positions.find(p => p.instrument === symbol);
    try {
      if (button.dataset.manage) {
        const stopLoss = prompt("Stop-loss price (leave blank to keep current)", position.stop_loss ?? "");
        if (stopLoss === null) return;
        const target = prompt("Target price (leave blank to keep current)", position.target ?? "");
        if (target === null) return;
        const body = {};
        if (stopLoss !== "") body.stop_loss = Number(stopLoss);
        if (target !== "") body.target = Number(target);
        render(await api(`/sessions/${sessionId}/positions/${position.instrument}`, {method: "PATCH", body: JSON.stringify(body)}));
        return;
      }
      const requested = prompt(`Exit quantity (1-${position.qty})`, position.qty); if (requested === null) return;
      render(await api(`/sessions/${sessionId}/positions/${position.instrument}/exit`, {method: "POST", body: JSON.stringify({qty: Number(requested)})}));
    }
    catch (error) { showError(error); }
  }
  async function orderAction(event) {
    const button = event.target.closest("button[data-cancel], button[data-modify]"); if (!button) return;
    try {
      if (button.dataset.modify) {
        const order = snapshot.state.orders.find(o => o.order_id === button.dataset.modify);
        const quantity = prompt("Order quantity", order.qty); if (quantity === null) return;
        const body = {qty: Number(quantity)};
        if (order.order_type === "LIMIT") {
          const limit = prompt("Limit price", order.limit_price); if (limit === null) return;
          body.limit_price = Number(limit);
        }
        if (order.order_type === "STOP") {
          const trigger = prompt("Trigger price", order.trigger_price); if (trigger === null) return;
          body.trigger_price = Number(trigger);
        }
        render(await api(`/sessions/${sessionId}/orders/${order.order_id}`, {method: "PATCH", body: JSON.stringify(body)}));
        return;
      }
      render(await api(`/sessions/${sessionId}/orders/${button.dataset.cancel}`, {method: "DELETE"}));
    }
    catch (error) { showError(error); }
  }

  async function indicatorClick(event) {
    if (event.target.id === "reset-indicators") indicatorSpecs = [];
    else {
      const button = event.target.closest("button[data-indicator]"); if (!button) return;
      const name = button.dataset.indicator;
      const existing = indicatorSpecs.findIndex(s => s.name === name);
      if (existing >= 0) indicatorSpecs.splice(existing, 1);
      else indicatorSpecs.push({name, params: button.dataset.period ? {period: Number(button.dataset.period)} : {}});
    }
    try { await configure(); } catch (error) { showError(error); }
  }

  function render(payload) {
    snapshot = payload;
    const state = payload.state, account = state.account;
    indicatorSpecs = state.indicators || [];
    $("simulation-mode").value = state.mode || "HISTORICAL";
    applyModeUi();
    $("instrument").value = state.instrument;
    if (state.trade_date) $("trade-date").value = state.trade_date;
    $("speed").value = state.speed;
    $("chart-type").value = state.chart_type;
    $("slippage-mode").value = state.slippage.mode; $("slippage-value").value = state.slippage.value;
    $("timeframes").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.value === state.timeframe));
    $("replay-status").textContent = state.replay_status;
    $("sim-time").textContent = timeText(state.current_timestamp);
    $("historical-ts").textContent = state.current_timestamp ? new Date(state.current_timestamp).toLocaleString("en-IN", {timeZone: "Asia/Kolkata"}) : "Select a day";
    const meta = bootstrap.instruments.find(i => i.symbol === state.instrument) || {};
    $("quote-symbol").textContent = $("ticket-symbol").textContent = state.instrument;
    $("quote-name").textContent = meta.name || "";
    $("last-price").textContent = $("ticket-price").textContent = number(state.current_price);
    $("account-value").textContent = money(account.net_account_value);
    $("available-cash").textContent = money(account.available_cash); $("used-capital").textContent = money(account.used_capital);
    pnlText($("realized-pnl"), account.realized_pnl - account.charges); pnlText($("unrealized-pnl"), account.unrealized_pnl); $("charges").textContent = money(account.charges);
    renderChart(payload.chart || {candles: [], indicators: {}});
    renderPositions(state.positions); renderOrders(state.orders); renderTrades(state.trades); renderPerformance(state.performance);
    renderNotes(state.notifications); renderIndicatorLabels();
  }

  function renderChart(data) {
    candleSeries.setData(data.candles || []);
    mainIndicatorSeries.forEach(s => chart.removeSeries(s)); mainIndicatorSeries = [];
    oscillatorSeries.forEach(s => oscillatorChart.removeSeries(s)); oscillatorSeries = [];
    Object.entries(data.indicators || {}).forEach(([key, values], index) => {
      const label = key.split(":").slice(1).join(":");
      const overlay = overlayPrefixes.some(prefix => label.startsWith(prefix));
      const host = overlay ? chart : oscillatorChart;
      const series = host.addLineSeries({title: label, color: colors[index % colors.length], lineWidth: label.includes("DI") ? 1 : 2, priceLineVisible: false, lastValueVisible: true});
      series.setData(values); (overlay ? mainIndicatorSeries : oscillatorSeries).push(series);
    });
    $("oscillator-chart").classList.toggle("hidden", oscillatorSeries.length === 0);
    priceLines.forEach(line => candleSeries.removePriceLine(line)); priceLines = [];
    const markers = [];
    if (snapshot) {
      snapshot.state.orders.filter(o => o.status === "FILLED").forEach(o => markers.push({time: epoch(o.updated_at), position: o.side === "BUY" ? "belowBar" : "aboveBar", color: o.side === "BUY" ? "#20c77a" : "#f05252", shape: o.side === "BUY" ? "arrowUp" : "arrowDown", text: `${o.side} ${o.filled_qty} @ ${o.filled_price}`}));
      snapshot.state.positions.forEach(p => {
        addPriceLine(p.avg_price, "Entry", "#5b8cff"); if (p.stop_loss) addPriceLine(p.stop_loss, "SL", "#f05252"); if (p.target) addPriceLine(p.target, "Target", "#20c77a");
      });
      snapshot.state.orders.filter(o => ["OPEN", "TRIGGER_PENDING"].includes(o.status)).forEach(o => addPriceLine(o.limit_price || o.trigger_price, o.order_type, "#f4ad45"));
    }
    candleSeries.setMarkers(markers.filter(m => m.time).sort((a, b) => a.time - b.time));
    $("bar-progress").textContent = `${data.visible_count_1m || 0} / ${data.total_count_1m || 0} one-minute candles visible`;
    if (data.candles?.length) chart.timeScale().scrollToRealTime();
    if (data.error) showAlert(data.error);
  }
  function addPriceLine(price, title, color) { if (!price) return; priceLines.push(candleSeries.createPriceLine({price:Number(price), color, lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title})); }

  function renderPositions(rows) {
    $("position-count").textContent = rows.length;
    $("positions-body").innerHTML = rows.length ? rows.map(p => `<tr><td>${p.instrument}</td><td class="${p.side === "LONG" ? "positive" : "negative"}">${p.side}</td><td>${p.qty}</td><td>${number(p.avg_price)}</td><td>${number(p.current_price)}</td><td class="${p.unrealized_pnl >= 0 ? "positive" : "negative"}">${money(p.unrealized_pnl)}</td><td>${money(p.realized_pnl)}</td><td>${number(p.stop_loss)}</td><td>${number(p.target)}</td><td><div class="row-actions"><button class="row-action" data-manage="${p.instrument}">Manage</button><button class="row-action danger-soft" data-exit="${p.instrument}">Exit</button></div></td></tr>`).join("") : emptyRow(10, "No open positions");
  }
  function renderOrders(rows) {
    $("order-count").textContent = rows.length;
    $("orders-body").innerHTML = rows.length ? [...rows].reverse().map(o => `<tr><td>${timeText(o.timestamp)}</td><td>${o.instrument}</td><td class="${o.side === "BUY" ? "positive" : "negative"}">${o.side}</td><td>${o.qty}</td><td>${o.order_type}</td><td>${number(o.limit_price)}</td><td>${number(o.trigger_price)}</td><td><span class="status ${o.status}">${o.status}</span></td><td>${number(o.filled_price)}</td><td>${["OPEN","TRIGGER_PENDING"].includes(o.status) ? `<div class="row-actions"><button class="row-action" data-modify="${o.order_id}">Modify</button><button class="row-action danger-soft" data-cancel="${o.order_id}">Cancel</button></div>` : ""}</td></tr>`).join("") : emptyRow(10, "No orders yet");
  }
  function renderTrades(rows) {
    $("trade-count").textContent = rows.length;
    $("trades-body").innerHTML = rows.length ? [...rows].reverse().map(t => `<tr><td>${timeText(t.entry_time)}</td><td>${timeText(t.exit_time)}</td><td>${t.instrument}</td><td>${t.direction}</td><td>${t.qty}</td><td>${number(t.entry_price)}</td><td>${number(t.exit_price)}</td><td class="${t.gross_pnl >= 0 ? "positive" : "negative"}">${money(t.gross_pnl)}</td><td>${money(t.charges)}</td><td class="${t.net_pnl >= 0 ? "positive" : "negative"}">${money(t.net_pnl)}</td><td>${t.exit_reason}</td></tr>`).join("") : emptyRow(11, "No completed trades");
  }
  function renderPerformance(stats) {
    const labels = {trades:"Trades",winning_trades:"Winners",losing_trades:"Losers",win_rate:"Win rate %",gross_pnl:"Gross P&L",net_pnl:"Net P&L",charges:"Charges",largest_winner:"Largest winner",largest_loser:"Largest loser",max_realized_drawdown:"Max drawdown",average_trade:"Average trade",average_winner:"Average winner",average_loser:"Average loser",profit_factor:"Profit factor"};
    $("performance-grid").innerHTML = Object.entries(labels).map(([key,label]) => `<div><span>${label}</span><strong>${key.includes("pnl") || key.includes("winner") || key.includes("loser") || key.includes("drawdown") || key.includes("average") || key === "charges" ? money(stats[key]) : number(stats[key])}</strong></div>`).join("");
  }
  function renderNotes(notes) { notes.forEach(note => { if (!seenNotes.has(note.id)) { seenNotes.add(note.id); toast(note.message, note.level); } }); }
  function renderIndicatorLabels() { $("active-indicators").textContent = indicatorSpecs.length ? indicatorSpecs.map(s => `${s.name}${s.params?.period ? ` ${s.params.period}` : ""}`).join(" | ") : "No indicators"; document.querySelectorAll("[data-indicator]").forEach(b => b.classList.toggle("active", indicatorSpecs.some(s => s.name === b.dataset.indicator))); }

  function setSide(side) { orderSide = side; $("buy-side").classList.toggle("active", side === "BUY"); $("sell-side").classList.toggle("active", side === "SELL"); $("place-order").className = `place ${side.toLowerCase()}`; $("place-order").textContent = `PLACE ${side} ORDER`; }
  async function modeChanged() {
    const mode = $("simulation-mode").value;
    applyModeUi();
    const hasActivity = snapshot && (snapshot.state.orders.length || snapshot.state.positions.length || snapshot.state.trades.length);
    if (hasActivity) {
      if (!confirm("Changing mode clears this simulation's orders, positions and trades. Continue?")) {
        $("simulation-mode").value = snapshot.state.mode || "HISTORICAL";
        return applyModeUi();
      }
    }
    stopTimer();
    const body = {instrument: $("instrument").value, mode, trade_date: mode === "HISTORICAL" ? $("trade-date").value : null, starting_capital: snapshot.state.starting_capital};
    try { render(await api(`/sessions/${sessionId}/reset`, {method: "POST", body: JSON.stringify(body)})); }
    catch (error) { showError(error); }
  }
  function applyModeUi() {
    const live = $("simulation-mode").value === "LIVE_PAPER";
    $("date-field").classList.toggle("hidden", live);
    $("fetch-data").classList.toggle("hidden", live);
    $("speed").disabled = live;
    $("clock-label").textContent = live ? "LATEST COMPLETED BAR" : "SIMULATED TIME";
    $("start").textContent = live ? "START LIVE" : "START";
  }
  function updateOrderFields() { const type = document.querySelector("input[name=order-type]:checked").value; $("limit-price").disabled = type !== "LIMIT"; $("trigger-price").disabled = type !== "STOP"; }
  function switchTab(event) { const button = event.target.closest("button[data-tab]"); if (!button) return; document.querySelectorAll(".tabbar button").forEach(b => b.classList.toggle("active", b === button)); document.querySelectorAll(".tab-content").forEach(p => p.classList.toggle("active", p.id === `${button.dataset.tab}-tab`)); }
  function activeTimeframe() { return $("timeframes").querySelector("button.active")?.dataset.value || "1m"; }
  function epoch(iso) { if (!iso) return null; return Math.floor(new Date(iso).getTime() / 1000); }
  function emptyRow(cols, text) { return `<tr><td colspan="${cols}" class="empty-row">${text}</td></tr>`; }
  function pnlText(element, value) { element.textContent = money(value); element.className = value >= 0 ? "positive" : "negative"; }
  function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
  function showAlert(message) { $("alert").textContent = message; $("alert").classList.remove("hidden"); }
  function showError(error) { showAlert(error.message || String(error)); toast(error.message || String(error), "error"); }
  function toast(message, level="info") { const node=document.createElement("div"); node.className=`toast ${level}`; node.textContent=message; $("toasts").appendChild(node); setTimeout(()=>node.remove(),4500); }

  updateOrderFields();
  initialize();
})();
