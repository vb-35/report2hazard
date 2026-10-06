// The editor owns its draft. Polling can change availability, never its input values.
window.candidateEditor = (() => {
  const form = document.querySelector('.candidate-edit');
  if (!form) return null;
  const labels = JSON.parse(document.querySelector('#workspace').dataset.labels);
  let row = null, dirty = false, submittedRevision = null;
  const field = form.elements.field;
  const input = form.querySelector('input[name="new_value"]');
  const select = form.querySelector('select[name="new_value"]');
  const update = () => {
    const options = labels[field.value];
    input.hidden = input.disabled = Boolean(options);
    select.hidden = select.disabled = !options;
    if (options) {
      select.replaceChildren(...options.map(value => new Option(value, value)));
      select.value = row?.[field.value] || '';
    } else {
      input.type = field.value === 'causal_order' ? 'number' : 'text';
      input.min = '1';
      input.required = field.value !== 'predecessor_segment_ids';
      const value = row?.[field.value];
      input.value = Array.isArray(value) ? value.join(', ') : value ?? '';
    }
  };
  form.addEventListener('input', () => { dirty = true; });
  field.addEventListener('change', () => { dirty = true; update(); });
  document.querySelector('#discard-edit').addEventListener('click', () => {
    dirty = false; submittedRevision = null;
    window.dispatchEvent(new Event('editor-reset'));
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  return {
    form,
    bind(next, revision, available) {
      const notice = document.querySelector('#edit-notice');
      if (!dirty) {
        row = next; submittedRevision = revision;
        form.elements.segment.value = next?.segment ?? '';
        update();
      }
      const stale = dirty && (row?.segment !== next?.segment || submittedRevision !== revision);
      notice.textContent = stale ? `Unsaved edit for segment ${row?.segment} retained. Discard it to edit this selection or revision.` : dirty ? 'Unsaved edit retained.' : '';
      form.querySelector('button[type="submit"]').disabled = !available || !next || stale;
    },
    saved() { dirty = false; },
  };
})();
