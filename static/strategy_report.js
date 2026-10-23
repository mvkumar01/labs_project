/* Keep labels available when tables reflow into cards on smaller screens. */
document.querySelectorAll('.strategy-dashboard table').forEach(table => {
  const headers = [...table.querySelectorAll('thead tr:last-child th')].map(th => th.textContent.trim());
  table.querySelectorAll('tbody tr').forEach(row => {
    [...row.cells].forEach((cell, index) => {
      if (!cell.dataset.label && cell.colSpan === 1 && headers[index]) cell.dataset.label = headers[index];
    });
  });
});
/* Read-only excursion estimates load separately so ledger rendering stays fast. */
document.querySelectorAll('.report-excursion-load').forEach(button => {
  button.addEventListener('click', async () => {
    const section = button.closest('.strategy-report');
    const status = section.querySelector('.report-excursion-status');
    const rows = [...section.querySelectorAll('tr[data-trade-id]')];
    button.disabled = true;
    let done = 0;
    try {
      for (let start = 0; start < rows.length; start += 20) {
        status.textContent = `Calculating ${done} / ${rows.length} records…`;
        const batch = rows.slice(start, start + 20);
        const query = new URLSearchParams(window.location.search);
        query.set('panel', button.dataset.panelIndex);
        query.set('ids', batch.map(row => row.dataset.tradeId).join(','));
        const response = await fetch('/labs/api/strategy-report/excursions?' + query);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Price history could not be read.');
        for (const row of batch) {
          const values = payload.trades[row.dataset.tradeId];
          if (!values) continue;
          row.querySelectorAll('[data-excursion]').forEach(cell => {
            const value = values[cell.dataset.excursion];
            cell.textContent = value == null ? 'Unavailable' : new Intl.NumberFormat('en-IN', {
              style: 'currency', currency: 'INR', maximumFractionDigits: 0
            }).format(value);
            cell.title = values.note;
          });
        }
        done += batch.length;
      }
      status.textContent = 'Complete. Hover each value for price coverage and definitions.';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
});
