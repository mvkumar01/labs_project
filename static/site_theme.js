/* Presentation only: label asynchronously populated tables for mobile reflow. */
(() => {
  let scheduled = false;
  function labelTables() {
    scheduled = false;
    document.querySelectorAll('table:not(.report-table)').forEach(table => {
      const headers = [...table.querySelectorAll('thead tr:last-child th')].map(th => th.textContent.trim());
      if (!headers.length) return;
      table.classList.add('site-reflow-table');
      table.querySelectorAll('tbody tr').forEach(row => {
        [...row.cells].forEach((cell, index) => {
          if (cell.colSpan === 1 && headers[index] && cell.dataset.label !== headers[index]) {
            cell.dataset.label = headers[index];
          }
        });
      });
    });
  }
  const observer = new MutationObserver(() => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(labelTables); }
  });
  labelTables();
  observer.observe(document.body, {childList: true, subtree: true});
})();
