(() => {
  const form = document.querySelector('#provided-form');
  if (!form) return;
  const groups = {package: form.querySelector('#spatial-package'), files: form.querySelector('#spatial-files')};
  const sync = () => {
    const chosen = form.querySelector('[name="spatial_source"]:checked')?.value || 'package';
    // A disabled fieldset is neither validated nor submitted.
    Object.entries(groups).forEach(([name, group]) => { group.hidden = group.disabled = name !== chosen; });
  };
  form.querySelectorAll('[name="spatial_source"]').forEach(input => input.addEventListener('change', sync));
  sync();
  form.addEventListener('submit', event => {
    const message = form.querySelector('#provided-message');
    const report = form.querySelector('[name="report_files"]').files.length || form.querySelector('[name="report_path"]').value.trim();
    const table = form.querySelector('[name="segments_file"]').files.length || form.querySelector('[name="segments_path"]').value.trim();
    if (!report || !table) {
      event.preventDefault();
      message.className = 'message error';
      message.textContent = !report ? 'Choose the report files or enter a report path.' : 'Choose the segment table or enter its path.';
      return;
    }
    message.className = 'message';
    message.textContent = 'Checking the inputs and queuing the localization test…';
    form.querySelector('button[type="submit"]').disabled = true;
  });
})();
