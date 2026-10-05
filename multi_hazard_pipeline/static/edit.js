document.querySelectorAll('.candidate-edit').forEach(form => {
  const row = JSON.parse(form.dataset.row);
  const labels = JSON.parse(form.dataset.labels);
  const field = form.elements.field;
  const input = form.querySelector('input[name="new_value"]');
  const select = form.querySelector('select[name="new_value"]');
  const update = () => {
    const options = labels[field.value];
    input.hidden = input.disabled = Boolean(options);
    select.hidden = select.disabled = !options;
    if (options) {
      select.replaceChildren(...options.map(value => new Option(value, value)));
      select.value = row[field.value];
    } else {
      input.type = field.value === 'causal_order' ? 'number' : 'text';
      input.min = '1';
      input.required = field.value !== 'predecessor_segment_ids';
      const value = row[field.value];
      input.value = Array.isArray(value) ? value.join(', ') : value;
    }
  };
  field.addEventListener('change', update);
  update();
});
