const importForm = document.querySelector('#import-form');
const zipInput = importForm.elements.outputs;
const zipSelection = document.querySelector('#selected-zip');
const zipPrompt = document.querySelector('#zip-prompt');
const importMessage = document.querySelector('#import-message');

zipInput.addEventListener('change', () => {
  const file = zipInput.files[0];
  zipSelection.textContent = file ? `Selected: ${file.name}` : 'No ZIP selected';
  zipPrompt.textContent = file ? 'Change the zip' : 'Choose the zip';
  importMessage.textContent = '';
});

importForm.addEventListener('submit', () => {
  const submit = importForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.textContent = 'Importing…';
  importMessage.textContent = 'Uploading and importing your ZIP. Please keep this page open.';
});
