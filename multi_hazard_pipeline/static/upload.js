const form = document.querySelector('#upload-form');
form.addEventListener('submit', event => {
  const files = [...form.elements.files.files];
  const folder = form.elements.input_dir.value.trim();
  const message = document.querySelector('#upload-message');
  if (Boolean(files.length) === Boolean(folder)) {
    event.preventDefault();
    message.textContent = 'Choose report files or enter a folder, using just one of these options.';
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
