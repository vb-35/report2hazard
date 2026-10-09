const dialog = document.querySelector('#delete-dialog');
if (dialog) {
  const form = document.querySelector('#delete-form');
  document.querySelectorAll('.delete-run').forEach(button => button.addEventListener('click', () => {
    const reports = Number(button.dataset.reports);
    form.action = button.dataset.url;
    document.querySelector('#delete-title').textContent = button.dataset.title;
    document.querySelector('#delete-folder').textContent = button.dataset.folder;
    document.querySelector('#delete-reports').textContent = reports
      ? `This is a multi-report collection: the ${reports} report(s) separated from it, their results and reviews are deleted as well.`
      : '';
    dialog.showModal();
  }));
  document.querySelector('#cancel-delete').addEventListener('click', () => dialog.close());
  form.addEventListener('submit', () => { form.querySelector('[type=submit]').disabled = true; });
}
