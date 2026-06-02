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
        "<td>" + (o.created_at || "").replace("T", " ").slice(0, 19) + "</td>" +
        "<td>" + (o.action || "") + " " + (o.side || "") + "</td>" +
        "<td>" + (o.symbol || "") + "</td>" +
        "<td>" + (o.qty || 0) + "</td>" +
        "<td>" + (o.status || "") + "</td>" +
        "<td>" + tag + "</td>";
      tb.appendChild(tr);
    });
  }

  function refreshStatus() {
    fetch("/live/status", { headers: { "Accept": "application/json" } })
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
          pnl.textContent = fmtRs(s.today_pnl);
          pnl.className = "v " + (Number(s.today_pnl) >= 0 ? "pos" : "neg");
        }
        var cap = document.getElementById("stat-cap");
        if (cap) cap.textContent = "₹" + Number(s.daily_loss_cap || 0).toLocaleString("en-IN");
        var lots = document.getElementById("stat-lots");
        if (lots) lots.textContent = s.lots;

        applyFunds(s);

        renderOrders(s.last_orders);
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
