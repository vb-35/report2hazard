(() => {
  const root = document.querySelector('#collection');
  if (!root) return;
  const $ = selector => document.querySelector(selector);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const labels = {queued: 'Queued', running: 'Running', awaiting_human_review: 'Awaiting human review', revision_required: 'Needs automatic revision', approved: 'Approved', failed: 'Failed', rejected: 'Rejected'};
  const processing = status => status === 'running' || status === 'queued';
  function render(data) {
    $('#collection-status').className = `panel status state-${data.status}`;
    $('#status-name').textContent = data.status_label;
    $('#activity').textContent = data.activity;
    $('#progress').value = data.progress;
    $('#progress-text').textContent = `${data.progress}%`;
    $('#stage-states').replaceChildren(...data.stages.map(stage => node('li', `${stage.label} · ${stage.status}`, `stage-${stage.status === 'awaiting' ? 'running' : stage.status}`)));
    const diagnostics = [...data.warnings.map(item => `Warning: ${item.message || item}`), ...data.errors.map(item => `Pipeline failure${item.stage ? ' / ' + item.stage : ''}: ${item.message}`)];
    $('#diagnostic-count').textContent = `(${diagnostics.length})`;
    $('#diagnostic-items').replaceChildren(...diagnostics.map(text => node('p', text)));
    if (data.status === 'failed') $('#diagnostics').open = true;
    $('#report-table tbody').replaceChildren(...data.reports.map(report => {
      const tr = node('tr', null, `state-${report.status}`);
      const link = node('a', report.filename); link.href = report.url;
      const name = node('td'); name.append(link);
      tr.append(node('td', report.index), name, node('td', labels[report.status] || report.status),
        node('td', (report.current_stage || '').replaceAll('_', ' ')), node('td', `${report.progress}%`));
      return tr;
    }));
    $('#reports-empty').hidden = data.reports.length > 0;
    const next = data.reports.find(report => report.status === 'awaiting_human_review' || report.status === 'revision_required');
    $('#review-next').hidden = !next;
    if (next) $('#review-next').href = next.url;
    return processing(data.status) || data.reports.some(report => processing(report.status));
  }
  async function poll() {
    let active = true;
    try {
      const response = await fetch(root.dataset.url, {headers: {Accept: 'application/json'}, cache: 'no-store', signal: AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      active = render(await response.json());
      $('#connection-status').hidden = true;
    } catch (error) {
      $('#connection-status').hidden = false;
      $('#connection-status').textContent = `Connection problem (${error.message}). Displaying last received status; retrying.`;
    }
    // Keep polling slowly after processing ends so reviews done in other tabs still show up.
    setTimeout(poll, active ? 1500 : 5000);
  }
  render(JSON.parse($('#collection-data').textContent));
  setTimeout(poll, 1500);
})();
