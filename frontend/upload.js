const API_BASE = window.location.origin;

const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileList = document.getElementById('fileList');
const submitBtn = document.getElementById('submitBtn');
const status = document.getElementById('status');

let selectedFiles = [];

function formatSize(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function updateFileList() {
  fileList.innerHTML = '';
  if (selectedFiles.length === 0) {
    fileList.innerHTML = '<p class="empty">No files selected</p>';
    submitBtn.disabled = true;
    return;
  }

  selectedFiles.forEach((file, index) => {
    const div = document.createElement('div');
    div.className = 'file-item';
    div.innerHTML = `
      <span class="name">${escapeHtml(file.name)} <span class="size">${formatSize(file.size)}</span></span>
      <button class="remove" title="Remove" data-index="${index}">×</button>
    `;
    fileList.appendChild(div);
  });

  fileList.querySelectorAll('.remove').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const idx = parseInt(e.target.dataset.index);
      selectedFiles.splice(idx, 1);
      updateFileList();
    });
  });

  submitBtn.disabled = false;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function addFiles(fileList) {
  const valid = Array.from(fileList).filter(f => {
    const ext = f.name.split('.').pop().toLowerCase();
    return ext === 'pdf' || ext === 'html';
  });

  if (valid.length !== fileList.length) {
    showStatus('Only PDF and HTML files are allowed.', 'error');
  }

  selectedFiles = selectedFiles.concat(valid);
  updateFileList();
}

dropZone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  addFiles(fileInput.files);
  fileInput.value = '';
});

dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  addFiles(e.dataTransfer.files);
});

function showStatus(message, type) {
  status.textContent = message;
  status.className = 'status ' + (type || '');
}

submitBtn.addEventListener('click', async () => {
  if (selectedFiles.length === 0) return;

  submitBtn.disabled = true;
  showStatus('Uploading files...', 'info');

  const formData = new FormData();
  selectedFiles.forEach(file => formData.append('files', file));

  try {
    const resp = await fetch(`${API_BASE}/upload`, {
      method: 'POST',
      body: formData,
    });

    const data = await resp.json();

    if (!resp.ok || !data.ok) {
      throw new Error(data.detail || data.error || 'Upload failed');
    }

    const meta = (data.metadata && data.metadata[0]) || {};
    const displayName = meta.display_name || meta.generated_filename || data.files[0];
    const summary = meta.summary || '';

    let statusMsg = `Upload successful! Named: ${escapeHtml(displayName)}`;
    if (summary) {
      statusMsg += `\nSummary: ${escapeHtml(summary)}`;
    }
    statusMsg += '\nRedirecting to dialogue...';

    showStatus(statusMsg, 'success');
    setTimeout(() => {
      window.location.href = data.redirect || '/';
    }, 2500);
  } catch (e) {
    showStatus('Error: ' + e.message, 'error');
    submitBtn.disabled = false;
  }
});
