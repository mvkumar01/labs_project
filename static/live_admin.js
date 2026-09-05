document.querySelectorAll('.route-assignment').forEach(function (select) {
  select.addEventListener('change', function () {
    var option = select.selectedOptions[0];
    select.form.elements.revision.value = option ? option.dataset.revision || '0' : '0';
    select.form.elements.confirmed.checked = false;
  });
});
