/* Labs OI calibration charts. Inputs are persisted candidate artifacts. */
document.addEventListener("DOMContentLoaded", () => {
  const payload = window.CALIBRATION_DATA;
  if (!payload || typeof Chart === "undefined") return;
  const current = payload.metrics.current.trends || [];
  const candidate = payload.metrics.candidate.trends || [];
  const charts = [];
  const colors = { current: "#818cf8", candidate: "#4ade80", adverse: "#f87171" };

  function filtered() {
    const from = document.getElementById("calChartFrom")?.value || "";
    const to = document.getElementById("calChartTo")?.value || "9999-12-31";
    return {
      current: current.filter(row => row.date >= from && row.date <= to),
      candidate: candidate.filter(row => row.date >= from && row.date <= to),
    };
  }
  function series(rows, field) { return rows.map(row => row[field]); }
  function lineChart(id, title, fields) {
    const canvas = document.getElementById(id); if (!canvas) return;
    const rows = filtered();
    const labels = rows.candidate.map(row => row.date);
    const datasets = fields.map((field, index) => ({
      label: field.label,
      data: series(field.source === "current" ? rows.current : rows.candidate, field.key),
      borderColor: field.color || (field.source === "current" ? colors.current : colors.candidate),
      backgroundColor: "transparent", tension: 0.25, pointRadius: 2, spanGaps: true,
    }));
    const chart = new Chart(canvas, {type: "line", data: {labels, datasets}, options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {title: {display: true, text: title, color: "#e2e8f0"}, legend: {labels: {color: "#cbd5e1"}}, zoom: {zoom: {wheel: {enabled: true}, pinch: {enabled: true}, mode: "x"}, pan: {enabled: true, mode: "x"}}},
      scales: {x: {ticks: {color: "#94a3b8", maxRotation: 45}}, y: {ticks: {color: "#94a3b8"}, grid: {color: "#2d3148"}}},
    }});
    chart.$calFields = fields;
    charts.push(chart);
  }
  lineChart("calAccuracyChart", "Next Prediction Accuracy over time", [
    {label: "Current", key: "next_accuracy", source: "current"}, {label: "Candidate", key: "next_accuracy", source: "candidate"},
  ]);
  lineChart("calCoverageChart", "Coverage", [{label: "Current", key: "coverage", source: "current"}, {label: "Candidate", key: "coverage", source: "candidate"}]);
  lineChart("calFlipChart", "Flip Frequency", [{label: "Current", key: "flip_pct", source: "current"}, {label: "Candidate", key: "flip_pct", source: "candidate"}]);
  lineChart("calRatioChart", "Bull / Bear ratio", [{label: "Current", key: "bull_bear_ratio", source: "current"}, {label: "Candidate", key: "bull_bear_ratio", source: "candidate"}]);
  lineChart("calFlatChart", "Flat %", [{label: "Current", key: "flat_pct", source: "current"}, {label: "Candidate", key: "flat_pct", source: "candidate"}]);
  lineChart("calExcursionChart", "Candidate MFE / MAE", [{label: "Average MFE", key: "average_mfe", source: "candidate"}, {label: "Average MAE", key: "average_mae", source: "candidate", color: colors.adverse}]);

  const comparisonCanvas = document.getElementById("calComparisonChart");
  if (comparisonCanvas) {
    const rows = (payload.comparison || []).filter(row => ["next_prediction_accuracy_pct", "balanced_accuracy_pct", "coverage_pct", "average_mfe", "average_mae"].includes(row.metric));
    charts.push(new Chart(comparisonCanvas, {type: "bar", data: {labels: rows.map(row => row.metric.replaceAll("_", " ")), datasets: [
      {label: "Current", data: rows.map(row => row.current), backgroundColor: colors.current},
      {label: "Candidate", data: rows.map(row => row.candidate), backgroundColor: colors.candidate},
    ]}, options: {responsive: true, maintainAspectRatio: false, plugins: {title: {display: true, text: "Candidate vs Current", color: "#e2e8f0"}, legend: {labels: {color: "#cbd5e1"}}}, scales: {x: {ticks: {color: "#94a3b8"}}, y: {ticks: {color: "#94a3b8"}, grid: {color: "#2d3148"}}}}}));
  }
  document.getElementById("calResetZoom")?.addEventListener("click", () => charts.forEach(chart => chart.resetZoom?.()));
  function applyDateRange() {
    const rows = filtered();
    for (const chart of charts) {
      if (!chart.$calFields) continue;
      chart.data.labels = rows.candidate.map(row => row.date);
      chart.$calFields.forEach((field, index) => {
        chart.data.datasets[index].data = series(field.source === "current" ? rows.current : rows.candidate, field.key);
      });
      chart.update();
    }
  }
  for (const id of ["calChartFrom", "calChartTo"]) document.getElementById(id)?.addEventListener("change", applyDateRange);
});
