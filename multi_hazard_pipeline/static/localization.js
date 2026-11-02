(() => {
  const root = document.querySelector('#localization-state');
  if (!root) return;
  const $ = selector => document.querySelector(selector);
  const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
  const label = text => String(text || '').replaceAll('_', ' ');
  const render = state => {
    const info = state.info;
    $('#localization-status').textContent = state.status_label;
    root.className = `panel status state-${info.status || 'none'}`;
    $('#localization-activity').textContent = info.activity || 'Choose a spatial package to begin.';
    $('#localization-counters').textContent = `${state.processed} of ${state.total} segment decisions saved · ${state.model_calls} model calls`;
    const progress = $('#localization-progress');
    progress.max = Math.max(state.total, 1);
    progress.value = Math.min(state.processed, progress.max);
    const stages = $('#localization-stages');
    if (stages && state.provided) stages.replaceChildren(...state.provided.stages.map(stage => {
      const li = node('li', `${stage.label} · ${label(stage.status)}`); li.className = `stage-${stage.status}`; return li;
    }));
    $('#localization-fields').disabled = state.busy || root.dataset.approved !== 'true';
    if (info.status && !state.busy) $('[name="resume"]').checked = true;
    const errors = [info.error, info.review_error,
      ...(info.build?.errors || []), ...(info.validation?.errors || []),
      ...Object.entries(info.processing_failures || {}).map(([id, error]) => `Segment ${id}: ${error}`),
      ...(info.review?.issues || []).map(issue => `Review: ${issue.message || JSON.stringify(issue)}`)
    ].filter(Boolean);
    const warnings = [...(info.build?.warnings || []), ...(info.validation?.warnings || [])];
    $('#localization-issues').replaceChildren(...errors.map(text => { const p = node('p', text); p.className = 'error'; return p; }),
      ...warnings.map(text => { const p = node('p', text); p.className = 'warning'; return p; }));
    $('#localization-log').replaceChildren(...(info.log || []).map(item => node('li', `${new Date(item.time).toLocaleTimeString()} · ${item.message}`)));
    $('#localization-downloads').replaceChildren(...state.downloads.map(file => {
      const a = node('a', file.label); a.href = file.url; a.className = 'button secondary'; return a;
    }));
    $('#localization-output-notice').textContent = state.busy ? 'Processing is in progress. Available files may belong to an earlier build.' :
      state.downloads.length ? (['delivered', 'delivered_with_unresolved'].includes(info.status) ? 'Spatial outputs are available.' : 'Available outputs may be partial. Check the status and diagnostics above.') : 'Spatial outputs will appear here after the first build.';
    $('#localization-results').replaceChildren(...state.results.map(row => {
      const tr = document.createElement('tr');
      ['seg_id', 'localization_status', 'precision', 'event_confidence', 'location_confidence', 'boundary_confidence', 'reason'].forEach(key => tr.append(node('td', label(row[key] ?? '—'))));
      return tr;
    }));
    const map = $('#localization-map'), link = $('#localization-map-link');
    map.hidden = link.hidden = !state.map_url;
    if (state.map_url) {
      link.href = state.map_url;
      // Reload after a new build while preserving the map during ordinary status polling.
      const version = info.finished_at || info.started_at || '';
      const src = `${state.map_url}?v=${encodeURIComponent(version)}`;
      if (map.getAttribute('src') !== src) map.src = src;
    }
  };
  render(JSON.parse($('#localization-data').textContent));
  async function poll() {
    try {
      const response = await fetch(root.dataset.statusUrl, {cache: 'no-store', signal: AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      render(await response.json());
      $('#localization-connection').hidden = true;
    } catch (error) {
      $('#localization-connection').textContent = 'Connection interrupted. Retrying localization status…';
      $('#localization-connection').hidden = false;
    } finally { window.setTimeout(poll, 2500); }
  }
  window.setTimeout(poll, 2500);
  $('#localization-form').addEventListener('submit', () => {
    // Keep successful form controls enabled until the browser serializes the request.
    $('#localization-form button[type="submit"]').disabled = true;
  });
})();
