// Show the selected provider's default model as the placeholder of the model field.
document.querySelectorAll('.model-options').forEach(group => {
  const models = JSON.parse(group.dataset.models || '{}');
  const select = group.querySelector('.provider-select');
  const input = group.querySelector('.model-input');
  if (!select || !input) return;
  select.addEventListener('change', () => { input.placeholder = models[select.value] || ''; });
});
