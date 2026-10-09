(() => {
  const root = document.querySelector('#workspace');
  if (!root) return;
  document.body.classList.add('workspace-page');
  const $ = selector => document.querySelector(selector);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, action, className = 'secondary') => {
    const element = node('button', text, className);
    element.type = 'button'; element.addEventListener('click', action); return element;
  };
  let state = {}, selected = null, citation = null, inFlight = null, submitting = false;
  let commentDraft = false;
  const fields = ['generalized_category', 'interaction_type', 'sediment_transport_phase'];
  const filters = ['#category-filter', '#interaction-filter', '#phase-filter'];
  const rows = () => state.results?.rows || [];
  const issues = () => state.results?.evaluation?.latest_evaluation?.issues || [];
  const rowIssues = id => issues().filter(issue => (issue.segment_ids || []).some(value => String(value) === String(id)));
  const reviewable = () => ['awaiting_human_review', 'revision_required'].includes(state.manifest.status) && !state.results?.previous;
  const selectedRow = () => rows().find(row => String(row.segment) === String(selected));
  const issueList = list => {
    const ul = node('ul');
    list.forEach(issue => ul.append(node('li', `${issue.stage || ''} / ${issue.code || ''}: ${issue.message || ''}`)));
    return ul;
  };
  const stageLabels = {extraction: 'Extraction', translation: 'Translation', segmentation: 'Segmentation', categorization: 'Categorization', candidate_report: 'Candidate report', self_evaluation: 'Self-evaluation', other: 'Other model calls'};
  const count = (value, singular, plural = `${singular}s`) => `${value} ${value === 1 ? singular : plural}`;
  const duration = seconds => {
    if (seconds == null) return '—';
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ${String(Math.floor(seconds % 60)).padStart(2, '0')} s`;
    return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`;
  };
  // A running step shows its saved time plus the time since it started; tick() keeps it live.
  const runningSeconds = (base, since) => (base || 0) + (since ? Math.max(0, Date.now() - Date.parse(since)) / 1000 : 0);
  const timeNode = (tag, seconds, since) => {
    const element = node(tag, duration(since ? runningSeconds(seconds, since) : seconds));
    if (since) { element.dataset.base = seconds || 0; element.dataset.since = since; }
    return element;
  };
  const tick = () => document.querySelectorAll('[data-since]').forEach(element => {
    element.textContent = duration(runningSeconds(Number(element.dataset.base), element.dataset.since));
  });
  const badges = item => [
    item.retries ? node('span', `↻ ${count(item.retries, 'retry', 'retries')}`, 'stage-badge badge-retry') : null,
    item.timeouts ? node('span', `⏱ ${count(item.timeouts, 'timeout')}`, 'stage-badge badge-timeout') : null,
  ].filter(Boolean);
  const stepSummary = item => [count(item.requests, 'model request'), count(item.retries, 'retry', 'retries'), count(item.timeouts, 'timeout'), count(item.failed_attempts, 'failed attempt')].join(', ');
  const provenance = item => ['filename', 'document_id', 'page', 'paragraph', 'table', 'table_path', 'row', 'cell', 'parent_chunk_id', 'char_start', 'char_end', 'source_type']
    .filter(key => item[key] != null).map(key => `${key.replaceAll('_', ' ')}: ${item[key]}`).join(' · ');
  function renderHeader() {
    const m = state.manifest;
    const labels = {queued: 'Queued', running: 'Running', awaiting_human_review: 'Awaiting human review', revision_required: 'Needs automatic revision', approved: 'Approved', failed: 'Failed', rejected: 'Rejected', split: 'Reports separated'};
    // Runs waiting for the single worker keep status "running" with stage "queued".
    const status = m.status === 'running' && m.current_stage === 'queued' ? 'queued' : m.status;
    $('#status-name').textContent = labels[status] || status;
    $('#current-stage').textContent = (m.current_stage || '').replaceAll('_', ' ');
    $('#correction-round').textContent = `${m.correction_rounds || 0} / ${m.max_correction_rounds ?? 'Unknown'}`;
    const stages = ['preparation', 'extraction', 'translation', 'segmentation', 'categorization', 'candidate_report', 'self_evaluation', 'human_review', 'final_export'];
    const timing = Object.fromEntries((state.statistics?.stages || []).map(item => [item.stage, item]));
    $('#stage-states').replaceChildren(...stages.filter(name => name !== 'preparation' || m.stages?.preparation).map(name => {
      const status = m.stages?.[name] || 'pending';
      const text = status === 'awaiting' ? 'awaiting you' : status;
      const li = node('li', `${name === 'final_export' ? 'Export' : name.replaceAll('_', ' ')} · ${text}`, `stage-${status === 'awaiting' ? 'running' : status}`);
      const stat = timing[name];
      if (stat) {
        li.append(' · ', timeNode('span', stat.elapsed_seconds, stat.running_since), ...badges(stat));
        li.title = `${stageLabels[name]} ran ${count(stat.executions, 'time')}: ${stepSummary(stat)}`;
      }
      return li;
    }));
    const diagnostics = [...(m.warnings || []).map(item => ({...typeof item === 'object' ? item : {}, message: item.message || item, type: 'Warning'})), ...(m.errors || []).map(item => ({...item, type: 'Pipeline failure'}))];
    $('#diagnostic-count').textContent = `(${diagnostics.length})`;
    $('#diagnostic-items').replaceChildren(...diagnostics.map(item => node('p', `${item.type}${item.stage ? ' / ' + item.stage : ''}: ${item.message}`)));
    if (m.status === 'failed') $('#diagnostics').open = true;
    $('#child-runs').replaceChildren(...(m.child_runs || []).map(child => {
      const a = node('a', child.filename); a.href = `/runs/${encodeURIComponent(child.run_id)}`; return a;
    }));
    const remaining = Math.max(0, (m.max_correction_rounds || 0) - (m.correction_rounds || 0));
    const passed = state.results?.evaluation?.latest_evaluation?.status === 'pass';
    $('#review-state').textContent = `${remaining} automatic correction round(s) remaining. ` + (m.status === 'approved' ? 'Approved exports are available.' : m.status === 'rejected' ? 'Rejected. No authoritative exports.' : reviewable() ? passed ? 'Current candidate passed evaluation and is ready for review.' : 'Current candidate must pass re-evaluation before approval.' : 'Review actions become available when processing finishes.');
    const approve = $('[value="approve"]'), reject = $('[value="reject"]');
    approve.textContent = 'Approve report';
    approve.hidden = reject.hidden = m.status !== 'awaiting_human_review';
    approve.disabled = !reviewable() || !passed || submitting;
    reject.disabled = !reviewable() || submitting;
    $('[value="request_correction"]').disabled = !reviewable() || !remaining || submitting;
    document.querySelectorAll('.approved-export').forEach(a => { a.hidden = m.status !== 'approved'; });
    $('#candidate-download').hidden = state.results?.kind !== 'candidate_report.json';
    window.candidateEditor?.bind(selectedRow(), state.results?.revision, reviewable());
  }
  function renderTable() {
    const scroll = $('.table-scroll').scrollTop;
    const focusId = document.activeElement?.closest('tr')?.dataset.segment;
    const search = $('#segment-search').value.toLocaleLowerCase();
    filters.forEach((selector, index) => {
      const select = $(selector), value = select.value;
      const values = [...new Set(rows().map(row => row[fields[index]] || 'Pending'))].sort();
      // Preserve a filter even if its category disappears in a new revision.
      if (value && !values.includes(value)) values.push(value);
      select.replaceChildren(new Option(['All categories', 'All interactions', 'All phases'][index], ''), ...values.map(value => new Option(value, value)));
      select.value = value;
    });
    const visible = rows().filter(row => (!search || [row.segment, row.event, row.process, ...(row.evidence || []).map(e => e.quote)].join(' ').toLocaleLowerCase().includes(search))
      && filters.every((selector, index) => !$(selector).value || $(selector).value === (row[fields[index]] || 'Pending'))
      && (!$('#issues-filter').checked || rowIssues(row.segment).length));
    $('#segment-table tbody').replaceChildren(...visible.map(row => {
      const tr = node('tr'); tr.dataset.segment = row.segment;
      tr.tabIndex = 0; tr.setAttribute('aria-selected', String(String(selected) === String(row.segment)));
      tr.classList.toggle('selected', String(selected) === String(row.segment));
      const select = () => selectSegment(row.segment);
      tr.addEventListener('click', select);
      tr.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); (event.key === 'ArrowDown' ? tr.nextElementSibling : tr.previousElementSibling)?.focus(); }
      });
      const count = rowIssues(row.segment).length;
      [row.causal_order, row.segment, row.event, ...fields.map(field => row[field] || 'Pending'), (row.evidence || []).length, state.results.evaluation ? count ? `${count} issue(s)` : 'None' : 'Pending'].forEach(value => tr.append(node('td', value)));
      return tr;
    }));
    $('#table-empty').hidden = Boolean(visible.length);
    $('#table-empty').textContent = rows().length ? 'No segments match these filters.' : 'Segments will appear after segmentation.';
    $('.table-scroll').scrollTop = scroll;
    if (focusId) [...$('#segment-table tbody').children].find(tr => tr.dataset.segment === focusId)?.focus({preventScroll: true});
    $('#revision-state').textContent = state.results?.previous ? `Previous revision${state.results.revision ? ' ' + state.results.revision : ''} retained while replacements are processed. Classifications and evaluation will update together with their artifacts.` : state.results?.revision ? `Candidate revision ${state.results.revision}` : 'Progressive results · classification fields remain Pending until categorization completes.';
  }
  function renderDetails() {
    const row = selectedRow(), detail = $('#segment-detail'), scroll = $('.detail-scroll').scrollTop;
    detail.replaceChildren();
    $('#selection-notice').textContent = selected != null && !row ? `Selected segment ${selected} no longer exists in the current results. Select another segment.` : '';
    if (!row) { detail.append(node('p', 'Select a segment to inspect its evidence.')); window.candidateEditor?.bind(null, state.results?.revision, false); return; }
    if (!commentDraft) {
      $('#decision-form').elements.segment_ids.value = row.segment;
      $('#decision-form').elements.comment_segment.value = row.segment;
    }
    detail.append(node('h3', `Segment ${row.segment} · Causal order ${row.causal_order}`), node('h4', row.event), node('p', row.process));
    const predecessors = node('div', 'Predecessors: ');
    (row.predecessor_segment_ids || []).forEach(id => predecessors.append(button(`Segment ${id}`, () => selectSegment(id))));
    if (!row.predecessor_segment_ids?.length) predecessors.append('None');
    detail.append(predecessors);
    const dl = node('dl');
    fields.forEach(field => { const pair = node('div'); pair.append(node('dt', field.replaceAll('_', ' ')), node('dd', row[field] || 'Pending')); dl.append(pair); });
    detail.append(dl, node('h4', 'Classification rationale'));
    const rationales = row.classification_rationale;
    (Array.isArray(rationales) ? rationales : [rationales || 'Pending']).forEach(value => detail.append(node('p', typeof value === 'string' ? value : JSON.stringify(value))));
    detail.append(node('h4', 'Supporting quotations & provenance'));
    (row.evidence || []).forEach((evidence, index) => {
      const quote = node('blockquote');
      quote.append(button(`Citation ${index + 1} · Read passage`, () => navigateCitation(row, index)), node('p', evidence.quote), node('small', `${evidence.chunk_id} · ${provenance(evidence.provenance || {})}`));
      if (citation?.segment === row.segment && citation.index === index) quote.classList.add('active-citation');
      detail.append(quote);
    });
    detail.append(node('h4', 'Segment evaluation issues'), state.results.evaluation ? rowIssues(row.segment).length ? issueList(rowIssues(row.segment)) : node('p', 'None.') : node('p', 'Pending evaluation of these results.'));
    $('.detail-scroll').scrollTop = scroll;
    window.candidateEditor?.bind(row, state.results.revision, reviewable());
  }
  function selectSegment(id, evidenceIndex = 0) {
    selected = id;
    if (!commentDraft) {
      $('#decision-form').elements.segment_ids.value = id;
      $('#decision-form').elements.comment_segment.value = id;
    }
    renderTable(); renderDetails();
    const row = selectedRow();
    if (row?.evidence?.length) navigateCitation(row, evidenceIndex);
  }
  function renderStatistics() {
    const stats = state.statistics || {stages: [], executions: [], totals: {}};
    const totals = stats.totals;
    const tile = (label, value, detail) => {
      const element = node('div', null, 'stat-tile');
      element.append(node('span', label, 'stat-label'), typeof value === 'string' ? node('strong', value) : value, node('small', detail));
      return element;
    };
    $('#statistics-tiles').replaceChildren(
      tile('Processing time', timeNode('strong', totals.elapsed_seconds || 0, totals.running_since), totals.running_since ? 'Still running' : 'Sum of all steps'),
      tile('Model requests', String(totals.requests || 0), count(totals.attempts || 0, 'attempt')),
      tile('Retries', String(totals.retries || 0), `${duration(totals.retry_wait_seconds || 0)} waiting before retries`),
      tile('Timeouts', String(totals.timeouts || 0), count(totals.failed_attempts || 0, 'failed attempt')),
    );
    // Bars compare each step with the longest one; the percentage is its share of the total.
    const seconds = item => runningSeconds(item.elapsed_seconds, item.running_since);
    const longest = Math.max(0, ...stats.stages.map(seconds));
    const total = stats.stages.reduce((sum, item) => sum + seconds(item), 0);
    $('#statistics-table tbody').replaceChildren(...stats.stages.map(item => {
      const tr = node('tr'); tr.title = `${stageLabels[item.stage] || item.stage}: ${duration(seconds(item))}; ${stepSummary(item)}`;
      const time = node('td'), share = node('td', null, 'share-cell'), bar = node('span', null, 'share-bar'), fill = node('span');
      time.append(timeNode('span', item.elapsed_seconds, item.running_since));
      fill.style.width = `${longest ? seconds(item) / longest * 100 : 0}%`;
      bar.append(fill); share.append(bar, node('span', `${total ? Math.round(seconds(item) / total * 100) : 0}%`));
      tr.append(node('th', stageLabels[item.stage] || item.stage), time, share, node('td', item.executions), node('td', item.requests),
        ...[item.retries, item.timeouts, item.failed_attempts].map(value => node('td', value, value ? 'flagged' : '')),
        node('td', duration(item.retry_wait_seconds)));
      return tr;
    }));
    $('#statistics-empty').hidden = Boolean(stats.stages.length);
    const history = $('#statistics-history');
    const open = new Set([...history.querySelectorAll('details[open]')].map(item => item.dataset.index));
    history.replaceChildren(...stats.executions.map((item, index) => {
      const details = node('details', null, `history-${item.outcome}`);
      details.dataset.index = index; details.open = open.has(String(index));
      const summary = node('summary', `${stageLabels[item.stage] || item.stage} · Round ${item.correction_round ?? '—'} · ${item.outcome} · `);
      summary.append(timeNode('span', item.elapsed_seconds, item.outcome === 'running' ? item.started_at : null), ...badges(item));
      details.append(summary, node('p', `${item.started_at ? `Started ${new Date(item.started_at).toLocaleString()} · ` : ''}${stepSummary(item)}`, 'hint'));
      if (!item.attempt_log.length) { details.append(node('p', 'No model requests in this step.', 'hint')); return details; }
      const table = node('table', null, 'stats-table attempts-table'), head = node('tr');
      ['Task', 'Attempt', 'Outcome', 'Duration', 'Retry wait', 'Details'].forEach(text => head.append(node('th', text)));
      table.append(head, ...item.attempt_log.map(attempt => {
        const tr = node('tr', null, attempt.outcome === 'pass' ? '' : 'attempt-failed');
        [(attempt.task || '').replaceAll('_', ' '), attempt.attempt, attempt.timed_out ? '⏱ timeout' : (attempt.outcome || '').replaceAll('_', ' '),
          duration(attempt.elapsed_seconds), attempt.retry_delay_seconds ? duration(attempt.retry_delay_seconds) : '—', attempt.error || ''].forEach(value => tr.append(node('td', value)));
        return tr;
      }));
      const scroll = node('div', null, 'stats-scroll'); scroll.append(table); details.append(scroll);
      return details;
    }));
    if (!stats.executions.length) history.append(node('p', 'No steps have been timed yet.', 'hint'));
  }
  function renderEvaluation() {
    const evaluation = state.results?.evaluation;
    $('#report-evaluation').replaceChildren(node('p', evaluation?.latest_evaluation?.summary || 'Current evaluation pending or unavailable.'), issueList(issues().filter(issue => !(issue.segment_ids || []).length)));
    (evaluation?.evaluations || []).slice().reverse().forEach(item => {
      const details = node('details'); details.append(node('summary', `${item.trigger || 'Round ' + item.round} · ${item.evaluation.status}`), node('p', item.evaluation.summary), issueList(item.evaluation.issues || []));
      $('#report-evaluation').append(details);
    });
    const human = state.results?.human;
    $('#audit-history').replaceChildren(node('h3', 'Human review audit trail'));
    (human?.edits || []).forEach(edit => $('#audit-history').append(node('p', `Segment ${edit.segment} · ${edit.field}: ${JSON.stringify(edit.old_value)} → ${JSON.stringify(edit.new_value)} (${edit.timestamp})`)));
    (human?.decisions || []).forEach(decision => {
      $('#audit-history').append(node('p', `${decision.decision}: ${decision.global_comment || ''} (${decision.timestamp})`));
      (decision.segment_comments || []).forEach(item => $('#audit-history').append(node('p', `${item.issue_type} · Segment ${item.segment ?? 'report'}: ${item.comment}`)));
    });
  }
  const activeEvidence = () => rows().find(row => row.segment === citation?.segment)?.evidence?.[citation?.index];
  function renderCitationStatus() {
    const evidence = activeEvidence();
    if (!evidence) { $('#citation-state').textContent = ''; return; }
    const descriptions = {exact: 'Quoted original text highlighted in the extracted-text reader.', repeated: 'Quotation repeats in this chunk; exact occurrence is ambiguous. Containing extracted-text chunk highlighted.', unmatched: 'Quotation could not be matched using source normalization. Containing extracted-text chunk highlighted.', missing: 'Cited chunk unavailable. PDF page navigation is available when the original file and page reference exist.'};
    $('#citation-state').textContent = `${descriptions[evidence.highlight?.state] || descriptions.missing} Corresponding translation is linked at chunk level.`;
  }
  function renderReader() {
    const reader = state.reader || {}, select = $('#document-selector'), docValue = select.value;
    const scroll = $('#text-reader').scrollTop;
    select.replaceChildren(...(reader.documents || []).map(doc => new Option(doc.filename, doc.id)));
    if ((reader.documents || []).some(doc => doc.id === docValue)) select.value = docValue;
    const doc = (reader.documents || []).find(doc => doc.id === select.value);
    const mode = $('#reader-mode').value;
    const evidence = activeEvidence();
    const source = $('#source-access'); source.hidden = !doc?.available;
    if (doc?.url) source.href = doc.url;
    source.textContent = doc?.source_type === 'pdf' ? 'Open original PDF file' : 'Download source file';
    source.target = doc?.source_type === 'pdf' ? '_blank' : '';
    $('#text-reader').hidden = mode === 'pdf';
    $('#pdf-reader').hidden = mode !== 'pdf' || doc?.source_type !== 'pdf' || !doc.available;
    const skipped = reader.translated?.language_analysis?.decision === 'skip_translation' || state.manifest.stages?.translation === 'skipped';
    $('#reader-state').textContent = (reader.source_previous ? 'Extracted text belongs to the previous revision. ' : '') + (reader.translation_previous ? 'Translation belongs to the previous revision. ' : '') + (!doc?.available ? 'Original source file unavailable. Saved extracted text remains accessible. ' : '') + (mode === 'pdf' ? doc?.source_type !== 'pdf' ? 'This document has no PDF view. Use Original extracted text.' : 'Original document layout. Page navigation is separate from quote highlighting in extracted text.' : skipped ? 'Translation skipped; text passes through unchanged. No generated translation.' : !reader.translated?.chunks?.length ? 'Translation pending or unavailable.' : 'Original and translated passages are linked by chunk identity.');
    renderCitationStatus();
    if (mode === 'pdf' && doc?.source_type === 'pdf' && doc.available) {
      const page = evidence?.provenance?.filename === doc.filename ? evidence?.provenance?.page : null;
      const url = doc.url + (page ? `#page=${page}` : '');
      if ($('#pdf-reader').getAttribute('src') !== url) $('#pdf-reader').src = url;
    }
    const translated = new Map((reader.translated?.chunks || []).map(chunk => [chunk.chunk_id, chunk]));
    const chunks = (reader.source?.chunks || []).filter(chunk => (chunk.filename || chunk.file) === doc?.filename);
    $('#text-reader').replaceChildren(...chunks.map(chunk => {
      const passage = node('article', null, 'passage'); passage.dataset.chunkId = chunk.chunk_id;
      const active = evidence?.chunk_id === chunk.chunk_id;
      passage.classList.toggle('cited-chunk', active);
      const heading = node('header'); heading.append(node('small', `${chunk.chunk_id} · ${provenance(chunk)}`));
      const citations = rows().flatMap(row => (row.evidence || []).map((item, index) => ({row, item, index}))).filter(item => item.item.chunk_id === chunk.chunk_id);
      if (citations.length) heading.append(button(`Citations (${citations.length})`, () => {
        if (citations.length === 1) selectSegment(citations[0].row.segment, citations[0].index);
        else {
          const choices = $('#citation-choice'); choices.hidden = false;
          choices.replaceChildren(node('strong', 'Select a citation for this passage: '), ...citations.map(item => button(`Segment ${item.row.segment} · Citation ${item.index + 1}`, () => { choices.hidden = true; selectSegment(item.row.segment, item.index); })));
          choices.querySelector('button').focus();
        }
      }));
      passage.append(heading);
      const pair = node('div', null, mode === 'compare' ? 'passage-pair' : '');
      if (mode === 'original' || mode === 'compare' || mode === 'pdf') {
        const original = node('div'); original.append(node('strong', 'Original · Extracted text'));
        const text = node('p', null, 'source-text');
        const range = active && evidence.highlight?.state === 'exact' ? evidence.highlight.ranges[0] : null;
        // Python offsets count codepoints; convert to codepoints before slicing JavaScript strings.
        const content = Array.from(chunk.text || '');
        if (range) text.append(content.slice(0, range[0]).join(''), node('mark', content.slice(...range).join('')), content.slice(range[1]).join(''));
        else text.textContent = chunk.text || '';
        original.append(text); pair.append(original);
      }
      if (mode === 'translation' || mode === 'compare') {
        const translation = node('div', null, active ? 'translated-context' : '');
        const match = translated.get(chunk.chunk_id);
        const applied = reader.translated?.language_analysis?.chunks?.[chunk.chunk_id]?.translation_applied;
        const unchanged = skipped || applied === false || (match && (match.source_language === 'English' || match.translated_text === chunk.text));
        translation.append(node('strong', active ? 'Corresponding translated passage' : 'Translation'), node('p', !match ? 'No corresponding translation available.' : unchanged ? 'Unchanged source passage · No generated translation.' : 'Chunk-level translation · Exact quotation alignment unavailable.', 'hint'), node('p', match?.translated_text || '', 'source-text'));
        pair.append(translation);
      }
      passage.append(pair); return passage;
    }));
    if (!chunks.length) $('#text-reader').append(node('p', 'Extracted text pending or unavailable for this document.'));
    $('#text-reader').scrollTop = scroll;
  }
  function navigateCitation(row, index) {
    $('#citation-choice').hidden = true;
    citation = {segment: row.segment, index};
    const evidence = row.evidence[index];
    const chunk = state.reader?.source?.chunks?.find(chunk => chunk.chunk_id === evidence.chunk_id);
    const filename = chunk?.filename || chunk?.file || evidence.provenance?.filename;
    const doc = state.reader?.documents?.find(doc => doc.filename === filename);
    if (doc) $('#document-selector').value = doc.id;
    renderDetails(); renderReader();
    if (!doc) $('#citation-state').textContent = 'Cited source document unavailable. ' + $('#citation-state').textContent;
    const passage = [...$('#text-reader').children].find(item => item.dataset.chunkId === evidence.chunk_id);
    if (passage) $('#text-reader').scrollTop = passage.offsetTop - 20;
  }
  function apply(data) {
    state = {...state, ...data};
    if (data.statistics) renderStatistics();
    const autoSelect = selected == null && data.results && rows().length;
    if (autoSelect) selected = rows()[0].segment;
    renderHeader();
    if (data.results) { renderTable(); renderDetails(); renderEvaluation(); }
    if (data.reader || data.results) renderReader();
    if (autoSelect && selectedRow()?.evidence?.length) navigateCitation(selectedRow(), 0);
  }
  async function refresh() {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      try {
        const response = await fetch(`${root.dataset.url}?since=${encodeURIComponent(JSON.stringify(state.versions || {}))}`, {headers: {Accept: 'application/json'}, cache: 'no-store', signal: AbortSignal.timeout(12000)});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        apply(await response.json());
        $('#connection-status').hidden = true;
      } catch (error) {
        $('#connection-status').hidden = false;
        $('#connection-status').textContent = `Connection or saved-results read problem (${error.message}). Displaying last received results; retrying. Pipeline failures are listed separately under Warnings and errors.`;
      } finally { inFlight = null; }
    })();
    return inFlight;
  }
  async function poll() { if (!submitting) await refresh(); setTimeout(poll, 1500); }
  async function submit(event) {
    event.preventDefault();
    if (submitting) return;
    const form = event.currentTarget, data = new FormData(form);
    if (event.submitter?.name) data.set(event.submitter.name, event.submitter.value);
    submitting = true; renderHeader(); $('#action-error').hidden = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body: data});
      if (!response.ok) {
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        throw new Error(html.querySelector('[role="alert"]')?.textContent || `Review operation failed (HTTP ${response.status}).`);
      }
      if (form === window.candidateEditor?.form) window.candidateEditor.saved();
      else commentDraft = false;
      if (inFlight) await inFlight;
      await refresh();
    } catch (error) { $('#action-error').textContent = error.message; $('#action-error').hidden = false; }
    finally { submitting = false; renderHeader(); }
  }
  $('#decision-form').addEventListener('input', () => { commentDraft = true; });
  $('#decision-form').addEventListener('change', () => { commentDraft = true; });
  window.addEventListener('beforeunload', event => { if (commentDraft) { event.preventDefault(); event.returnValue = ''; } });
  $('#decision-form').addEventListener('submit', submit);
  window.candidateEditor?.form.addEventListener('submit', submit);
  window.addEventListener('editor-reset', () => renderDetails());
  ['#segment-search', ...filters, '#issues-filter'].forEach(selector => $(selector).addEventListener('input', renderTable));
  $('#reader-mode').addEventListener('change', () => { renderReader(); if (activeEvidence()) navigateCitation(selectedRow(), citation.index); });
  $('#document-selector').addEventListener('change', () => { renderReader(); $('#text-reader').scrollTop = 0; });
  $('#expand-reader').addEventListener('click', () => {
    const expanded = root.classList.toggle('reader-expanded');
    $('#expand-reader').setAttribute('aria-expanded', String(expanded));
    $('#expand-reader').textContent = expanded ? 'Restore workspace' : 'Expand reader';
  });
  $('#open-statistics').addEventListener('click', () => { renderStatistics(); $('#statistics-dialog').showModal(); });
  $('#close-statistics').addEventListener('click', () => $('#statistics-dialog').close());
  setInterval(tick, 1000);
  apply(JSON.parse($('#workspace-data').textContent));
  setTimeout(poll, 1500);
})();
