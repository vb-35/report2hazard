// New agent run: the effort list and model hint follow the chosen agent.
const agentForm = document.querySelector('#agent-form');
if (agentForm) {
  const effort = agentForm.querySelector('#agent-effort');
  const model = agentForm.querySelector('#agent-model');
  const update = radio => {
    const chosen = effort.value;
    const levels = JSON.parse(radio.dataset.efforts || '[]');
    effort.replaceChildren(new Option('CLI default', ''), ...levels.map(level => new Option(level, level, false, level === chosen)));
    model.placeholder = radio.dataset.modelHint || '';
  };
  agentForm.querySelectorAll('input[name="runner"]').forEach(radio => radio.addEventListener('change', () => update(radio)));
  agentForm.addEventListener('submit', event => {
    const message = agentForm.querySelector('#agent-message');
    if (!agentForm.querySelector('input[name="runner"]:checked')) {
      event.preventDefault();
      message.classList.add('error');
      message.textContent = 'Choose Claude Code or Codex (install and sign in to one of them first).';
      return;
    }
    message.classList.remove('error');
    message.textContent = 'Preparing the run…';
  });
}

// Agent run page: follow the run until it finishes.
const state = document.querySelector('#agent-state');
if (state) {
  const url = state.dataset.statusUrl;
  const busy = new Set(['queued', 'running', 'stopping']);
  const links = (list, items, newTab) => list.replaceChildren(...items.map(item => {
    const li = document.createElement('li');
    const a = Object.assign(document.createElement('a'), { href: item.url, textContent: item.label });
    if (newTab) Object.assign(a, { target: '_blank', rel: 'noopener' });
    li.append(a);
    return li;
  }));
  const refresh = async () => {
    let data;
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(response.statusText);
      data = await response.json();
    } catch {
      setTimeout(refresh, 10000);
      return;
    }
    state.className = state.className.replace(/state-\S+/, `state-${data.status}`);
    document.querySelector('#agent-status').textContent = data.status_label;
    document.querySelector('#agent-activity').textContent = data.activity;
    const error = document.querySelector('#agent-error');
    error.hidden = !data.error;
    error.textContent = data.error || '';
    document.querySelector('#agent-log').replaceChildren(...[...data.log].reverse().map(item => {
      const li = document.createElement('li');
      li.append(Object.assign(document.createElement('time'), { textContent: (item.time || '').slice(11, 19) }), ` ${item.message}`);
      return li;
    }));
    links(document.querySelector('#agent-downloads'), data.downloads, false);
    links(document.querySelector('#agent-logs'), data.logs, true);
    if (data.map_url) {
      const frame = document.querySelector('#agent-map');
      if (frame.hidden) { frame.src = data.map_url; frame.hidden = false; }
      const link = document.querySelector('#agent-map-link');
      link.href = data.map_url;
      link.hidden = false;
      document.querySelector('#agent-no-map').hidden = true;
    }
    if (busy.has(data.status)) {
      setTimeout(refresh, 5000);
    } else if (document.querySelector('#stop-agent')) {
      window.location.reload(); // finished: show Compare and remove the stop button
    }
  };
  if (busy.has(state.className.match(/state-(\S+)/)?.[1])) setTimeout(refresh, 3000);
}
