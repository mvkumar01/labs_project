/* live.js — /live status polling + mode/kill controls.
   Filenames are live.* so they never overwrite labs.*.
   This script only POSTs to config-mutating routes; it never places orders. */
(function () {
  "use strict";

  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  function post(url) {
    return fetch(url, {
      method: "POST",
      headers: { "X-CSRF-Token": CSRF, "Accept": "application/json" },
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
    });
  }

  function fmtRs(v) {
    var n = Number(v || 0);
    return (n >= 0 ? "+" : "-") + "₹" + Math.abs(n).toLocaleString("en-IN");
  }

  function fmtMoney(v) {
    if (v === null || v === undefined || v === "") return "--";
    return "\u20b9" + Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  function fmtPrice(v) {
    if (v === null || v === undefined || v === "") return "--";
    return Number(v || 0).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function fmtTime(v) {
    if (!v) return "";
    var text = String(v).trim();
    var iso = text.indexOf("T") >= 0 ? text : text.replace(" ", "T");
    if (!/[zZ]$/.test(iso) && !/[+-]\d\d:\d\d$/.test(iso)) iso += "Z";
    var d = new Date(iso);
    if (Number.isNaN(d.getTime())) return text.replace("T", " ").slice(0, 19);
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).formatToParts(d).reduce(function (acc, part) {
      acc[part.type] = part.value;
      return acc;
    }, {});
    return parts.year + "-" + parts.month + "-" + parts.day + " " +
      parts.hour + ":" + parts.minute + ":" + parts.second + " IST";
  }

  function addCell(row, text, className) {
    var td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    row.appendChild(td);
  }

  function setText(id, value, className) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    if (className !== undefined) el.className = className;
  }

  function selectedDateRange() {
    var from = document.getElementById("trade-date-from");
    var to = document.getElementById("trade-date-to");
    return {
      from: from && from.value ? from.value : "",
      to: to && to.value ? to.value : ""
    };
  }

  function statusUrl() {
    var r = selectedDateRange();
    var params = [];
    if (r.from) params.push("date_from=" + encodeURIComponent(r.from));
    if (r.to) params.push("date_to=" + encodeURIComponent(r.to));
    return params.length ? "/live/status?" + params.join("&") : "/live/status";
  }

  function applyFunds(s) {
    var funds = document.getElementById("stat-funds");
    if (funds) funds.textContent = fmtMoney(s.funds_available);
    var fundsNote = document.getElementById("stat-funds-note");
    if (fundsNote) {
      fundsNote.textContent = s.funds_error
        ? s.funds_error
        : (s.funds_updated_at
          ? (s.funds_available === null || s.funds_available === undefined ? "funds unavailable" : "refreshed")
          : "");
    }
  }

  function applyConnectionStatus(s) {
    var status = document.getElementById("conn-status");
    if (status && s.connection_status) status.textContent = s.connection_status;
    var relogin = document.getElementById("zerodha-relogin-banner");
    if (relogin) {
      var broker = (s.broker || relogin.getAttribute("data-broker") || "").toLowerCase();
      relogin.style.display = (broker === "zerodha" && s.connection_status !== "connected")
        ? "flex"
        : "none";
    }
  }

  function applyModeBanner(mode) {
    var b = document.getElementById("mode-banner");
    if (!b) return;
    b.className = "mode-banner mode-" + mode;
    var v = b.querySelector(".mode-value");
    if (v) v.textContent = mode;
  }

  function renderOrders(orders) {
    var tb = document.querySelector("#orders-body");
    if (!tb) return;
    tb.innerHTML = "";
    if (!orders || !orders.length) {
      tb.innerHTML = '<tr><td colspan="6" style="color:#64748b">No orders yet.</td></tr>';
      return;
    }
    orders.forEach(function (o) {
      var tr = document.createElement("tr");
      var tag = o.dry_run
        ? '<span class="tag-dry">DRY</span>'
        : '<span class="tag-live">LIVE</span>';
      tr.innerHTML =
        "<td>" + fmtTime(o.created_at) + "</td>" +
        "<td>" + (o.action || "") + " " + (o.side || "") + "</td>" +
        "<td>" + (o.symbol || "") + "</td>" +
        "<td>" + (o.qty || 0) + "</td>" +
        "<td>" + (o.status || "") + "</td>" +
        "<td>" + tag + "</td>";
      tb.appendChild(tr);
    });
  }

  function renderTrades(trades) {
    var tb = document.querySelector("#trades-body");
    if (!tb) return;
    tb.innerHTML = "";
    if (!trades || !trades.length) {
      tb.innerHTML = '<tr><td colspan="11" style="color:#64748b">No completed trades for this date.</td></tr>';
      return;
    }
    trades.forEach(function (t) {
      var tr = document.createElement("tr");
      addCell(tr, fmtTime(t.exit_time || t.entry_time));
      addCell(tr, t.side || "");
      addCell(tr, t.symbol || "");
      addCell(tr, String(t.qty || 0));
      addCell(tr, fmtPrice(t.entry_price));
      addCell(tr, fmtPrice(t.exit_price));
      addCell(tr, t.gross_pnl === null || t.gross_pnl === undefined ? "--" : fmtRs(t.gross_pnl),
        Number(t.gross_pnl || 0) >= 0 ? "pos" : "neg");
      addCell(tr, t.charges_total === null || t.charges_total === undefined ? "--" : fmtMoney(t.charges_total));
      addCell(tr, fmtRs(t.pnl), Number(t.pnl || 0) >= 0 ? "pos" : "neg");
      addCell(tr, t.reason || "");

      var td = document.createElement("td");
      td.innerHTML = t.dry_run
        ? '<span class="tag-dry">DRY</span>'
        : '<span class="tag-live">LIVE</span>';
      tr.appendChild(td);
      tb.appendChild(tr);
    });
  }

  function applyTradeHistory(s) {
    var fromInput = document.getElementById("trade-date-from");
    if (fromInput && s.date_from && !fromInput.value) fromInput.value = s.date_from;
    var toInput = document.getElementById("trade-date-to");
    if (toInput && s.date_to && !toInput.value) toInput.value = s.date_to;

    var pnl = document.getElementById("trade-pnl");
    if (pnl) {
      pnl.textContent = fmtRs(s.live_pnl);
      pnl.className = "v " + (Number(s.live_pnl || 0) >= 0 ? "pos" : "neg");
    }
    var count = document.getElementById("trade-count");
    if (count) {
      var n = Number(s.live_count || 0);
      count.textContent = n.toLocaleString("en-IN") + (n === 1 ? " trade" : " trades");
    }
    var pnlDry = document.getElementById("trade-pnl-dry");
    if (pnlDry) {
      pnlDry.textContent = fmtRs(s.dry_pnl);
      pnlDry.className = "v " + (Number(s.dry_pnl || 0) >= 0 ? "pos" : "neg");
    }
    var countDry = document.getElementById("trade-count-dry");
    if (countDry) {
      var nd = Number(s.dry_count || 0);
      countDry.textContent = nd.toLocaleString("en-IN") + (nd === 1 ? " trade" : " trades");
    }
    renderTrades(s.trades);
  }

  function applyOpenMtm(s) {
    var mtm = s.open_mtm || {};
    if (!document.getElementById("open-mtm-card")) return;
    if (!mtm.open) {
      setText("open-mtm-sub", "No open position.");
      setText("open-mtm-net", "--", "v");
      setText("open-mtm-updated", "");
      setText("open-mtm-symbol", "--");
      setText("open-mtm-qty", "--");
      setText("open-mtm-entry", "--");
      setText("open-mtm-ltp", "--");
      setText("open-mtm-gross", "--");
      setText("open-mtm-charges", "--");
      return;
    }

    var mode = mtm.dry_run ? "DRY" : "LIVE";
    setText("open-mtm-sub", mode + " " + (mtm.side || "") + " opened " + fmtTime(mtm.entry_time));
    setText("open-mtm-symbol", mtm.symbol || "--");
    setText("open-mtm-qty", String(mtm.qty || 0));
    setText("open-mtm-entry", fmtPrice(mtm.entry_price));

    if (!mtm.ltp_available) {
      setText("open-mtm-net", "--", "v");
      setText("open-mtm-updated", mtm.error ? "LTP unavailable: " + mtm.error : "LTP unavailable");
      setText("open-mtm-ltp", "--");
      setText("open-mtm-gross", "--");
      setText("open-mtm-charges", "--");
      return;
    }

    setText("open-mtm-ltp", fmtPrice(mtm.latest_price));
    setText("open-mtm-gross", fmtRs(mtm.gross_pnl));
    setText("open-mtm-charges", fmtMoney(mtm.charges_total));
    setText("open-mtm-net", fmtRs(mtm.net_pnl),
      "v " + (Number(mtm.net_pnl || 0) >= 0 ? "pos" : "neg"));
    setText("open-mtm-updated", mtm.latest_time ? "updated " + fmtTime(mtm.latest_time) : "");
  }

  function refreshStatus() {
    fetch(statusUrl(), { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        applyModeBanner(s.mode);
        applyConnectionStatus(s);

        var kill = document.getElementById("kill-banner");
        if (kill) kill.style.display = s.kill_switch ? "block" : "none";

        var rec = document.getElementById("reconcile-banner");
        if (rec) {
          if (!s.reconcile_ok) {
            rec.style.display = "block";
            rec.textContent = "Reconciliation mismatch: " + (s.reconcile_warning || "new entries blocked");
          } else {
            rec.style.display = "none";
          }
        }

        var pnl = document.getElementById("stat-pnl");
        if (pnl) {
          pnl.textContent = fmtRs(s.today_pnl_live !== undefined ? s.today_pnl_live : s.today_pnl);
          pnl.className = "v " + (Number(s.today_pnl_live !== undefined ? s.today_pnl_live : s.today_pnl) >= 0 ? "pos" : "neg");
          var pnlDryNote = document.getElementById("stat-pnl-dry");
          if (pnlDryNote) pnlDryNote.textContent = "DRY " + fmtRs(s.today_pnl_dry || 0);
        }
        var cap = document.getElementById("stat-cap");
        if (cap) cap.textContent = "₹" + Number(s.daily_loss_cap || 0).toLocaleString("en-IN");
        var lots = document.getElementById("stat-lots");
        if (lots) lots.textContent = s.lots;

        applyFunds(s);

        renderOrders(s.last_orders);
        applyTradeHistory(s);
        applyOpenMtm(s);
      })
      .catch(function () { /* transient; next poll retries */ });
  }

  function wireButton(id, url, confirmMsg, afterReloadOnFail) {
    var el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", function () {
      if (confirmMsg && !window.confirm(confirmMsg)) return;
      el.disabled = true;
      post(url).then(function (res) {
        if (!res.ok && res.body && res.body.failed_gates) {
          var brokerGateFailedEarly = res.body.failed_gates.some(function (g) {
            return g.name === "broker_connected";
          });
          if (res.body.broker === "zerodha" && brokerGateFailedEarly && res.body.relogin_url) {
            if (window.confirm("Zerodha session is not connected. Re-login to Kite now?")) {
              window.location.href = res.body.relogin_url;
              return;
            }
          }
          var lines = res.body.failed_gates.map(function (g) {
            return "✖ " + g.name + ": " + g.detail;
          }).join("\n");
          window.alert("Cannot arm LIVE — gates failed:\n\n" + lines);
        } else if (!res.ok && res.body && res.body.error) {
          window.alert("Failed: " + res.body.error);
        }
        refreshStatus();
      }).finally(function () { el.disabled = false; });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireButton("btn-arm-dry", "/live/arm_dry_run");
    wireButton("btn-arm-live", "/live/arm",
      "Arm LIVE mode? Real orders become permitted (still gated). Continue?");
    wireButton("btn-disarm", "/live/disarm", "Disarm and stop all live activity?");
    wireButton("btn-kill", "/live/kill", "Trigger KILL SWITCH? Halts all new activity immediately.");
    wireButton("btn-resume", "/live/resume", "Clear the kill switch?");

    var refreshFunds = document.getElementById("btn-refresh-funds");
    if (refreshFunds) {
      refreshFunds.addEventListener("click", function () {
        refreshFunds.disabled = true;
        post("/live/refresh_funds").then(function (res) {
          if (res.body) {
            applyFunds(res.body);
            applyConnectionStatus(res.body);
          }
          if (!res.ok && res.body && res.body.error) {
            window.alert("Funds refresh failed: " + res.body.error);
          }
        }).finally(function () { refreshFunds.disabled = false; });
      });
    }

    var tradeFrom = document.getElementById("trade-date-from");
    if (tradeFrom) tradeFrom.addEventListener("change", refreshStatus);
    var tradeTo = document.getElementById("trade-date-to");
    if (tradeTo) tradeTo.addEventListener("change", refreshStatus);

    // Broker selector highlight (connect page).
    var radios = document.querySelectorAll('input[name="broker"]');
    radios.forEach(function (r) {
      r.addEventListener("change", function () {
        document.querySelectorAll(".broker-option").forEach(function (o) {
          o.classList.remove("selected");
        });
        var card = r.closest(".broker-option");
        if (card) card.classList.add("selected");
      });
    });

    if (document.getElementById("mode-banner")) {
      refreshStatus();
      setInterval(refreshStatus, 4000);
    }
  });
})();
