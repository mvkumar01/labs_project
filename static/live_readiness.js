(function () {
  'use strict';
  async function refresh() {
    var summary = document.getElementById('readiness-summary');
    var list = document.getElementById('readiness-checks');
    var card = document.getElementById('order-egress');
    try {
      var response = await fetch('/live/readiness', {headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('Readiness unavailable');
      var data = await response.json();
      summary.textContent = data.ready ? 'Ready for arming. The runner checks again before each order.' : 'Complete the checks below before arming.';
      list.replaceChildren();
      (data.checks || []).forEach(function (check) {
        var item = document.createElement('li');
        item.textContent = (check.passed ? 'PASS: ' : 'ACTION: ') + check.name.replaceAll('_', ' ') + ' - ' + check.detail;
        list.appendChild(item);
      });
      var route = data.egress || {};
      card.textContent = route.label ? route.label + ' | IPs: ' + route.expected_ips.join(', ') + ' | Observed: ' + route.observed_ip + ' | Today: ' + route.usage.day + '/' + route.daily_quota + ' | Month: ' + route.usage.month + '/' + route.monthly_quota + ' | Exit reserve: ' + route.exit_reserve : route.detail;
    } catch (error) {
      summary.textContent = 'Readiness unavailable. Refresh or sign in again.';
      list.replaceChildren();
      card.textContent = 'Route status unavailable';
    } finally {
      window.setTimeout(refresh, 10000);
    }
  }
  refresh();
}());
