/* Labs frontend — Chart.js equity curve + trade/signal log loaders + condition builder */

document.addEventListener("DOMContentLoaded", () => {
  // Condition builder — runs on bot_form.html (where EXISTING_LEGS is defined)
  if (typeof EXISTING_LEGS !== "undefined") {
    initConditionBuilder();
  }

  // Detail-page logic — runs on bot_detail.html (where BOT_ID is defined)
  if (typeof BOT_ID !== "undefined") {
    loadEquityChart();
    loadTradeLog();
    loadSignalLog();
  }
});

// ══════════════════════════════════════════════════════════════════════════════
// CONDITION BUILDER
// ══════════════════════════════════════════════════════════════════════════════

// Condition type definitions per section
// Each entry: { value, label, params: [{name, label, default, type}], allowTimeframe }
const CONDITION_DEFS = {
  entry: [
    { value: "rsi_lt",                 label: "RSI <",                params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:30,type:"number"}], tf: true },
    { value: "rsi_gt",                 label: "RSI >",                params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:70,type:"number"}], tf: true },
    { value: "ema_gt",                 label: "EMA(a) > EMA(b)",      params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "ema_lt",                 label: "EMA(a) < EMA(b)",      params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_gt",                 label: "MA(a) > MA(b)",        params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_lt",                 label: "MA(a) < MA(b)",        params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "adx_diplus_gt_diminus",  label: "ADX: DI+ > DI−",      params: [{name:"period",label:"Period",default:14,type:"number"}], tf: true },
    { value: "adx_diminus_gt_diplus",  label: "ADX: DI− > DI+",      params: [{name:"period",label:"Period",default:14,type:"number"}], tf: true },
  ],
  gate: [
    { value: "spot_gt_sma",       label: "Spot > SMA",               params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "spot_lt_sma",       label: "Spot < SMA",               params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "spot_above_sma_by", label: "Spot above SMA by ≥ pts",  params: [{name:"period",label:"SMA period",default:50,type:"number"},{name:"value",label:"Points",default:0,type:"number"}], tf: true },
    { value: "spot_below_sma_by", label: "Spot below SMA by ≥ pts",  params: [{name:"period",label:"SMA period",default:50,type:"number"},{name:"value",label:"Points",default:0,type:"number"}], tf: true },
    { value: "ema_gt",            label: "EMA(a) > EMA(b)",          params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "ema_lt",            label: "EMA(a) < EMA(b)",          params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_gt",            label: "MA(a) > MA(b)",            params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_lt",            label: "MA(a) < MA(b)",            params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
  ],
  exit: [
    { value: "spot_gain_gte", label: "Spot gain ≥ pts",  params: [{name:"value",label:"Points",default:50,type:"number"}], tf: false },
    { value: "spot_loss_gte", label: "Spot loss ≥ pts",  params: [{name:"value",label:"Points",default:30,type:"number"}], tf: false },
    { value: "ltp_gain_gte",  label: "LTP gain ≥ pts",   params: [{name:"value",label:"Points",default:20,type:"number"}], tf: false },
    { value: "ltp_loss_gte",  label: "LTP loss ≥ pts",   params: [{name:"value",label:"Points",default:15,type:"number"}], tf: false },
    { value: "spot_gt_sma",   label: "Spot > SMA",        params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "spot_lt_sma",   label: "Spot < SMA",        params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "rsi_gt",        label: "RSI >",            params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:70,type:"number"}], tf: true },
    { value: "rsi_lt",        label: "RSI <",            params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:30,type:"number"}], tf: true },
    { value: "ema_gt",        label: "EMA(a) > EMA(b)",  params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "ema_lt",        label: "EMA(a) < EMA(b)",  params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_gt",        label: "MA(a) > MA(b)",    params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_lt",        label: "MA(a) < MA(b)",    params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_gt_spot",   label: "SMA > Spot",       params: [{name:"period",label:"Period",default:50,type:"number"}], tf: true },
    { value: "sma_lt_spot",   label: "SMA < Spot",       params: [{name:"period",label:"Period",default:50,type:"number"}], tf: true },
  ],
  stoploss: [
    { value: "spot_loss_gte", label: "Spot loss ≥ pts",  params: [{name:"value",label:"Points",default:30,type:"number"}], tf: false },
    { value: "ltp_loss_gte",  label: "LTP loss ≥ pts",   params: [{name:"value",label:"Points",default:15,type:"number"}], tf: false },
    { value: "spot_gain_gte", label: "Spot gain ≥ pts",  params: [{name:"value",label:"Points",default:50,type:"number"}], tf: false },
    { value: "ltp_gain_gte",  label: "LTP gain ≥ pts",   params: [{name:"value",label:"Points",default:20,type:"number"}], tf: false },
    { value: "spot_gt_sma",   label: "Spot > SMA",       params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "spot_lt_sma",   label: "Spot < SMA",       params: [{name:"period",label:"SMA period",default:50,type:"number"}], tf: true },
    { value: "rsi_gt",        label: "RSI >",            params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:70,type:"number"}], tf: true },
    { value: "rsi_lt",        label: "RSI <",            params: [{name:"period",label:"Period",default:3,type:"number"},{name:"value",label:"Value",default:30,type:"number"}], tf: true },
    { value: "sma_gt",        label: "MA(a) > MA(b)",    params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
    { value: "sma_lt",        label: "MA(a) < MA(b)",    params: [{name:"period_a",label:"Fast period",default:9,type:"number"},{name:"period_b",label:"Slow period",default:21,type:"number"}], tf: true },
  ],
};

const TIMEFRAMES = ["1m", "5m", "10m", "15m"];
const LEG_CODES  = ["C1", "C2", "P1", "P2"];

// Section code → JSON field id suffix
const SECTION_KEY = { entry: "entry", gate: "gate", exit: "exit", stoploss: "stoploss" };

function initConditionBuilder() {
  // Populate existing legs from server-seeded EXISTING_LEGS
  LEG_CODES.forEach(code => {
    const legData = EXISTING_LEGS[code];
    if (!legData || !legData.is_enabled) return;

    _populateSection(code, "entry",    _parseJSON(legData.entry_conditions_json));
    _populateSection(code, "gate",     _parseJSON(legData.entry_gates_json));
    _populateSection(code, "exit",     _parseJSON(legData.exit_conditions_json));
    _populateSection(code, "stoploss", _parseJSON(legData.stoploss_conditions_json));
  });
}

function _parseJSON(val) {
  try { return JSON.parse(val || "[]"); } catch { return []; }
}

function _populateSection(code, section, conditions) {
  conditions.forEach(cond => addRow(code, section, cond));
}

// ── Toggle leg card visibility ───────────────────────────────────────────────

function toggleLegCard(code, enabled) {
  const card = document.getElementById(`legCard_${code}`);
  if (card) card.style.display = enabled ? "" : "none";
}

// ── Add a condition row ──────────────────────────────────────────────────────

function addRow(legCode, section, existingCond) {
  const defs      = CONDITION_DEFS[section];
  const container = document.getElementById(`${legCode}_${section}_rows`);
  if (!container) return;

  const rowId   = `${legCode}_${section}_${Date.now()}_${Math.random().toString(36).slice(2,6)}`;
  const first   = defs[0];
  const selType = existingCond ? existingCond.type : first.value;
  const selDef  = defs.find(d => d.value === selType) || first;

  const row = document.createElement("div");
  row.className = "cond-row";
  row.id        = rowId;

  // Type select
  const typeOpts = defs.map(d =>
    `<option value="${d.value}" ${d.value === selType ? "selected" : ""}>${d.label}</option>`
  ).join("");

  // Param inputs for the selected type
  const paramHtml = _buildParamInputs(selDef, existingCond);

  // Timeframe select (hidden if !tf)
  const selTf       = existingCond ? (existingCond.timeframe || "5m") : "5m";
  const tfDisplay   = selDef.tf ? "" : "display:none";
  const tfOpts      = TIMEFRAMES.map(tf =>
    `<option value="${tf}" ${tf === selTf ? "selected" : ""}>${tf}</option>`
  ).join("");

  // Enabled checkbox
  const isEnabled = existingCond ? (existingCond.enabled !== false) : true;

  row.innerHTML = `
    <select class="cond-type" onchange="updateConditionParams(this, '${section}')">
      ${typeOpts}
    </select>
    <span class="cond-params">${paramHtml}</span>
    <label class="tf-wrap" style="${tfDisplay}">
      TF&nbsp;<select class="cond-tf">${tfOpts}</select>
    </label>
    <label class="enabled-wrap" title="Enable/disable this condition">
      <input type="checkbox" class="cond-enabled" ${isEnabled ? "checked" : ""}> On
    </label>
    <button type="button" class="btn-del" onclick="deleteRow('${rowId}')">✕</button>
  `;

  container.appendChild(row);
}

// ── Delete a row ─────────────────────────────────────────────────────────────

function deleteRow(rowId) {
  const el = document.getElementById(rowId);
  if (el) el.remove();
}

// ── Rebuild param inputs when type changes ───────────────────────────────────

function updateConditionParams(select, section) {
  const row    = select.closest(".cond-row");
  const selVal = select.value;
  const defs   = CONDITION_DEFS[section];
  const def    = defs.find(d => d.value === selVal);
  if (!def) return;

  row.querySelector(".cond-params").innerHTML = _buildParamInputs(def, null);

  const tfWrap = row.querySelector(".tf-wrap");
  if (tfWrap) tfWrap.style.display = def.tf ? "" : "none";
}

// ── Build param <input> elements for a definition ────────────────────────────

function _buildParamInputs(def, existingCond) {
  return def.params.map(p => {
    const val = existingCond && existingCond.params && existingCond.params[p.name] !== undefined
      ? existingCond.params[p.name]
      : p.default;
    return `<label class="param-label">${p.label}&nbsp;<input
      type="${p.type}" class="cond-param" data-param="${p.name}"
      value="${val}" step="any" style="width:60px"></label>`;
  }).join(" ");
}

// ── Serialize all legs to hidden JSON inputs before submit ────────────────────

function serializeAllLegs() {
  LEG_CODES.forEach(code => {
    ["entry", "gate", "exit", "stoploss"].forEach(section => {
      const container = document.getElementById(`${code}_${section}_rows`);
      if (!container) return;

      const rows = container.querySelectorAll(".cond-row");
      const conditions = Array.from(rows).map(row => {
        const type    = row.querySelector(".cond-type").value;
        const tfEl    = row.querySelector(".cond-tf");
        const tf      = tfEl ? tfEl.value : "5m";
        const enabled = row.querySelector(".cond-enabled").checked;

        const params = {};
        row.querySelectorAll(".cond-param").forEach(inp => {
          const key = inp.dataset.param;
          params[key] = isNaN(inp.value) ? inp.value : Number(inp.value);
        });

        return { type, timeframe: tf, params, enabled };
      });

      // Map section → hidden field id
      const fieldMap = { entry: "entry", gate: "gate", exit: "exit", stoploss: "stoploss" };
      const fieldId  = `leg_${code}_${fieldMap[section]}_json`;
      const hidden   = document.getElementById(fieldId);
      if (hidden) hidden.value = JSON.stringify(conditions);
    });
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// DETAIL PAGE — Equity curve + trade/signal logs
// ══════════════════════════════════════════════════════════════════════════════

// ── Equity curve ────────────────────────────────────────────────────────────

async function loadEquityChart() {
  const canvas = document.getElementById("equityChart");
  if (!canvas) return;

  let data;
  try {
    const res = await fetch(`/labs/api/${BOT_ID}/equity`);
    data = await res.json();
  } catch (e) {
    return;
  }

  if (!data.length) {
    canvas.parentElement.innerHTML += '<p style="color:#64748b;font-size:13px">No trades yet.</p>';
    canvas.style.display = "none";
    return;
  }

  const labels = data.map(d => d.date);
  const values = data.map(d => d.cumulative_pnl_rs);
  const lastVal = values[values.length - 1];
  const lineColor = lastVal >= 0 ? "#4ade80" : "#f87171";

  new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Cumulative P&L (₹)",
        data: values,
        borderColor: lineColor,
        backgroundColor: lineColor + "22",
        fill: true,
        tension: 0.3,
        pointRadius: data.length > 60 ? 0 : 3,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `₹${ctx.parsed.y.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`,
          },
        },
      },
      scales: {
        x: {
          ticks: { color: "#64748b", maxTicksLimit: 12 },
          grid:  { color: "#1e2235" },
        },
        y: {
          ticks: {
            color: "#64748b",
            callback: v => "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 }),
          },
          grid: { color: "#1e2235" },
        },
      },
    },
  });
}

// ── Trade log ────────────────────────────────────────────────────────────────

async function loadTradeLog() {
  const tbody = document.getElementById("tradeBody");
  if (!tbody) return;

  let trades;
  try {
    const res = await fetch(`/labs/api/${BOT_ID}/trades`);
    trades = await res.json();
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="11" style="color:#f87171">Failed to load.</td></tr>';
    return;
  }

  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="11" style="color:#64748b">No trades yet.</td></tr>';
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const pnlPts = parseFloat(t.pnl_pts);
    const pnlRs  = parseFloat(t.pnl_rs);
    const cls    = pnlRs > 0 ? "green" : (pnlRs < 0 ? "red" : "");
    return `
      <tr>
        <td>${t.trade_date}</td>
        <td>${t.side}</td>
        <td style="font-size:11px">${t.symbol}</td>
        <td>${t.entry_time.slice(11,16)}</td>
        <td>${t.exit_time.slice(11,16)}</td>
        <td>${t.entry_ltp.toFixed(2)}</td>
        <td>${t.exit_ltp.toFixed(2)}</td>
        <td class="${cls}">${pnlPts > 0 ? "+" : ""}${pnlPts.toFixed(1)}</td>
        <td class="${cls}">₹${pnlRs > 0 ? "+" : ""}${pnlRs.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</td>
        <td>${t.exit_reason}</td>
        <td>${t.holding_mins}</td>
      </tr>`;
  }).join("");
}

// ── Signal log ───────────────────────────────────────────────────────────────

async function loadSignalLog() {
  const tbody = document.getElementById("signalBody");
  if (!tbody) return;

  let signals;
  try {
    const res = await fetch(`/labs/api/${BOT_ID}/signals`);
    signals = await res.json();
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:#f87171">Failed to load.</td></tr>';
    return;
  }

  if (!signals.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:#64748b">No signals yet.</td></tr>';
    return;
  }

  tbody.innerHTML = signals.map(s => {
    const actedLabel = s.acted ? '<span class="badge badge-active">Yes</span>' : '<span style="color:#64748b">No</span>';
    const typeColor  = s.signal_type === "CE" ? "green" : (s.signal_type === "PE" ? "red" : "");
    return `
      <tr>
        <td>${s.ts.slice(0, 16)}</td>
        <td class="${typeColor}">${s.signal_type}</td>
        <td>${parseFloat(s.bar_close).toFixed(0)}</td>
        <td>${s.rsi !== null ? parseFloat(s.rsi).toFixed(1) : "—"}</td>
        <td>${actedLabel}</td>
        <td style="color:#64748b">${s.skip_reason || "—"}</td>
      </tr>`;
  }).join("");
}
