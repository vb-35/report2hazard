const form = document.querySelector('#upload-form');
form.addEventListener('submit', event => {
  const files = [...form.elements.files.files];
  const folder = form.elements.input_dir.value.trim();
  const mode = form.elements.report_mode.value;
  const message = document.querySelector('#upload-message');
  if (!mode) {
    event.preventDefault();
    message.textContent = 'Choose whether this is a single report or a multi-report collection.';
    return;
  }
  if (Boolean(files.length) === Boolean(folder)) {
    event.preventDefault();
    message.textContent = 'Choose report files or enter a folder, using just one of these options.';
    return;
  }
  if (mode === 'multi' && files.length && (files.length !== 1 || !files[0].name.toLowerCase().endsWith('.pdf'))) {
    event.preventDefault();
    message.textContent = 'A multi-report collection must be exactly one PDF file.';
    return;
  }
  if (files.reduce((total, file) => total + file.size, 0) >= 100 * 1024 * 1024) {
    event.preventDefault();
    message.textContent = 'Please choose files totaling less than 100 MB.';
    return;
  }
  form.querySelector('button').disabled = true;
  message.textContent = 'Uploading your report… You will be taken to its progress page.';
});
