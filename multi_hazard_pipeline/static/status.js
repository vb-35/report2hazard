(() => {
  const panel = document.querySelector("#run-status[data-status-url]");
  if (!panel) return;
  const initialStatus = panel.dataset.status;
  const poll = async () => {
    try {
      const response = await fetch(panel.dataset.statusUrl, {headers: {Accept: "application/json"}});
      if (!response.ok) return;
      const data = await response.json();
      document.querySelector("#current-stage").textContent = (data.current_stage || '').replaceAll('_', ' ');
      document.querySelector("#progress").value = data.progress || 0;
      document.querySelector("#progress-text").textContent = `${data.progress || 0}%`;
      document.querySelector("#correction-round").textContent = `${data.correction_rounds || 0} / ${data.max_correction_rounds}`;
      if (data.status !== initialStatus || data.errors.length) location.reload();
    } catch (_) {
      // A later poll retries transient local-server failures.
    }
  };
  setInterval(poll, 1500);
})();
