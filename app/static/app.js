/**
 * app/static/app.js — Single-Page UI Controller for Zamp AP Engine.
 */
let currentRunId = null;
let currentEventSource = null;
let activeRunsData = [];
let activeFilter = 'ALL';
let stageDataCache = {};

// On window load
document.addEventListener('DOMContentLoaded', () => {
  setupDropzone();
  loadSamples();
  loadMetricsAndRuns();

  window.addEventListener('click', (e) => {
    const pdfModal = document.getElementById('pdf-view-modal');
    if (e.target === pdfModal) closePdfModal();
    const ovModal = document.getElementById('override-modal');
    if (e.target === ovModal) closeOverrideModal();
  });
});

function getCurrencySymbol(curr) {
  if (!curr) return '$';
  const c = curr.trim().toUpperCase();
  if (c === 'INR') return '₹';
  if (c === 'USD') return '$';
  if (c === 'GBP') return '£';
  if (c === 'EUR') return '€';
  if (c === 'CAD') return 'CA$';
  return c;
}

// Tab navigation
function switchTab(tab) {
  document.querySelectorAll('.nav-tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));

  const btn = document.getElementById(`tab-btn-${tab}`);
  const pane = document.getElementById(`tab-${tab}`);
  if (btn) btn.classList.add('active');
  if (pane) pane.classList.add('active');

  if (tab === 'dashboard') {
    loadMetricsAndRuns();
  } else if (tab === 'rules') {
    loadRules();
  } else if (tab === 'master') {
    loadMasterData();
  }
}

// ==========================================================================
// File Upload & Sample Handling
// ==========================================================================
function setupDropzone() {
  const dropzone = document.getElementById('dropzone');
  if (!dropzone) return;

  ['dragenter', 'dragover'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    }, false);
  });

  ['dragleave', 'drop'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
    }, false);
  });

  dropzone.addEventListener('drop', (e) => {
    const dt = e.dataTransfer;
    const files = dt.files;
    if (files.length > 0) {
      uploadFile(files[0]);
    }
  });
}

function handleFileSelect(event) {
  const file = event.target.files[0];
  if (file) {
    uploadFile(file);
  }
}

async function uploadFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    alert('Please upload a valid PDF invoice document.');
    return;
  }

  const formData = new FormData();
  formData.append('file', file);

  resetPipeline();
  document.getElementById('active-filename').textContent = file.name;
  document.getElementById('active-run-id').textContent = 'Uploading...';

  try {
    const res = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Upload failed');
    }
    const data = await res.json();
    connectSSE(data.run_id, data.filename);
  } catch (err) {
    alert(`Upload Error: ${err.message}`);
    document.getElementById('active-run-id').textContent = 'Failed';
  }
}

function formatShortBadge(dec) {
  switch (dec) {
    case 'AUTO_APPROVED':
      return { text: 'APPROVED', cls: 'badge-auto-approved' };
    case 'APPROVED_WITH_NOTE':
      return { text: 'APPROVED (NOTE)', cls: 'badge-approved-note' };
    case 'HOLD_FOR_REVIEW':
      return { text: 'HOLD REVIEW', cls: 'badge-hold' };
    case 'REJECTED':
      return { text: 'REJECTED', cls: 'badge-rejected' };
    case 'REQUEST_INFO':
      return { text: 'NEED INFO', cls: 'badge-request-info' };
    default:
      return { text: dec || 'PENDING', cls: 'badge-hold' };
  }
}

async function loadSamples() {
  try {
    const res = await fetch('/api/samples');
    const data = await res.json();
    const container = document.getElementById('samples-list');
    container.innerHTML = '';

    data.samples.forEach(s => {
      const card = document.createElement('div');
      card.className = 'sample-card';
      card.id = `sample-card-${s.filename}`;

      // Enforce strictly single expected outcome
      const rawDec = Array.isArray(s.expected_decision) ? s.expected_decision[0] : (s.expected_decision || 'HOLD_FOR_REVIEW');
      const badgeInfo = formatShortBadge(rawDec);

      card.innerHTML = `
        <div class="sample-card-header">
          <div class="sample-card-title-group">
            <span class="sample-name" title="${s.filename}">${s.filename}</span>
          </div>
          <span class="badge ${badgeInfo.cls}" title="Expected: ${rawDec}">${badgeInfo.text}</span>
        </div>
        <div class="sample-desc" title="${s.expected_reason}">${s.expected_reason}</div>
        <div class="sample-actions">
          <button class="btn-sample-action btn-sample-run" onclick="runSample('${s.filename}')" title="Run processing pipeline">
            <span>▶ Run</span>
          </button>
          <button class="btn-sample-action btn-sample-view" onclick="previewSamplePdf('${s.filename}')" title="Preview PDF inline">
            <span>👁 View</span>
          </button>
          <a href="/api/samples/${encodeURIComponent(s.filename)}/download" class="btn-sample-action btn-sample-download" title="Download PDF file" download>
            <span>⬇ Download</span>
          </a>
        </div>
      `;
      container.appendChild(card);
    });

    document.getElementById('sample-count').textContent = `${data.samples.length} Samples`;
  } catch (err) {
    console.error('Failed to load samples:', err);
  }
}

function previewSamplePdf(filename) {
  const modal = document.getElementById('pdf-view-modal');
  const title = document.getElementById('pdf-modal-title');
  const iframe = document.getElementById('pdf-frame');
  const externalLink = document.getElementById('pdf-modal-external');

  const url = `/api/samples/${encodeURIComponent(filename)}/view`;
  title.textContent = `Invoice Preview: ${filename}`;
  iframe.src = url;
  externalLink.href = url;
  modal.classList.add('active');
}

function closePdfModal() {
  const modal = document.getElementById('pdf-view-modal');
  const iframe = document.getElementById('pdf-frame');
  modal.classList.remove('active');
  iframe.src = 'about:blank';
}

async function runSample(filename) {
  // Highlight active card
  document.querySelectorAll('.sample-card').forEach(c => c.classList.remove('active'));
  const activeCard = document.getElementById(`sample-card-${filename}`);
  if (activeCard) activeCard.classList.add('active');

  resetPipeline();
  document.getElementById('active-filename').textContent = filename;
  document.getElementById('active-run-id').textContent = 'Initializing...';

  try {
    const res = await fetch('/api/run-sample', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    });
    if (!res.ok) throw new Error('Failed to start sample run');
    const data = await res.json();
    connectSSE(data.run_id, data.filename);
  } catch (err) {
    alert(`Error running sample: ${err.message}`);
  }
}

// ==========================================================================
// SSE Live Pipeline Streaming
// ==========================================================================
function connectSSE(runId, filename) {
  if (currentEventSource) {
    currentEventSource.close();
  }

  currentRunId = runId;
  stageDataCache = {};
  document.getElementById('active-run-id').textContent = `ID: ${runId.slice(0, 8)}…`;
  document.getElementById('active-filename').textContent = filename;

  const es = new EventSource(`/api/stream/${runId}`);
  currentEventSource = es;

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handlePipelineEvent(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  es.onerror = (e) => {
    console.warn('SSE connection closed or interrupted');
    es.close();
  };
}

function handlePipelineEvent(ev) {
  const eventType = ev.event;

  if (eventType === 'stage_started') {
    const idx = ev.stage_index;
    const stageEl = document.getElementById(`stage-${idx}`);
    if (stageEl) {
      stageEl.className = 'stage-item stage-running';
      document.getElementById(`stage-${idx}-dur`).textContent = 'Running…';
    }
  } else if (eventType === 'stage_completed') {
    const idx = ev.stage_index;
    const stageEl = document.getElementById(`stage-${idx}`);
    const dur = ev.duration_ms !== null && ev.duration_ms !== undefined ? `${ev.duration_ms} ms` : '—';
    const status = ev.status;

    if (stageEl) {
      stageEl.className = `stage-item stage-${status.toLowerCase()}`;
      document.getElementById(`stage-${idx}-dur`).textContent = dur;
    }

    stageDataCache[idx] = ev.payload;
    renderStageDetails(idx, ev.stage_name, ev.payload);
  } else if (eventType === 'run_completed') {
    renderDecisionCard(ev);
    if (currentEventSource) currentEventSource.close();
    loadMetricsAndRuns(); // Refresh table background
  } else if (eventType === 'run_failed') {
    alert(`Pipeline failed: ${ev.error}`);
    if (currentEventSource) currentEventSource.close();
  }
}

function toggleStageDetails(idx) {
  const el = document.getElementById(`stage-${idx}-details`);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function renderStageDetails(idx, name, payload) {
  const el = document.getElementById(`stage-${idx}-details`);
  if (!el || !payload) return;

  let html = '';

  if (idx === 0) { // Ingest
    html = `
      <div style="display:flex; gap:1.5rem; flex-wrap:wrap;">
        <div><strong>Method:</strong> <span class="badge ${payload.method === 'ocr' ? 'badge-hold' : 'badge-auto-approved'}">${payload.method ? payload.method.toUpperCase() : 'TEXT'}</span></div>
        <div><strong>Pages:</strong> ${payload.page_count || 1}</div>
        <div><strong>Characters:</strong> ${payload.char_count || 0}</div>
      </div>
    `;
  } else if (idx === 1) { // Extract
    const ext = payload.extraction || {};
    const items = ext.line_items || [];
    html = `
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:0.75rem; margin-bottom:0.75rem;">
        <div><span style="color:var(--text-muted)">Vendor:</span> <strong>${ext.vendor_name?.value || '—'}</strong> (${formatConf(ext.vendor_name?.confidence)})</div>
        <div><span style="color:var(--text-muted)">Invoice #:</span> <strong>${ext.invoice_number?.value || '—'}</strong> (${formatConf(ext.invoice_number?.confidence)})</div>
        <div><span style="color:var(--text-muted)">Total:</span> <strong>${getCurrencySymbol(ext.currency?.value)} ${formatCurrency(ext.total?.value)}</strong></div>
        <div><span style="color:var(--text-muted)">PO Ref:</span> <strong>${ext.po_reference?.value || 'None'}</strong></div>
      </div>
      ${items.length > 0 ? `
        <div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:4px;">Line Items (${items.length}):</div>
        <table class="data-table" style="font-size:0.75rem;">
          <thead><tr><th>Description</th><th>Qty</th><th>Unit Price</th><th>Amount</th></tr></thead>
          <tbody>
            ${items.map(i => `<tr><td>${i.description}</td><td>${i.quantity}</td><td>${formatCurrency(i.unit_price)}</td><td>${formatCurrency(i.amount)}</td></tr>`).join('')}
          </tbody>
        </table>
      ` : ''}
    `;
  } else if (idx === 2) { // Validate
    const issues = payload.issues || [];
    html = `
      <div><strong>Status:</strong> ${payload.passed ? '<span style="color:var(--success)">Math & Schema Verified ✓</span>' : '<span style="color:var(--danger)">Validation Issues Detected</span>'}</div>
      ${issues.length > 0 ? `
        <ul style="margin-top:0.4rem; padding-left:1.2rem; color:var(--danger);">
          ${issues.map(i => `<li>${i.field}: ${i.message}</li>`).join('')}
        </ul>
      ` : '<div style="color:var(--text-muted); font-size:0.75rem; margin-top:2px;">No math discrepancies or missing required fields.</div>'}
    `;
  } else if (idx === 3) { // Vendor Check
    const fraud = payload.fraud_flags || [];
    html = `
      <div style="display:flex; gap:1.5rem; flex-wrap:wrap;">
        <div><strong>Matched Vendor:</strong> ${payload.matched_vendor_name || 'No master match'}</div>
        <div><strong>Similarity:</strong> ${payload.match_score || 0}%</div>
        <div><strong>Approved:</strong> ${payload.approved ? 'Yes ✓' : 'No ❌'}</div>
      </div>
      ${fraud.length > 0 ? `
        <div style="margin-top:0.5rem; padding:0.5rem; background:var(--danger-bg); border:1px solid var(--danger-border); border-radius:4px; color:var(--danger);">
          <strong>🚨 Fraud Alert:</strong> ${fraud.map(f => `${f.code} — ${f.message}`).join(', ')}
        </div>
      ` : ''}
    `;
  } else if (idx === 4) { // Duplicate Check
    html = `
      <div><strong>Duplicate Status:</strong> ${payload.is_duplicate ? '<span style="color:var(--danger)">DUPLICATE INVOICE DETECTED ❌</span>' : '<span style="color:var(--success)">Unique Submission ✓</span>'}</div>
      <div style="font-size:0.75rem; color:var(--text-muted); margin-top:2px;">Normalized Key: <code>${payload.normalized_key || '—'}</code></div>
      ${payload.prior_run_id ? `<div style="font-size:0.75rem; color:var(--warning);">Matches prior run: ${payload.prior_run_id}</div>` : ''}
    `;
  } else if (idx === 5) { // PO Match
    const poSym = getCurrencySymbol(payload.po_currency || stageDataCache[1]?.extraction?.currency?.value);
    html = `
      <div style="display:flex; gap:1.5rem; flex-wrap:wrap;">
        <div><strong>PO Number:</strong> ${payload.po_number || 'None'}</div>
        <div><strong>Match Type:</strong> <span class="badge ${payload.match_type === 'explicit' ? 'badge-auto-approved' : 'badge-approved-note'}">${payload.match_type ? payload.match_type.toUpperCase() : 'NONE'}</span></div>
        <div><strong>Remaining Balance:</strong> ${poSym}${formatCurrency(payload.po_remaining_balance)}</div>
        <div><strong>Variance:</strong> ${poSym}${formatCurrency(payload.variance_amount)} (${(payload.variance_pct * 100).toFixed(1)}%)</div>
      </div>
    `;
  } else if (idx === 6) { // Rules Engine
    const results = payload.results || [];
    html = `
      <div style="font-size:0.775rem;">
        <div style="margin-bottom:4px;">Evaluated Rules: ${results.filter(r => r.passed).length} passed, ${payload.failed_count || 0} failed</div>
        <div style="display:flex; flex-wrap:wrap; gap:4px;">
          ${results.map(r => `
            <span class="badge ${r.passed ? 'badge-auto-approved' : 'badge-rejected'}" title="${r.details}">
              ${r.rule_id} ${r.passed ? '✓' : '❌'}
            </span>
          `).join('')}
        </div>
      </div>
    `;
  } else if (idx === 7) { // Decision
    html = `
      <div><strong>Synthesized Decision:</strong> ${payload.decision}</div>
      <div style="font-size:0.775rem; color:var(--text-muted); margin-top:4px;">Reason codes: ${(payload.reason_codes || []).join(', ')}</div>
    `;
  }

  el.innerHTML = html;
}

function renderDecisionCard(data) {
  const card = document.getElementById('decision-result-card');
  const badge = document.getElementById('decision-badge');
  const exp = document.getElementById('decision-explanation');
  const reasonBox = document.getElementById('decision-reason-codes');
  const emailBox = document.getElementById('vendor-email-container');
  const emailContent = document.getElementById('vendor-email-content');
  const prefillBtn = document.getElementById('btn-prefill-test-data');
  const rerunBtn = document.getElementById('btn-rerun-run');

  const dec = data.decision || 'HOLD_FOR_REVIEW';

  let badgeClass = 'badge-auto-approved';
  let badgeLabel = dec;
  if (dec === 'HOLD_FOR_REVIEW') {
    badgeClass = 'badge-hold';
    badgeLabel = 'HOLD FOR REVIEW';
  } else if (dec === 'REJECTED') {
    badgeClass = 'badge-rejected';
    badgeLabel = 'REJECTED';
  } else if (dec === 'APPROVED_WITH_NOTE') {
    badgeClass = 'badge-approved-note';
    badgeLabel = 'APPROVED (WITH AUDIT NOTE)';
  } else if (dec === 'REQUEST_INFO') {
    badgeClass = 'badge-request-info';
    badgeLabel = 'REQUEST MORE INFO';
  } else if (dec === 'AUTO_APPROVED') {
    badgeClass = 'badge-auto-approved';
    badgeLabel = 'AUTO-APPROVED';
  }

  badge.textContent = badgeLabel;
  badge.className = `decision-badge-large ${badgeClass}`;

  const lastStage = stageDataCache[7] || {};
  exp.textContent = lastStage.explanation || 'Invoice processed through the autonomous decision matrix.';

  reasonBox.innerHTML = '';
  const reasonCodes = data.reason_codes || lastStage.reason_codes || [];
  reasonCodes.forEach(rc => {
    const span = document.createElement('span');
    span.className = 'reason-tag';
    span.textContent = rc;
    reasonBox.appendChild(span);
  });

  if (lastStage.vendor_email_draft) {
    emailBox.style.display = 'block';
    emailContent.textContent = lastStage.vendor_email_draft;
  } else {
    emailBox.style.display = 'none';
  }

  // Check if vendor is unknown or no PO matched
  const stageVendor = stageDataCache[3] || {};
  const stagePO = stageDataCache[5] || {};
  const isVendorUnknown = !stageVendor.matched_vendor_name || stageVendor.approved === false ||
    reasonCodes.includes('R2_VENDOR_KNOWN') || reasonCodes.includes('VENDOR_UNKNOWN') || reasonCodes.includes('VENDOR_UNAPPROVED');
  const isNoPOMatched = !stagePO.match_type || stagePO.match_type === 'none' ||
    reasonCodes.includes('R5_PO_REQUIRED') || reasonCodes.includes('NO_MATCHING_PO');

  if (prefillBtn) {
    if (isVendorUnknown || isNoPOMatched) {
      prefillBtn.style.display = 'inline-flex';
    } else {
      prefillBtn.style.display = 'none';
    }
  }

  if (rerunBtn) {
    rerunBtn.style.display = 'inline-flex';
    rerunBtn.disabled = false;
    rerunBtn.textContent = '🔄 Re-run';
  }

  card.style.display = 'block';
}

function resetPipeline() {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  currentRunId = null;
  stageDataCache = {};

  document.getElementById('active-filename').textContent = 'No run active';
  document.getElementById('active-run-id').textContent = 'ID: —';
  document.getElementById('decision-result-card').style.display = 'none';

  for (let i = 0; i < 8; i++) {
    const el = document.getElementById(`stage-${i}`);
    if (el) el.className = 'stage-item';
    const dur = document.getElementById(`stage-${i}-dur`);
    if (dur) dur.textContent = '—';
    const det = document.getElementById(`stage-${i}-details`);
    if (det) {
      det.style.display = 'none';
      det.innerHTML = '';
    }
  }
}

// ==========================================================================
// Dashboard & Metrics
// ==========================================================================
async function loadMetricsAndRuns() {
  try {
    const mRes = await fetch('/api/metrics');
    const m = await mRes.json();

    document.getElementById('kpi-total').textContent = m.total_processed;
    document.getElementById('kpi-rate').textContent = `${m.auto_approval_rate_pct}%`;
    document.getElementById('kpi-saved').textContent = `${m.hours_saved} hrs`;
    document.getElementById('kpi-fraud').textContent = m.fraud_caught_count;

    const rRes = await fetch('/api/runs?limit=50');
    const rData = await rRes.json();
    activeRunsData = rData.runs;
    renderRunsTable(activeRunsData);
  } catch (err) {
    console.error('Failed to load dashboard:', err);
  }
}

function renderRunsTable(runs) {
  const tbody = document.getElementById('runs-table-body');
  tbody.innerHTML = '';

  if (!runs || runs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; padding:2rem;">No matching runs found.</td></tr>`;
    return;
  }

  runs.forEach(run => {
    const tr = document.createElement('tr');

    let badgeClass = 'badge-auto-approved';
    const dec = run.decision || 'PENDING';
    if (dec === 'HOLD_FOR_REVIEW') badgeClass = 'badge-hold';
    else if (dec === 'REJECTED') badgeClass = 'badge-rejected';
    else if (dec === 'APPROVED_WITH_NOTE') badgeClass = 'badge-approved-note';
    else if (dec === 'REQUEST_INFO') badgeClass = 'badge-request-info';

    const ts = run.created_at ? new Date(run.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
    const currSym = getCurrencySymbol(run.currency);
    const total = run.invoice_total !== null ? `${currSym} ${formatCurrency(run.invoice_total)}` : '—';

    tr.innerHTML = `
      <td>${ts}</td>
      <td class="strong">${run.filename}</td>
      <td>${run.vendor_name || '—'}</td>
      <td><code>${run.invoice_number || '—'}</code></td>
      <td>${total}</td>
      <td>${run.po_number || '—'}</td>
      <td><span class="badge ${badgeClass}">${dec}</span></td>
      <td>
        <button class="btn-secondary" style="padding:0.25rem 0.6rem; font-size:0.75rem;" onclick="inspectRun('${run.id}')">
          Inspect
        </button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function setRunFilter(filter, btn) {
  activeFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  filterRunsTable();
}

function filterRunsTable() {
  const query = (document.getElementById('run-search-input').value || '').toLowerCase();
  const filtered = activeRunsData.filter(r => {
    const matchesFilter = activeFilter === 'ALL' || r.decision === activeFilter;
    const matchesSearch = !query ||
      (r.filename && r.filename.toLowerCase().includes(query)) ||
      (r.vendor_name && r.vendor_name.toLowerCase().includes(query)) ||
      (r.invoice_number && r.invoice_number.toLowerCase().includes(query));
    return matchesFilter && matchesSearch;
  });
  renderRunsTable(filtered);
}

async function inspectRun(runId) {
  try {
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) throw new Error('Run not found');
    const data = await res.json();

    switchTab('studio');
    resetPipeline();

    const run = data.run;
    document.getElementById('active-filename').textContent = run.filename;
    document.getElementById('active-run-id').textContent = `ID: ${run.id.slice(0, 8)}…`;
    currentRunId = run.id;

    // Replay stages from stored results
    const stages = data.stages || [];
    stages.forEach(s => {
      handlePipelineEvent(s);
    });

    // Render final decision
    if (run.decision) {
      renderDecisionCard({
        decision: run.decision,
        reason_codes: data.decision_details?.reason_codes || [],
      });
      if (data.decision_details?.explanation) {
        document.getElementById('decision-explanation').textContent = data.decision_details.explanation;
      }
    }
  } catch (err) {
    alert(`Could not load run: ${err.message}`);
  }
}

// ==========================================================================
// Manual Decision Override Modal
// ==========================================================================
function openOverrideModal() {
  if (!currentRunId) {
    alert('Please select or execute an invoice run first.');
    return;
  }
  document.getElementById('override-modal').classList.add('active');
}

function closeOverrideModal() {
  document.getElementById('override-modal').classList.remove('active');
}

async function submitDecisionOverride() {
  const decision = document.getElementById('override-decision-select').value;
  const reason = document.getElementById('override-reason-input').value.trim();

  if (!reason) {
    alert('Please enter a justification reason for the audit log.');
    return;
  }

  try {
    const res = await fetch(`/api/runs/${currentRunId}/override`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason, actor: 'finance_manager' }),
    });
    if (!res.ok) throw new Error('Override request failed');

    closeOverrideModal();
    alert(`Decision updated to ${decision}. Logged to audit trail.`);

    // Refresh display
    inspectRun(currentRunId);
    loadMetricsAndRuns();
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

// ==========================================================================
// Rules Engine Configuration
// ==========================================================================
async function loadRules() {
  try {
    const res = await fetch('/api/rules');
    const rules = await res.json();

    document.getElementById('rule-max-auto').value = rules.max_auto_approve_amount;
    document.getElementById('rule-po-pct').value = rules.amount_tolerance_pct;
    document.getElementById('rule-po-fixed').value = rules.amount_tolerance_fixed;
    document.getElementById('rule-min-conf').value = rules.min_extraction_confidence;
    document.getElementById('rule-dup-days').value = rules.duplicate_date_window_days;
    document.getElementById('rule-mins-inv').value = rules.minutes_per_invoice_manual;
  } catch (err) {
    console.error('Failed to load rules:', err);
  }
}

async function saveRulesConfig() {
  const payload = {
    max_auto_approve_amount: parseFloat(document.getElementById('rule-max-auto').value),
    amount_tolerance_pct: parseFloat(document.getElementById('rule-po-pct').value),
    amount_tolerance_fixed: parseFloat(document.getElementById('rule-po-fixed').value),
    min_extraction_confidence: parseFloat(document.getElementById('rule-min-conf').value),
    min_inferred_po_confidence: 0.65,
    currency_must_match: true,
    require_po_for_amount_above: 10000.0,
    duplicate_date_window_days: parseInt(document.getElementById('rule-dup-days').value),
    minutes_per_invoice_manual: parseFloat(document.getElementById('rule-mins-inv').value),
  };

  try {
    const res = await fetch('/api/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error('Failed to update rules');

    const statusEl = document.getElementById('rules-save-status');
    statusEl.textContent = 'Saved successfully! ✓';
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
  } catch (err) {
    alert(`Save error: ${err.message}`);
  }
}

// ==========================================================================
// Master Data View & Test Data Management
// ==========================================================================
async function loadMasterData() {
  try {
    const [vRes, pRes] = await Promise.all([
      fetch('/api/vendors'),
      fetch('/api/purchase-orders'),
    ]);

    const vData = await vRes.json();
    const pData = await pRes.json();

    // Populate Vendors table
    const vTbody = document.getElementById('master-vendors-body');
    const vBadge = document.getElementById('vendor-count-badge');
    if (vBadge) vBadge.textContent = `${vData.vendors.length} Records`;

    if (vTbody) {
      if (vData.vendors.length === 0) {
        vTbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:1.5rem;">No vendors found.</td></tr>';
      } else {
        vTbody.innerHTML = vData.vendors.map(v => `
          <tr>
            <td><code>${v.id}</code></td>
            <td class="strong">${escapeHtml(v.name)}</td>
            <td>${escapeHtml(v.tax_id) || '—'}</td>
            <td><code>${escapeHtml(v.bank_account) || '—'}</code></td>
            <td><span class="badge ${v.approved ? 'badge-auto-approved' : 'badge-rejected'}">${v.approved ? 'Approved' : 'Unapproved'}</span></td>
            <td>${v.is_user_added ? '<span class="badge-test">Test (user-added)</span>' : '<span class="badge-seeded">Seeded</span>'}</td>
            <td>${v.is_user_added ? `<button class="btn-delete" onclick="deleteVendor('${v.id}')">Delete</button>` : '<span style="color:var(--text-muted)">—</span>'}</td>
          </tr>
        `).join('');
      }
    }

    // Populate PO form vendor select dropdown
    const poVendorSelect = document.getElementById('po-input-vendor');
    if (poVendorSelect) {
      const currentSelected = poVendorSelect.value;
      let opts = '<option value="">Select a vendor...</option>';
      vData.vendors.forEach(v => {
        opts += `<option value="${v.id}">${escapeHtml(v.name)} (${v.id})</option>`;
      });
      poVendorSelect.innerHTML = opts;
      if (currentSelected) poVendorSelect.value = currentSelected;
    }

    // Populate Purchase Orders table
    const pTbody = document.getElementById('master-pos-body');
    const pBadge = document.getElementById('po-count-badge');
    if (pBadge) pBadge.textContent = `${pData.purchase_orders.length} Records`;

    if (pTbody) {
      if (pData.purchase_orders.length === 0) {
        pTbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:1.5rem;">No purchase orders found.</td></tr>';
      } else {
        pTbody.innerHTML = pData.purchase_orders.map(p => `
          <tr>
            <td><code>${p.po_number}</code></td>
            <td>${p.vendor_id}</td>
            <td class="strong">${p.currency} ${formatCurrency(p.total_amount)}</td>
            <td>${p.currency}</td>
            <td>${p.is_user_added ? '<span class="badge-test">Test (user-added)</span>' : '<span class="badge-seeded">Seeded</span>'}</td>
            <td>${p.is_user_added ? `<button class="btn-delete" onclick="deletePO('${p.po_number}')">Delete</button>` : '<span style="color:var(--text-muted)">—</span>'}</td>
          </tr>
        `).join('');
      }
    }
  } catch (err) {
    console.error('Failed to load master data:', err);
  }
}

async function submitAddVendor(event) {
  event.preventDefault();
  const name = document.getElementById('vendor-input-name').value.trim();
  const taxId = document.getElementById('vendor-input-tax').value.trim() || null;
  const bankAccount = document.getElementById('vendor-input-bank').value.trim() || null;
  const currency = document.getElementById('vendor-input-currency').value;
  const approved = document.getElementById('vendor-input-approved').checked;
  const statusEl = document.getElementById('vendor-form-status');

  if (!name) {
    alert('Vendor name is required.');
    return;
  }

  statusEl.textContent = 'Adding vendor…';
  statusEl.style.color = 'var(--text-muted)';

  try {
    const res = await fetch('/api/vendors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, tax_id: taxId, bank_account: bankAccount, currency, approved }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Failed to add vendor');
    }

    statusEl.textContent = 'Vendor added! ✓';
    statusEl.style.color = 'var(--success)';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3500);

    // Refresh master data
    await loadMasterData();

    // Auto-select this newly created vendor in PO form
    const poVendorSelect = document.getElementById('po-input-vendor');
    if (poVendorSelect && data.vendor?.id) {
      poVendorSelect.value = data.vendor.id;
    }
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function submitAddPO(event) {
  event.preventDefault();
  const poNumber = document.getElementById('po-input-number').value.trim();
  const vendorId = document.getElementById('po-input-vendor').value;
  const amountVal = document.getElementById('po-input-amount').value;
  const currency = document.getElementById('po-input-currency').value;
  const description = document.getElementById('po-input-desc').value.trim() || null;
  const statusEl = document.getElementById('po-form-status');

  if (!poNumber) {
    alert('PO number is required.');
    return;
  }
  if (!vendorId) {
    alert('Please select a vendor.');
    return;
  }
  const totalAmount = parseFloat(amountVal);
  if (isNaN(totalAmount) || totalAmount <= 0) {
    alert('PO amount must be a positive number.');
    return;
  }

  statusEl.textContent = 'Adding purchase order…';
  statusEl.style.color = 'var(--text-muted)';

  try {
    const res = await fetch('/api/purchase-orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        po_number: poNumber,
        vendor_id: vendorId,
        total_amount: totalAmount,
        currency,
        description,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Failed to add purchase order');
    }

    statusEl.textContent = 'Purchase order added! ✓';
    statusEl.style.color = 'var(--success)';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3500);

    await loadMasterData();
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function deleteVendor(vendorId) {
  if (!confirm(`Delete test vendor '${vendorId}' and its associated test purchase orders?`)) return;

  try {
    const res = await fetch(`/api/vendors/${vendorId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Delete failed');

    await loadMasterData();
  } catch (err) {
    alert(`Could not delete vendor: ${err.message}`);
  }
}

async function deletePO(poNumber) {
  if (!confirm(`Delete test purchase order '${poNumber}'?`)) return;

  try {
    const res = await fetch(`/api/purchase-orders/${poNumber}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Delete failed');

    await loadMasterData();
  } catch (err) {
    alert(`Could not delete purchase order: ${err.message}`);
  }
}

async function handleResetDemoData() {
  if (!confirm('This will delete all test (user-added) vendors and purchase orders, clear uploaded PDFs, and restore seeded demo data. Proceed?')) {
    return;
  }

  try {
    const res = await fetch('/api/reset-demo', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Reset failed');

    alert('Demo data has been reset to defaults.');
    await loadMasterData();
    loadMetricsAndRuns();
  } catch (err) {
    alert(`Reset error: ${err.message}`);
  }
}

async function prefillFromCurrentRun() {
  let ext = stageDataCache[1]?.extraction;
  // If not cached, fetch from API
  if (!ext && currentRunId) {
    try {
      const res = await fetch(`/api/runs/${currentRunId}`);
      if (res.ok) {
        const d = await res.json();
        ext = d.extraction;
      }
    } catch (e) {
      console.error(e);
    }
  }

  const vendorName = ext?.vendor_name?.value || '';
  const taxId = ext?.tax_id?.value || '';
  const bankAccount = ext?.bank_account_number?.value || '';
  const currency = (ext?.currency?.value || 'INR').toUpperCase();
  const poRef = ext?.po_reference?.value || '';
  const totalAmount = ext?.total?.value || '';

  // Switch to Master Data tab
  switchTab('master');

  // Fill Vendor form
  const vNameEl = document.getElementById('vendor-input-name');
  const vTaxEl = document.getElementById('vendor-input-tax');
  const vBankEl = document.getElementById('vendor-input-bank');
  const vCurrEl = document.getElementById('vendor-input-currency');
  const vApprEl = document.getElementById('vendor-input-approved');

  if (vNameEl) vNameEl.value = vendorName;
  if (vTaxEl) vTaxEl.value = taxId;
  if (vBankEl) vBankEl.value = bankAccount;
  if (vCurrEl && ['USD', 'EUR', 'GBP', 'CAD', 'INR'].includes(currency)) {
    vCurrEl.value = currency;
  }
  if (vApprEl) vApprEl.checked = true;

  // Fill PO form
  const poNumEl = document.getElementById('po-input-number');
  const poAmtEl = document.getElementById('po-input-amount');
  const poCurrEl = document.getElementById('po-input-currency');
  const poDescEl = document.getElementById('po-input-desc');

  if (poNumEl) poNumEl.value = poRef || `PO-${Math.floor(100000 + Math.random() * 900000)}`;
  if (poAmtEl) poAmtEl.value = totalAmount ? parseFloat(totalAmount).toFixed(2) : '';
  if (poCurrEl && ['USD', 'EUR', 'GBP', 'CAD', 'INR'].includes(currency)) {
    poCurrEl.value = currency;
  }
  if (poDescEl) poDescEl.value = `Test invoice matching PO for ${vendorName || 'test vendor'}`;

  // Highlight forms and scroll into view
  const vCard = document.getElementById('card-add-vendor');
  const pCard = document.getElementById('card-add-po');
  if (vCard) {
    vCard.classList.remove('highlight-pulse');
    void vCard.offsetWidth;
    vCard.classList.add('highlight-pulse');
    vCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  if (pCard) {
    pCard.classList.remove('highlight-pulse');
    void pCard.offsetWidth;
    pCard.classList.add('highlight-pulse');
  }
}

async function rerunCurrentFile() {
  if (!currentRunId) {
    alert('No active run selected to re-run.');
    return;
  }

  const rerunBtn = document.getElementById('btn-rerun-run');
  if (rerunBtn) {
    rerunBtn.disabled = true;
    rerunBtn.textContent = 'Starting re-run…';
  }

  try {
    const res = await fetch(`/api/runs/${currentRunId}/rerun`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Re-run failed');
    }
    const data = await res.json();

    resetPipeline();
    document.getElementById('active-filename').textContent = data.filename;
    document.getElementById('active-run-id').textContent = `ID: ${data.run_id.slice(0, 8)}…`;

    connectStream(data.run_id);
    switchTab('studio');
  } catch (err) {
    alert(`Re-run failed: ${err.message}`);
    if (rerunBtn) {
      rerunBtn.disabled = false;
      rerunBtn.textContent = '🔄 Re-run';
    }
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// Helpers
function formatCurrency(val) {
  if (val === null || val === undefined || isNaN(val)) return '0.00';
  return Number(val).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatConf(val) {
  if (val === null || val === undefined) return '0%';
  return `${Math.round(val * 100)}%`;
}
