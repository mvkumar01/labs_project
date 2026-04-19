/* Labs frontend — Chart.js equity curve + trade/signal log loaders */

document.addEventListener("DOMContentLoaded", () => {
  // Only run detail-page logic when BOT_ID is defined
  if (typeof BOT_ID === "undefined") return;

  loadEquityChart();
  loadTradeLog();
  loadSignalLog();
});

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
