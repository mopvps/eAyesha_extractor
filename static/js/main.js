function handleFileSelect(input) {
  const label = document.getElementById('file-label');
  const dropzone = document.getElementById('dropzone');
  if (input.files && input.files[0]) {
    const file = input.files[0];
    const sizeKB = Math.round(file.size / 1024);
    label.innerText = `📄 ${file.name} (${sizeKB} KB)`;
    dropzone.classList.add('has-file');
    setStatus(`Loaded: ${file.name}`, 'success');
  } else {
    label.innerText = '📁 Choose or Drop PDF Document';
    dropzone.classList.remove('has-file');
  }
}

function handleCategoryChange() {
  const cat = document.getElementById('matter_category').value;
  const sectionSelect = document.getElementById('matter_section');
  const endPageWrap = document.getElementById('end_page_wrap');
  const pageLabel = document.getElementById('page-label-text');

  // Clear existing options
  sectionSelect.innerHTML = '';

  const options = {
    'chapter': [
      { value: 'Body Matter: Full Chapter', text: 'Full Chapter / Continuous Body Matter' },
      { value: 'Body Matter: Single Scene / Section', text: 'Single Scene / Section' }
    ],
    'fm': [
      { value: 'Cover / Full Page Art', text: 'Cover / Full Page Art' },
      { value: 'Title Page', text: 'Title Page' },
      { value: 'Half Title', text: 'Half Title' },
      { value: 'Copyright', text: 'Copyright Page' },
      { value: 'Contents / TOC', text: 'Table of Contents (TOC)' },
      { value: 'Dedication / Epigraph', text: 'Dedication / Epigraph' },
      { value: 'Introduction', text: 'Introduction / Foreword' }
    ],
    'back': [
      { value: 'Bibliography / Further Reading', text: 'Bibliography / Further Reading' },
      { value: 'Notes / Endnotes', text: 'Notes / Endnotes' },
      { value: 'Photo Gallery / Cover', text: 'Photo Gallery / Full Page Art' },
      { value: 'Acknowledgments', text: 'Acknowledgments (with Signature Box)' },
      { value: 'About the Author', text: 'About the Author' },
      { value: 'Numbered List (<ol>)', text: 'Numbered Ordered List (<ol>)' }
    ]
  };

  const currentOpts = options[cat] || [];
  currentOpts.forEach(opt => {
    const el = document.createElement('option');
    el.value = opt.value;
    el.textContent = opt.text;
    sectionSelect.appendChild(el);
  });

  if (cat === 'chapter') {
    endPageWrap.style.display = 'block';
    pageLabel.innerText = '3. Page Range (Continuous Multi-Page)';
  } else {
    endPageWrap.style.display = 'none';
    pageLabel.innerText = '3. PDF Page Number (Single Page)';
  }
}

function setStatus(msg, type = '') {
  const bar = document.getElementById('status-bar');
  bar.innerText = msg;
  bar.className = 'status-bar ' + type;
}

async function runConversion() {
  const fileInput = document.getElementById('pdf_file');
  if (!fileInput.files || !fileInput.files[0]) {
    alert('Please choose or upload a PDF file first.');
    return;
  }

  const runBtn = document.getElementById('run-btn');
  const outputCode = document.getElementById('output-code');
  runBtn.disabled = true;
  setStatus('Processing PDF & compiling PRH XHTML...', '');

  const formData = new FormData();
  formData.append('pdf', fileInput.files[0]);
  formData.append('matter_type', document.getElementById('matter_section').value);
  formData.append('matter_category', document.getElementById('matter_category').value);
  formData.append('book_title', document.getElementById('book_title').value);
  formData.append('isbn', document.getElementById('isbn').value);
  formData.append('start_page', document.getElementById('start_page').value);
  formData.append('end_page', document.getElementById('end_page').value);

  try {
    const response = await fetch('/convert', {
      method: 'POST',
      body: formData
    });

    const data = await response.json();
    if (data.success) {
      outputCode.value = data.xhtml;
      document.getElementById('preview-title').innerText = `Generated: ${data.section_title}`;
      document.getElementById('stat-lines').innerText = `${data.xhtml.split('\n').length} lines`;
      document.getElementById('stat-chars').innerText = `${data.xhtml.length.toLocaleString()} characters`;
      setStatus('Conversion complete! Valid PRH XHTML generated.', 'success');
    } else {
      alert('Error during conversion:\n' + data.error);
      setStatus('Conversion error: ' + data.error, 'error');
    }
  } catch (err) {
    alert('Request failed: ' + err);
    setStatus('Network or Server Error', 'error');
  } finally {
    runBtn.disabled = false;
  }
}

function toggleWrap() {
  const editor = document.getElementById('output-code');
  const btn = document.getElementById('wrap-toggle-btn');
  if (editor.classList.contains('nowrap')) {
    editor.classList.remove('nowrap');
    btn.innerText = '↩ Word Wrap: ON';
  } else {
    editor.classList.add('nowrap');
    btn.innerText = '➡ Word Wrap: OFF';
  }
}

function copyCode() {
  const code = document.getElementById('output-code').value;
  if (!code) {
    alert('No XHTML code to copy yet.');
    return;
  }
  navigator.clipboard.writeText(code).then(() => {
    setStatus('XHTML copied to clipboard!', 'success');
  });
}

function downloadFile() {
  const code = document.getElementById('output-code').value;
  if (!code) {
    alert('No XHTML code to download yet.');
    return;
  }
  const sectionName = document.getElementById('matter_section').value.replace(/[^a-z0-9]/gi, '_').toLowerCase();
  const blob = new Blob([code], { type: 'application/xhtml+xml;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${sectionName}.xhtml`;
  a.click();
  setStatus(`Saved ${sectionName}.xhtml`, 'success');
}

// Drag & drop & click handlers
document.addEventListener('DOMContentLoaded', () => {
  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('pdf_file');

  // Single clean click handler: clicking dropzone opens file picker
  dropzone.addEventListener('click', (e) => {
    if (e.target !== fileInput) {
      fileInput.click();
    }
  });

  fileInput.addEventListener('change', () => {
    handleFileSelect(fileInput);
  });

  ['dragenter', 'dragover'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.add('has-file');
    }, false);
  });

  ['dragleave', 'drop'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.remove('has-file');
    }, false);
  });

  dropzone.addEventListener('drop', (e) => {
    const dt = e.dataTransfer;
    const files = dt.files;
    if (files && files.length > 0) {
      fileInput.files = files;
      handleFileSelect(fileInput);
    }
  });

  // Initial category setup
  handleCategoryChange();
});

