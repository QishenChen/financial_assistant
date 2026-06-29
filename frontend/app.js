// ── Configuration ──
const API_BASE = window.location.origin;

// Known raw-PDF subdirectories for non-uploaded documents.
const RAW_DOMAINS = ['financial_reports', 'financial_contracts', 'insurance', 'regulatory/attachments', 'regulatory/html', 'regulatory/txt', 'research'];

// ── PDF.js setup ──
pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';

let pdfDoc = null;
let pdfPageNum = 1;
let pdfScale = 1.2;
let pdfRenderTask = null;
let currentPdfPath = null;
let currentDocPath = null;

// ── State ──
let isProcessing = false;
let currentSessionId = localStorage.getItem('currentSessionId') || null;

// ── DOM ──
const fileList = document.getElementById('fileList');
const chatHistory = document.getElementById('chatHistory');
const queryInput = document.getElementById('queryInput');
const sendBtn = document.getElementById('sendBtn');
const stopBtn = document.getElementById('stopBtn');
const pdfContainer = document.getElementById('pdfContainer');
const pdfCanvas = document.getElementById('pdfCanvas');
const pdfPlaceholder = document.getElementById('pdfPlaceholder');
const pdfControls = document.getElementById('pdfControls');
const pdfPrev = document.getElementById('pdfPrev');
const pdfNext = document.getElementById('pdfNext');
const pdfPageNumEl = document.getElementById('pdfPageNum');
const pdfPageInfo = document.getElementById('pdfPageInfo');

// ── Initialize ──
document.addEventListener('DOMContentLoaded', () => {
  loadFiles();
  loadSessions();
  setupResizers();
  setupSampleQueries();

  sendBtn.addEventListener('click', submitQuery);
  queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submitQuery();
    }
  });
  stopBtn.addEventListener('click', () => { isProcessing = false; });

  pdfPrev.addEventListener('click', () => changePage(-1));
  pdfNext.addEventListener('click', () => changePage(1));

  document.getElementById('refreshFiles').addEventListener('click', loadFiles);
  document.getElementById('refreshSessions').addEventListener('click', loadSessions);
  document.getElementById('newSession').addEventListener('click', createNewSession);

  // Auto-refresh the file list while background extraction/indexing runs
  setInterval(() => {
    if (!isProcessing) loadFiles();
  }, 10000);

  // If we have a saved session, load its history.
  if (currentSessionId) {
    loadSession(currentSessionId);
  }
});

// ── Sessions ──
async function loadSessions() {
  try {
    const resp = await fetch(`${API_BASE}/sessions`);
    const data = await resp.json();
    const list = document.getElementById('sessionList');
    list.innerHTML = '';

    const sessions = data.sessions || [];
    if (sessions.length === 0) {
      list.innerHTML = '<p class="loading">No sessions yet. Start a new chat.</p>';
      return;
    }

    for (const s of sessions) {
      const div = document.createElement('div');
      div.className = 'file-item session-item';
      if (s.id === currentSessionId) {
        div.classList.add('active');
      }
      div.style.paddingLeft = '16px';
      const date = new Date(s.updated_at * 1000).toLocaleString();
      div.textContent = s.title || 'Untitled chat';
      div.title = `Updated: ${date}`;
      div.addEventListener('click', () => {
        currentSessionId = s.id;
        localStorage.setItem('currentSessionId', currentSessionId);
        loadSession(currentSessionId);
        loadSessions(); // re-render highlights
      });
      list.appendChild(div);
    }
  } catch (e) {
    document.getElementById('sessionList').innerHTML = '<p class="loading">Failed to load sessions</p>';
  }
}

async function createNewSession() {
  try {
    const resp = await fetch(`${API_BASE}/sessions/new`, { method: 'POST' });
    const data = await resp.json();
    currentSessionId = data.session_id;
    localStorage.setItem('currentSessionId', currentSessionId);
    chatHistory.innerHTML = '';
    addWelcomeMessage();
    loadSessions();
  } catch (e) {
    console.error('Failed to create session:', e);
  }
}

async function loadSession(sessionId) {
  try {
    const resp = await fetch(`${API_BASE}/sessions/${sessionId}`);
    if (!resp.ok) {
      // Session may have been deleted; reset.
      currentSessionId = null;
      localStorage.removeItem('currentSessionId');
      return;
    }
    const data = await resp.json();
    chatHistory.innerHTML = '';
    const messages = data.messages || [];
    if (messages.length === 0) {
      addWelcomeMessage();
    } else {
      for (const m of messages) {
        renderMessage(m.role, m.content, m.file_path);
      }
    }
    chatHistory.scrollTop = chatHistory.scrollHeight;
  } catch (e) {
    console.error('Failed to load session:', e);
  }
}

function addWelcomeMessage() {
  chatHistory.innerHTML = `
    <div class="welcome-message">
      <h1>📊 Financial Document Intelligence</h1>
      <p>Ask questions about financial documents. The system will extract, analyze, and synthesize answers.</p>
      <div class="sample-queries">
        <span class="sample" data-query="分析招商银行坏账以及信用风险，收集详细数据">分析招商银行坏账及信用风险</span>
        <span class="sample" data-query="Compare R&D spending of BYD vs CATL">Compare BYD vs CATL R&D</span>
        <span class="sample" data-query="中国建筑有哪些施工项目城市？">中国建筑施工项目城市</span>
      </div>
    </div>
  `;
  setupSampleQueries();
}

function renderMessage(role, content, filePath) {
  if (role === 'assistant') {
    const msgDiv = addMessage('assistant', '');
    let processed = content.replace(
      /\[([^\]]*?)\]\(ref:([^:)\s]+)(?::([^)\s]+))?\)/g,
      (match, text, docId, pageNum) => {
        const pageAttr = pageNum ? ' data-page="' + escapeHtml(pageNum) + '"' : ' data-page=""';
        const label = pageNum ? docId + ' p.' + pageNum : docId;
        return '<a href="#" class="ref-badge pdf-ref" data-doc="' + escapeHtml(docId) + '"' + pageAttr +
               ' style="display:inline-block;background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb44;padding:1px 8px;border-radius:4px;font-size:11px;cursor:pointer;margin:0 2px;text-decoration:none;"' +
               ' title="View source: ' + escapeHtml(label) + '">📎 ' + escapeHtml(label) + '</a>';
      }
    );
    if (filePath) {
      processed += `\n\n<small style="color:#8b949e">📄 Report saved to: ${escapeHtml(filePath)}</small>`;
    }
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
      try {
        msgDiv.querySelector('.msg-content').innerHTML = marked.parse(processed, { breaks: true, gfm: true });
      } catch {
        msgDiv.querySelector('.msg-content').innerHTML = escapeHtml(processed).replace(/\n/g, '<br>');
      }
    } else {
      msgDiv.querySelector('.msg-content').innerHTML = escapeHtml(processed).replace(/\n/g, '<br>');
    }
    attachPdfLinks(msgDiv);
  } else {
    addMessage('user', content);
  }
}

// ── File List ──
async function loadFiles() {
  try {
    const resp = await fetch(`${API_BASE}/catalog`);
    const data = await resp.json();
    fileList.innerHTML = '';

    // Prefer structured domains response; fall back to legacy text catalog.
    const domains = data.domains;
    if (domains && typeof domains === 'object') {
      for (const [domain, docs] of Object.entries(domains)) {
        const domainDiv = document.createElement('div');
        domainDiv.className = 'file-item';
        domainDiv.style.color = '#58a6ff';
        domainDiv.style.fontWeight = '600';
        domainDiv.textContent = `[${domain}] — ${docs.length} document(s)`;
        fileList.appendChild(domainDiv);

        for (const doc of docs) {
          const div = document.createElement('div');
          const status = doc.status || (doc.source === 'indexed' && doc.pages > 0 ? 'ready' : 'extracting');
          div.className = `file-item file-item-${status}`;
          div.style.paddingLeft = '16px';
          const displayName = doc.display_name || doc.name || doc.id;
          const pageInfo = doc.pages ? `, ~${doc.pages} pages` : '';
          const statusLabels = {
            ready: '',
            extracting: ' [extracting…]',
            queued: ' [queued]',
            failed: ' [failed]',
          };
          const statusBadge = statusLabels[status] || ' [indexing…]';
          div.textContent = `${displayName} (id: ${doc.id}${pageInfo})${statusBadge}`;
          const statusHints = {
            ready: doc.summary || '',
            extracting: (doc.summary ? doc.summary + '\n' : '') + 'Extracting text and tables; page references will appear shortly.',
            queued: 'Waiting for the extraction worker to start.',
            failed: 'Extraction failed. You can try re-uploading the file.',
          };
          div.title = statusHints[status] || '';
          div.dataset.docId = doc.id;
          div.dataset.source = doc.source || 'indexed';
          div.addEventListener('click', () => {
            document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
            div.classList.add('active');
            if (status === 'ready') {
              loadPdfByDocId(doc.id, doc.source);
            } else {
              const msg = status === 'failed'
                ? `“${displayName}” could not be extracted. Please try re-uploading it.`
                : `“${displayName}” is ${status === 'extracting' ? 'being extracted' : 'queued for extraction'}. Preview and page references will appear shortly.`;
              showPdfPlaceholder(msg);
            }
          });
          fileList.appendChild(div);
        }
      }
      return;
    }

    // Legacy text-catalog fallback
    const catalog = data.catalog || '';
    const lines = catalog.split('\n');
    let currentDomain = null;
    for (const line of lines) {
      if (line.startsWith('[')) {
        const domain = line.slice(1, line.indexOf(']'));
        currentDomain = document.createElement('div');
        currentDomain.className = 'file-item';
        currentDomain.style.color = '#58a6ff';
        currentDomain.style.fontWeight = '600';
        currentDomain.textContent = line.trim();
        fileList.appendChild(currentDomain);
      } else if (line.includes('(id:') && currentDomain) {
        const match = line.match(/-\s+(.+?)\s+\(id:\s+(\S+)/);
        if (match) {
          const div = document.createElement('div');
          div.className = 'file-item';
          div.style.paddingLeft = '16px';
          div.textContent = match[1].trim();
          div.dataset.docId = match[2];
          div.addEventListener('click', () => {
            document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
            div.classList.add('active');
            loadPdfByDocId(match[2]);
          });
          fileList.appendChild(div);
        }
      }
    }
  } catch (e) {
    fileList.innerHTML = '<p class="loading">Failed to load documents</p>';
  }
}

// ── Sample Queries ──
function setupSampleQueries() {
  document.querySelectorAll('.sample').forEach(el => {
    el.addEventListener('click', () => {
      queryInput.value = el.dataset.query;
      submitQuery();
    });
  });
}

// ── Submit Query ──
async function submitQuery() {
  const query = queryInput.value.trim();
  if (!query || isProcessing) return;

  isProcessing = true;
  sendBtn.disabled = true;
  stopBtn.style.display = 'inline-block';

  const welcome = chatHistory.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  addMessage('user', query);

  const msgDiv = addMessage('assistant', '<div class="spinner"></div><p>Planning...</p>');
  const stepsDiv = document.createElement('div');
  stepsDiv.className = 'steps-status';
  msgDiv.querySelector('.msg-content').prepend(stepsDiv);

  try {
    const resp = await fetch(`${API_BASE}/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, session_id: currentSessionId }),
    });

    const data = await resp.json();

    if (data.session_id) {
      currentSessionId = data.session_id;
      localStorage.setItem('currentSessionId', currentSessionId);
      loadSessions();
    }

    stepsDiv.innerHTML = '';
    const stepsLog = data.steps_log || [];
    stepsLog.forEach(s => {
      const badge = document.createElement('span');
      badge.className = `step-badge ${s.verdict === 'PASS' ? 'pass' : 'fail'}`;
      badge.textContent = `${s.verdict === 'PASS' ? '✓' : '✗'} ${s.task_type}`;
      stepsDiv.appendChild(badge);
    });

    const answer = data.answer || '(No answer)';
    renderMessage('assistant', answer, data.file_path);
    // Remove the spinner placeholder we created earlier.
    msgDiv.remove();

  } catch (e) {
    msgDiv.querySelector('.msg-content').innerHTML = `<p style="color:#f85149">Error: ${e.message}</p>`;
  }

  chatHistory.scrollTop = chatHistory.scrollHeight;

  isProcessing = false;
  sendBtn.disabled = false;
  stopBtn.style.display = 'none';
  queryInput.value = '';
  queryInput.focus();
}

// ── Messages ──
function addMessage(role, content) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.innerHTML = `<div class="msg-content">${content}</div>`;
  chatHistory.appendChild(div);
  chatHistory.scrollTop = chatHistory.scrollHeight;
  return div;
}

// ── PDF References ──
function attachPdfLinks(container) {
  container.querySelectorAll('.pdf-ref, a[data-page]').forEach(link => {
    link.addEventListener('click', async (e) => {
      e.preventDefault();
      const doc = link.dataset.doc;
      const page = parseInt(link.dataset.page) || 1;

      if (currentDocPath !== doc) {
        currentDocPath = doc;
        const pdfPath = await resolvePdfPath(doc);
        if (pdfPath) {
          await loadPdf(pdfPath);
        }
      }
      
      if (pdfDoc) {
        jumpToPage(page);
      }
    });
  });
}

// Try both lowercase and uppercase PDF extensions and return the first existing URL.
async function resolvePdfUrlWithCase(basePath) {
  for (const ext of ['.pdf', '.PDF']) {
    const url = basePath + ext;
    try {
      const test = await fetch(url, { method: 'HEAD' });
      if (test.ok) return url;
    } catch {}
  }
  // Default to lowercase when nothing is found so the caller still gets a meaningful 404.
  return basePath + '.pdf';
}

async function resolvePdfPath(docRelPath) {
  // docRelPath is the doc_id from ref: links, e.g. "annual_cmb_2025_report"
  try {
    const resp = await fetch(`${API_BASE}/page-map`);
    const data = await resp.json();
    for (const [key, info] of Object.entries(data.documents || {})) {
      if (key.includes(docRelPath) || (info.raw_rel_path && info.raw_rel_path.includes(docRelPath))) {
        let stem = (info.raw_rel_path || key).replace(/\.(md|pdf|PDF)$/i, '');
        // Uploaded PDFs are served from /uploads/; everything else from /raw/.
        const basePath = (info.domain === 'uploaded') ? '/uploads/' + stem : '/raw/' + stem;
        return await resolvePdfUrlWithCase(basePath);
      }
    }
  } catch {}

  // Fallback: search known raw directories, then uploads.
  for (const domain of RAW_DOMAINS) {
    const path = await resolvePdfUrlWithCase('/raw/' + domain + '/' + docRelPath);
    try {
      const test = await fetch(path, { method: 'HEAD' });
      if (test.ok) return path;
    } catch {}
  }

  const uploadPath = await resolvePdfUrlWithCase('/uploads/' + docRelPath);
  try {
    const test = await fetch(uploadPath, { method: 'HEAD' });
    if (test.ok) return uploadPath;
  } catch {}

  return '/uploads/' + docRelPath + '.pdf';
}

// ── PDF.js Rendering ──
function showPdfPlaceholder(message) {
  pdfPlaceholder.textContent = message;
  pdfPlaceholder.style.display = 'block';
  pdfCanvas.style.display = 'none';
  pdfControls.style.display = 'none';
  pdfPageInfo.textContent = '';
  pdfDoc = null;
  currentPdfPath = null;
}

async function loadPdf(path) {
  try {
    pdfDoc = await pdfjsLib.getDocument({
      url: path,
      cMapUrl: 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/cmaps/',
      cMapPacked: true,
    }).promise;
    pdfPageNum = 1;
    pdfPlaceholder.style.display = 'none';
    pdfCanvas.style.display = 'block';
    pdfControls.style.display = 'flex';
    pdfPageInfo.textContent = `${pdfDoc.numPages} pages`;
    renderPdfPage();
  } catch (e) {
    let message = `Failed to load PDF: ${path}`;
    const errText = (e && (e.message || String(e))) || '';
    if (errText.includes('404') || errText.includes('Not Found')) {
      message = `PDF not found (404): ${path}`;
    } else if (errText.includes('NetworkError') || errText.includes('network') || errText.includes('CORS') || errText.includes('Failed to fetch')) {
      message = `Network/CORS error loading PDF. Direct download may work, but PDF.js cannot fetch it. Ensure the server and UI share the same origin or CORS is enabled.\n${path}`;
    } else if (errText.includes('Invalid') || errText.includes('parse') || errText.includes('structure')) {
      message = `PDF loaded but could not be parsed: ${path}`;
    }
    pdfPlaceholder.textContent = message;
    pdfPlaceholder.style.display = 'block';
    pdfCanvas.style.display = 'none';
    pdfControls.style.display = 'none';
    console.error('[PDF.js] load error:', e);
  }
}

async function renderPdfPage() {
  if (!pdfDoc || !pdfPageNum) return;

  if (pdfRenderTask) {
    pdfRenderTask.cancel();
    try { await pdfRenderTask.promise; } catch {}
  }

  const page = await pdfDoc.getPage(pdfPageNum);
  const baseViewport = page.getViewport({ scale: 1 });

  // Fit page inside the container while preserving aspect ratio.
  const padding = 16;
  const containerWidth = Math.max(pdfContainer.clientWidth - padding, 1);
  const containerHeight = Math.max(pdfContainer.clientHeight - padding, 1);
  pdfScale = Math.min(containerWidth / baseViewport.width, containerHeight / baseViewport.height);
  const scaledViewport = page.getViewport({ scale: pdfScale });

  pdfCanvas.width = scaledViewport.width;
  pdfCanvas.height = scaledViewport.height;

  const ctx = pdfCanvas.getContext('2d');
  pdfRenderTask = page.render({
    canvasContext: ctx,
    viewport: scaledViewport,
  });

  try {
    await pdfRenderTask.promise;
  } catch (e) {
    if (e && e.message && e.message.includes('cancel')) {
      // Ignore cancellation errors from rapid page changes.
      return;
    }
    console.error('[PDF.js] render error:', e);
  }

  pdfPageNumEl.textContent = `Page ${pdfPageNum} / ${pdfDoc.numPages}`;
}

function jumpToPage(page) {
  if (!pdfDoc) return;
  if (page < 1 || page > pdfDoc.numPages) return;
  pdfPageNum = page;
  renderPdfPage();
}

function changePage(delta) {
  if (!pdfDoc) return;
  pdfPageNum += delta;
  if (pdfPageNum < 1) pdfPageNum = 1;
  if (pdfPageNum > pdfDoc.numPages) pdfPageNum = pdfDoc.numPages;
  renderPdfPage();
}

async function loadPdfByDocId(docId, source) {
  try {
    // Prefer uploaded files, then fall back to raw dataset subdirectories.
    let path = await resolvePdfUrlWithCase('/uploads/' + docId);
    try {
      const test = await fetch(path, { method: 'HEAD' });
      if (!test.ok) throw new Error('not in uploads');
    } catch {
      path = null;
      for (const domain of RAW_DOMAINS) {
        const candidate = await resolvePdfUrlWithCase('/raw/' + domain + '/' + docId);
        try {
          const test = await fetch(candidate, { method: 'HEAD' });
          if (test.ok) { path = candidate; break; }
        } catch {}
      }
      if (!path) {
        path = await resolvePdfUrlWithCase('/raw/financial_reports/' + docId);
      }
    }
    await loadPdf(path);
  } catch {}
}

// ── Resizable Columns ──
function setupResizers() {
  const leftResizer = document.getElementById('resizerLeft');
  const rightResizer = document.getElementById('resizerRight');
  const leftPanel = document.getElementById('filePanel');
  const rightPanel = document.getElementById('pdfPanel');

  let resizing = null;
  let startX = 0;
  let startWidth = 0;

  function startResize(e, panel, side) {
    resizing = { panel, side, startX: e.clientX, startWidth: panel.offsetWidth };
    document.addEventListener('mousemove', onResize);
    document.addEventListener('mouseup', stopResize);
    e.target.classList.add('active');
  }

  function onResize(e) {
    if (!resizing) return;
    const dx = e.clientX - resizing.startX;
    const newWidth = resizing.startWidth + (resizing.side === 'left' ? dx : -dx);
    if (newWidth >= 150) {
      resizing.panel.style.width = newWidth + 'px';
    }
  }

  function stopResize() {
    resizing = null;
    document.removeEventListener('mousemove', onResize);
    document.removeEventListener('mouseup', stopResize);
    leftResizer.classList.remove('active');
    rightResizer.classList.remove('active');
    if (pdfDoc) renderPdfPage();
  }

  leftResizer.addEventListener('mousedown', (e) => startResize(e, leftPanel, 'left'));
  rightResizer.addEventListener('mousedown', (e) => startResize(e, rightPanel, 'right'));
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}