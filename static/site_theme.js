/* Presentation only: label asynchronously populated tables for mobile reflow. */
(() => {
  document.querySelectorAll('[data-margin-refresh]').forEach(button => {
    button.addEventListener('click', async () => {
      const status = document.querySelector('[data-margin-status]');
      button.disabled = true;
      if (status) status.textContent = ' Fetching standalone broker margin calculations…';
      try {
        const response = await fetch('/labs/api/strategy-report/margins/refresh', {method:'POST'});
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Some margin quotes are unavailable. Stored estimates are retained.');
        location.reload();
      } catch (error) {
        if (status) status.textContent = ' ' + error.message;
        button.disabled = false;
      }
    });
  });
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
