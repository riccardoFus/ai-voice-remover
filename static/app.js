/* AI Voice Remover · frontend */
(() => {
  'use strict';

  // ── Configurazione dal server ──────────────────────────────────────────────
  const body = document.body;
  const parseJson = (value, fallback) => {
    try { return JSON.parse(value); } catch { return fallback; }
  };

  const CFG = {
    maxMb: Number(body.dataset.maxMb) || 250,
    retentionHours: Number(body.dataset.retention) || 24,
    models: parseJson(body.dataset.modelInfo, {}),
    qualities: parseJson(body.dataset.qualityInfo, {}),
    formats: parseJson(body.dataset.formatInfo, {}),
    defaultModel: body.dataset.defaultModel || 'htdemucs_ft',
    denoise: parseJson(body.dataset.denoiseInfo, {}),
    denoiseFormats: parseJson(body.dataset.denoiseFormats, {}),
    denoiseAvailable: body.dataset.denoiseAvailable === '1',
    denoiseModel: body.dataset.denoiseModel || 'DeepFilterNet3',
    defaultDenoise: body.dataset.defaultDenoise || 'natural',
    polish: parseJson(body.dataset.polishInfo, {}),
    defaultPolish: body.dataset.defaultPolish || 'medium',
  };

  const ALLOWED_EXT = ['mp3', 'wav', 'flac', 'ogg', 'oga', 'm4a', 'aac', 'wma', 'opus', 'aiff', 'aif', 'alac', 'mp4', 'webm'];
  const VIDEO_EXT = ['mp4', 'm4v', 'mov', 'mkv', 'webm', 'avi'];
  const DENOISE_EXT = [...new Set([...ALLOWED_EXT, ...VIDEO_EXT])];
  const ACTIVE = new Set(['queued', 'processing']);

  const STAGE_LABELS = {
    preparing: 'Preparazione',
    loading: 'Caricamento',
    loading_model: 'Caricamento modello',
    separating: 'Separazione',
    merging: 'Unione stem',
    exporting: 'Salvataggio',
    analyzing: 'Analisi',
    cleaning: 'Pulizia',
    hum: 'Ronzio',
    done: 'Completato',
    queued: 'In coda',
    error: 'Errore',
    cancelled: 'Annullato',
  };

  const STEM_META = {
    instrumental: { icon: 'wave', accent: 'violet' },
    vocals: { icon: 'music', accent: 'pink' },
    drums: { icon: 'drums', accent: 'amber' },
    bass: { icon: 'headphones', accent: 'cyan' },
    other: { icon: 'sparkle', accent: 'violet' },
    guitar: { icon: 'guitar', accent: 'pink' },
    piano: { icon: 'piano', accent: 'cyan' },
  };

  // ── DOM ────────────────────────────────────────────────────────────────────
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const els = {
    serverChip: $('#serverChip'),
    modelChip: $('#modelChip'),
    refreshBtn: $('#refreshBtn'),
    dropzone: $('#dropzone'),
    fileInput: $('#fileInput'),
    chooseBtn: $('#chooseBtn'),
    modeSeparate: $('#modeSeparate'),
    modeDenoise: $('#modeDenoise'),
    uploadTitle: $('#uploadTitle'),
    uploadSubtitle: $('#uploadSubtitle'),
    dropTitle: $('#dropTitle'),
    dropFormats: $('#dropFormats'),
    separateOptions: $('[data-options="separate"]'),
    denoiseOptions: $('[data-options="denoise"]'),
    modelSelect: $('#modelSelect'),
    qualitySelect: $('#qualitySelect'),
    formatSelect: $('#formatSelect'),
    strengthSelect: $('#strengthSelect'),
    denoiseFormatSelect: $('#denoiseFormatSelect'),
    modelHint: $('#modelHint'),
    qualityHint: $('#qualityHint'),
    formatHint: $('#formatHint'),
    strengthHint: $('#strengthHint'),
    denoiseFormatHint: $('#denoiseFormatHint'),
    denoiseUnavailable: $('#denoiseUnavailable'),
    uploadHint: $('#uploadHint'),
    jobList: $('#jobList'),
    jobCount: $('#jobCount'),
    emptyQueue: $('#emptyQueue'),
    processingPanel: $('#processingPanel'),
    procStage: $('#procStage'),
    procName: $('#procName'),
    procMessage: $('#procMessage'),
    procPercent: $('#procPercent'),
    procBar: $('#procBar'),
    procTrack: $('#procTrack'),
    procElapsed: $('#procElapsed'),
    procEta: $('#procEta'),
    procEtaWrap: $('#procEtaWrap'),
    procOptions: $('#procOptions'),
    cancelBtn: $('#cancelBtn'),
    retryBtn: $('#retryBtn'),
    resultsPanel: $('#resultsPanel'),
    resultEyebrow: $('#resultEyebrow'),
    resultName: $('#resultName'),
    resultMeta: $('#resultMeta'),
    polishBtn: $('#polishBtn'),
    compareBtn: $('#compareBtn'),
    zipBtn: $('#zipBtn'),
    deleteJobBtn: $('#deleteJobBtn'),
    denoiseResult: $('#denoiseResult'),
    playerClean: $('#playerClean'),
    cleanVideo: $('#cleanVideo'),
    cleanVideoWrap: $('#cleanVideoWrap'),
    cleanTitle: $('#cleanTitle'),
    cleanSubtitle: $('#cleanSubtitle'),
    denoiseNote: $('#denoiseNote'),
    separatedFeatured: $('#separatedFeatured'),
    playerInstrumental: $('#playerInstrumental'),
    playerVocals: $('#playerVocals'),
    extraStemsWrap: $('#extraStemsWrap'),
    extrasToggle: $('#extrasToggle'),
    extraStems: $('#extraStems'),
    footerDevice: $('#footerDevice'),
    toasts: $('#toasts'),
    confirmDialog: $('#confirmDialog'),
    confirmTitle: $('#confirmTitle'),
    confirmText: $('#confirmText'),
    confirmOk: $('#confirmOk'),
    polishDialog: $('#polishDialog'),
    polishMode: $('#polishMode'),
    polishStrength: $('#polishStrength'),
    polishHum: $('#polishHum'),
    polishFormat: $('#polishFormat'),
    polishHint: $('#polishHint'),
    polishConfirm: $('#polishConfirm'),
  };

  const state = {
    jobs: new Map(),
    order: [],
    rows: new Map(),
    uploads: [],
    uploadQueue: [],
    uploadRunning: false,
    selectedId: null,
    renderedResultId: null,
    players: [],
    instrumentalPlayer: null,
    compareHandler: null,
    mode: 'separate',
    server: null,
    online: null,
    refreshing: false,
    pollTimer: null,
    renderOrder: '',
    lastStatuses: new Map(),
    initialSelectionDone: false,
  };

  // ── Utility ────────────────────────────────────────────────────────────────
  const icon = (name, cls = '') =>
    `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function fmtTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    seconds = Math.round(seconds);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return h > 0
      ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${m}:${String(s).padStart(2, '0')}`;
  }

  function fmtHuman(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return '—';
    seconds = Math.round(seconds);
    if (seconds < 60) return `${seconds} s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
    const h = Math.floor(m / 60);
    return `${h} h ${m % 60} min`;
  }

  function fmtSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '';
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function truncate(text, length = 42) {
    if (!text) return '';
    text = String(text).replace(/\s+/g, ' ').trim();
    return text.length > length ? text.slice(0, length - 1) + '…' : text;
  }

  function shortDevice(server) {
    if (!server) return 'Server';
    if (server.device === 'cpu') return 'CPU';
    const name = (server.device_name || '').replace(/^NVIDIA\s+/i, '');
    return name ? `GPU · ${truncate(name, 24)}` : 'GPU attiva';
  }

  function toast(message, type = 'info', duration = 5200) {
    const box = el('div', `toast ${type}`);
    const iconName = type === 'error' ? 'alert' : type === 'success' ? 'check' : 'sparkle';
    box.innerHTML = icon(iconName);
    const text = el('span', '', message);
    box.appendChild(text);
    els.toasts.appendChild(box);
    while (els.toasts.children.length > 3) {
      els.toasts.firstElementChild.remove();
    }
    window.setTimeout(() => {
      box.classList.add('hide');
      window.setTimeout(() => box.remove(), 260);
    }, duration);
  }

  function confirmAction({ title, text, okLabel = 'Elimina' }) {
    return new Promise((resolve) => {
      els.confirmTitle.textContent = title;
      els.confirmText.textContent = text;
      els.confirmOk.textContent = okLabel;
      const onClose = () => {
        els.confirmDialog.removeEventListener('close', onClose);
        resolve(els.confirmDialog.returnValue === 'confirm');
      };
      els.confirmDialog.addEventListener('close', onClose);
      els.confirmDialog.returnValue = 'cancel';
      els.confirmDialog.showModal();
    });
  }

  function currentOptions() {
    if (state.mode === 'denoise') {
      return {
        kind: 'denoise',
        strength: els.strengthSelect.value,
        format: els.denoiseFormatSelect.value,
      };
    }
    return {
      kind: 'separate',
      model: els.modelSelect.value,
      quality: els.qualitySelect.value,
      format: els.formatSelect.value,
    };
  }

  // ── Modalità: separazione / denoise ────────────────────────────────────────
  function setMode(mode, { persist = true } = {}) {
    if (mode === 'denoise' && !CFG.denoiseAvailable) {
      toast('Motore denoise non installato. Esegui ./install.sh --denoise.', 'error', 7000);
      return;
    }
    state.mode = mode;
    const isDenoise = mode === 'denoise';

    els.modeSeparate.classList.toggle('is-active', !isDenoise);
    els.modeDenoise.classList.toggle('is-active', isDenoise);
    els.modeSeparate.setAttribute('aria-selected', String(!isDenoise));
    els.modeDenoise.setAttribute('aria-selected', String(isDenoise));
    els.separateOptions.hidden = isDenoise;
    els.denoiseOptions.hidden = !isDenoise;

    if (isDenoise) {
      els.uploadTitle.textContent = 'Pulisci una clip';
      els.uploadSubtitle.textContent = 'Audio o video: l’AI riduce il rumore mantenendo il timbro.';
      els.dropTitle.textContent = 'Trascina qui audio o clip video';
      els.dropFormats.textContent = 'MP3 · WAV · FLAC · M4A · MP4 · MOV · WEBM · MKV';
      els.fileInput.accept = '.mp3,.wav,.flac,.ogg,.oga,.m4a,.aac,.wma,.opus,.aiff,.aif,.alac,.mp4,.m4v,.mov,.mkv,.webm,.avi,audio/*,video/*';
      els.uploadHint.textContent = 'Elaborazione locale con DeepFilterNet3. Per i video lo stream video resta intatto; l’audio viene ricodificato in AAC 256 kbps.';
    } else {
      els.uploadTitle.textContent = 'Carica un brano';
      els.uploadSubtitle.textContent = 'Seleziona uno o più file audio. Puoi accodare più brani.';
      els.dropTitle.textContent = 'Trascina qui i tuoi brani';
      els.dropFormats.textContent = 'MP3 · WAV · FLAC · M4A · OGG · OPUS · AAC';
      els.fileInput.accept = '.mp3,.wav,.flac,.ogg,.oga,.m4a,.aac,.wma,.opus,.aiff,.aif,.alac,.mp4,.webm,audio/*';
      els.uploadHint.textContent = `I file restano sul tuo computer e vengono rimossi automaticamente dopo ${CFG.retentionHours} ore.`;
    }

    if (persist) {
      try { localStorage.setItem('avr-mode', mode); } catch { /* noop */ }
    }
    updateHints();
  }

  // ── Hint delle opzioni ─────────────────────────────────────────────────────
  function updateHints() {
    if (state.mode === 'denoise') {
      const strength = CFG.denoise[els.strengthSelect.value];
      const format = CFG.denoiseFormats[els.denoiseFormatSelect.value];
      if (strength) {
        els.strengthHint.textContent = `${strength.description} · fino a ${strength.atten_db} dB`;
      }
      if (format) els.denoiseFormatHint.textContent = format.description || '';
      els.denoiseUnavailable.hidden = CFG.denoiseAvailable;
      updateModelChip();
      return;
    }

    const model = CFG.models[els.modelSelect.value];
    const quality = CFG.qualities[els.qualitySelect.value];
    const format = CFG.formats[els.formatSelect.value];
    if (model) {
      let hint = model.description || '';
      const cached = state.server?.models_cached?.[els.modelSelect.value];
      if (state.server && !cached) hint += ' · pesi scaricati al primo uso';
      els.modelHint.textContent = hint;
    }
    if (quality) els.qualityHint.textContent = quality.description || '';
    if (format) els.formatHint.textContent = format.description || '';
    updateModelChip();
  }

  function updateModelChip() {
    if (!state.server) {
      els.modelChip.hidden = true;
      return;
    }
    const use = els.modelChip.querySelector('use');
    if (state.mode === 'denoise') {
      const engine = state.server.denoise || {};
      const ready = Boolean(engine.available);
      const cached = Boolean(engine.model_cached);
      els.modelChip.hidden = false;
      els.modelChip.classList.toggle('subtle', !ready);
      els.modelChip.querySelector('span').textContent = !ready
        ? 'Denoise non installato'
        : cached
        ? `${CFG.denoiseModel} pronto`
        : `${CFG.denoiseModel} · download al primo uso`;
      if (use) use.setAttribute('href', !ready ? '#i-alert' : cached ? '#i-check' : '#i-download');
      return;
    }
    const modelKey = els.modelSelect.value;
    const model = CFG.models[modelKey];
    const cached = state.server.models_cached?.[modelKey];
    els.modelChip.hidden = false;
    els.modelChip.classList.toggle('subtle', !cached);
    els.modelChip.querySelector('span').textContent = cached
      ? `${model?.label || modelKey} pronto`
      : `${model?.label || modelKey} · download al primo uso`;
    if (use) use.setAttribute('href', cached ? '#i-check' : '#i-download');
  }

  // ── Server / stato ─────────────────────────────────────────────────────────
  function applyServer(server) {
    if (!server) return;
    state.server = server;
    const ok = server.demucs && server.torch && server.ffmpeg;
    const dot = els.serverChip.querySelector('.dot');
    dot.className = `dot ${ok ? 'ok' : 'warn'}`;
    els.serverChip.querySelector('span').textContent = shortDevice(server);
    els.serverChip.title = `${server.device_label}${server.device_name ? ' — ' + server.device_name : ''}`;
    if (els.footerDevice) els.footerDevice.textContent = server.device_label;

    const denoiseAvailable = Boolean(server.denoise?.available);
    if (CFG.denoiseAvailable !== denoiseAvailable) {
      CFG.denoiseAvailable = denoiseAvailable;
      if (!denoiseAvailable && state.mode === 'denoise') setMode('separate', { persist: false });
    }
    els.modeDenoise.disabled = !denoiseAvailable;
    els.modeDenoise.title = denoiseAvailable
      ? 'Denoise AI conservativo per voce e parlato'
      : 'Motore denoise non installato: ./install.sh --denoise';
    els.denoiseUnavailable.hidden = denoiseAvailable;

    updateModelChip();

    if (!ok && state.online !== false) {
      const warning = server.warning || 'Alcune dipendenze non sono disponibili.';
      toast(warning, 'error', 8000);
    }
  }

  function setOffline() {
    const dot = els.serverChip.querySelector('.dot');
    dot.className = 'dot err';
    els.serverChip.querySelector('span').textContent = 'Server offline';
    if (state.online !== false) {
      toast('Impossibile contattare il server. Verifica che la web app sia ancora in esecuzione.', 'error', 8000);
    }
    state.online = false;
  }

  // ── Coda ───────────────────────────────────────────────────────────────────
  const STATUS_ICON = {
    queued: 'clock',
    processing: 'wave',
    completed: 'check',
    error: 'alert',
    cancelled: 'x',
    uploading: 'upload',
  };

  function actionSignature(job) {
    if (job.status === 'completed') return 'completed';
    if (job.status === 'error' || job.status === 'cancelled') return 'retry';
    if (ACTIVE.has(job.status)) return 'active';
    return 'none';
  }

  function buildJobRow(key) {
    const row = el('div', 'job');
    row.dataset.key = key;

    const iconBox = el('div', 'job-icon');
    const main = el('div', 'job-main');
    const name = el('div', 'job-name');
    const meta = el('div', 'job-meta');
    const status = el('span', 'js-status');
    const sep = el('span', 'sep', '·');
    const sub = el('span', 'js-sub');
    meta.append(status, sep, sub);
    const bar = el('div', 'job-bar');
    const fill = el('span');
    bar.appendChild(fill);
    main.append(name, meta, bar);

    const actions = el('div', 'job-actions');
    row.append(iconBox, main, actions);

    row._refs = { iconBox, name, status, sub, bar, fill, actions };
    return row;
  }

  function uploadRowKey(key) { return `upload:${key}`; }
  function jobRowKey(id) { return `job:${id}`; }

  function buildUploadRow(key) {
    const row = buildJobRow(uploadRowKey(key));
    row.classList.add('status-uploading');
    row.dataset.kind = 'upload';
    row._refs.iconBox.innerHTML = icon('upload');
    row._refs.actions.remove();
    row._refs.bar.hidden = false;
    return row;
  }

  function updateUploadRow(row, item) {
    const refs = row._refs;
    if (row.dataset.mode !== item.mode) {
      row.dataset.mode = item.mode || 'separate';
      refs.iconBox.innerHTML = icon(item.mode === 'denoise' ? 'sparkle' : 'upload');
    }
    refs.name.textContent = item.name;
    refs.status.textContent = item.mode === 'denoise' ? 'Denoise AI' : 'Caricamento';
    refs.status.className = 'js-status status-queued';
    refs.sub.textContent = `${item.progress || 0}%`;
    refs.fill.style.width = `${item.progress || 0}%`;
  }

  function jobSubtitle(job) {
    if (job.status === 'queued') {
      return job.queue_position ? `#${job.queue_position} in coda` : 'In attesa';
    }
    if (job.status === 'processing') {
      const stage = STAGE_LABELS[job.stage] || (
        job.kind === 'denoise' ? 'Pulizia AI'
        : job.kind === 'polish' ? 'Pulizia stem'
        : 'Elaborazione'
      );
      return `${stage} · ${Math.round(job.progress || 0)}%`;
    }
    if (job.status === 'completed') {
      const dur = job.duration ? fmtTime(job.duration) : '';
      if (job.kind === 'polish') {
        return [dur, job.quality_label].filter(Boolean).join(' · ') || 'Stem pulito';
      }
      return [dur, job.format_label].filter(Boolean).join(' · ') || 'Pronto';
    }
    if (job.status === 'error') return truncate(job.error || job.message || 'Errore', 36);
    if (job.status === 'cancelled') return 'Annullato';
    return job.message || '';
  }

  function actionsMarkup(job) {
    const sig = actionSignature(job);
    if (sig === 'active') {
      return `<button class="job-action danger" data-action="cancel" title="Annulla" aria-label="Annulla">${icon('x')}</button>`;
    }
    if (sig === 'completed') {
      return `<button class="job-open" data-action="open">Apri</button>
        <button class="job-action danger" data-action="delete" title="Elimina" aria-label="Elimina">${icon('trash')}</button>`;
    }
    if (sig === 'retry') {
      return `<button class="job-action" data-action="retry" title="Riprova" aria-label="Riprova">${icon('refresh')}</button>
        <button class="job-action danger" data-action="delete" title="Elimina" aria-label="Elimina">${icon('trash')}</button>`;
    }
    return '';
  }

  function updateJobRow(row, job) {
    const refs = row._refs;
    row.className = `job status-${job.status}${job.id === state.selectedId ? ' selected' : ''}`;
    row.dataset.id = job.id;

    const iconName =
      (job.kind === 'denoise' || job.kind === 'polish') && job.status === 'processing'
        ? 'sparkle'
        : STATUS_ICON[job.status] || 'music';
    if (row.dataset.status !== job.status || row.dataset.kind !== job.kind) {
      row.dataset.status = job.status;
      row.dataset.kind = job.kind || 'separate';
      refs.iconBox.innerHTML = icon(iconName);
    }
    if (row.dataset.actions !== actionSignature(job)) {
      row.dataset.actions = actionSignature(job);
      refs.actions.innerHTML = actionsMarkup(job);
    }

    refs.name.textContent = job.name;
    refs.name.title = job.name;
    refs.status.textContent =
      job.status === 'processing'
        ? (job.kind === 'polish' ? 'Pulizia stem' : job.kind === 'denoise' ? 'Denoise AI' : 'Elaborazione')
        : job.status === 'queued'
        ? 'In coda'
        : job.status === 'completed'
        ? (job.kind === 'denoise' ? 'Pulito' : job.kind === 'polish' ? 'Stem pulito' : 'Completato')
        : job.status === 'error'
        ? 'Errore'
        : 'Annullato';
    refs.status.className = `js-status status-${job.status}`;
    refs.sub.textContent = jobSubtitle(job);

    if (ACTIVE.has(job.status)) {
      refs.bar.hidden = false;
      refs.fill.style.width = `${Math.max(2, job.progress || 0)}%`;
    } else {
      refs.bar.hidden = true;
    }
  }

  function renderJobs() {
    const wanted = new Set();
    const desiredOrder = [];

    for (const upload of state.uploads) {
      const key = uploadRowKey(upload.key);
      wanted.add(key);
      desiredOrder.push(key);
      let row = state.rows.get(key);
      if (!row) {
        row = buildUploadRow(upload.key);
        state.rows.set(key, row);
      }
      updateUploadRow(row, upload);
    }

    for (const id of state.order) {
      const job = state.jobs.get(id);
      if (!job) continue;
      const key = jobRowKey(id);
      wanted.add(key);
      desiredOrder.push(key);
      let row = state.rows.get(key);
      if (!row) {
        row = buildJobRow(key);
        state.rows.set(key, row);
      }
      updateJobRow(row, job);
    }

    for (const [key, row] of Array.from(state.rows.entries())) {
      if (!wanted.has(key)) {
        row.remove();
        state.rows.delete(key);
      }
    }

    const signature = desiredOrder.join('|');
    if (state.renderOrder !== signature) {
      state.renderOrder = signature;
      for (const key of desiredOrder) {
        const row = state.rows.get(key);
        if (row) els.jobList.appendChild(row);
      }
    }

    const total = state.jobs.size + state.uploads.length;
    els.jobCount.textContent = String(total);
    els.emptyQueue.hidden = total > 0;
    els.jobList.hidden = total === 0;
  }

  // ── Sincronizzazione job ───────────────────────────────────────────────────
  function syncJobs(jobs) {
    const next = new Map();
    const order = [];
    for (const job of jobs) {
      next.set(job.id, job);
      order.push(job.id);
    }

    const previous = state.jobs;
    state.jobs = next;
    state.order = order;

    // Notifiche per transizioni di stato (dopo il primo caricamento).
    if (state.initialSelectionDone) {
      for (const [id, job] of next) {
        const before = previous.get(id);
        const oldStatus = state.lastStatuses.get(id);
        if (!before && !oldStatus) continue;
        if (oldStatus && oldStatus !== job.status) {
          if (job.status === 'completed') {
            toast(`«${job.name}» è pronto`, 'success');
          } else if (job.status === 'error') {
            toast(`«${job.name}»: ${truncate(job.error || job.message || 'errore', 90)}`, 'error', 7000);
          }
        }
      }
    }
    state.lastStatuses = new Map(Array.from(next, ([id, job]) => [id, job.status]));

    renderJobs();
    updateSelected();
  }

  function selectJob(id, { scroll = false, hash = true } = {}) {
    if (!state.jobs.has(id)) return;
    state.selectedId = id;
    state.renderedResultId = null;
    destroyPlayers();
    for (const row of state.rows.values()) row.classList.remove('selected');
    const row = state.rows.get(jobRowKey(id));
    if (row) row.classList.add('selected');
    if (hash) {
      const value = `#job=${id}`;
      if (location.hash !== value) {
        history.replaceState(null, '', `${location.pathname}${location.search}${value}`);
      }
    }
    updateSelected();
    if (scroll) {
      const target = state.jobs.get(id)?.status === 'completed' ? els.resultsPanel : els.processingPanel;
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function clearSelection() {
    state.selectedId = null;
    state.renderedResultId = null;
    destroyPlayers();
    els.processingPanel.hidden = true;
    els.resultsPanel.hidden = true;
    if (location.hash) history.replaceState(null, '', `${location.pathname}${location.search}`);
  }

  function updateSelected() {
    if (!state.selectedId) {
      els.processingPanel.hidden = true;
      els.resultsPanel.hidden = true;
      return;
    }
    const job = state.jobs.get(state.selectedId);
    if (!job) {
      clearSelection();
      return;
    }
    if (job.status === 'completed') {
      showResults(job);
    } else {
      showProcessing(job);
    }
  }

  // ── Pannello elaborazione ──────────────────────────────────────────────────
  function showProcessing(job) {
    hideResults();
    els.processingPanel.hidden = false;

    const isActive = ACTIVE.has(job.status);
    const isError = job.status === 'error';
    const isCancelled = job.status === 'cancelled';

    els.procStage.textContent = isError
      ? 'Elaborazione non riuscita'
      : isCancelled
      ? 'Job annullato'
      : job.status === 'queued'
      ? 'In attesa'
      : job.kind === 'denoise'
      ? (STAGE_LABELS[job.stage] || 'Pulizia AI')
      : job.kind === 'polish'
      ? (STAGE_LABELS[job.stage] || 'Pulizia stem')
      : STAGE_LABELS[job.stage] || 'Elaborazione';

    els.procName.textContent = job.name;
    els.procName.title = job.name;

    let message = job.message || 'Preparazione…';
    if (isError && job.error) message = job.error;
    els.procMessage.textContent = message;

    const pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
    if (isError) {
      els.procPercent.innerHTML = '!';
    } else if (isCancelled) {
      els.procPercent.innerHTML = '–';
    } else {
      els.procPercent.innerHTML = `${Math.round(pct)}<span>%</span>`;
    }
    els.procBar.style.width = `${isActive ? Math.max(2, pct) : 0}%`;
    els.procTrack.setAttribute('aria-valuenow', String(Math.round(pct)));

    const elapsed = job.started_at
      ? Math.max(0, (Date.now() / 1000) - job.started_at)
      : job.elapsed;
    els.procElapsed.textContent = isActive && job.started_at
      ? fmtHuman(elapsed)
      : job.elapsed
      ? fmtHuman(job.elapsed)
      : '—';

    if (isActive && job.eta_seconds) {
      els.procEta.textContent = `~${fmtHuman(job.eta_seconds)} rimanenti`;
      els.procEtaWrap.hidden = false;
    } else if (isActive && pct < 4) {
      els.procEta.textContent = 'Stima in corso…';
      els.procEtaWrap.hidden = false;
    } else {
      els.procEtaWrap.hidden = true;
    }

    const opts = [job.model_label, job.quality_label, job.format_label].filter(Boolean);
    els.procOptions.textContent = opts.join(' · ');

    els.cancelBtn.hidden = !isActive;
    els.retryBtn.hidden = !(job.retryable || ((isError || isCancelled) && job.has_source));
    els.retryBtn.disabled = false;
    els.cancelBtn.disabled = false;
  }

  function hideResults() {
    if (!els.resultsPanel.hidden) {
      els.resultsPanel.hidden = true;
      state.renderedResultId = null;
      destroyPlayers();
    }
  }

  // ── Risultati ──────────────────────────────────────────────────────────────
  function fileUrl(jobId, key, download = false) {
    return `/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(key)}${download ? '?download=1' : ''}`;
  }

  function peaksUrl(jobId, key) {
    return `/api/jobs/${encodeURIComponent(jobId)}/peaks/${encodeURIComponent(key)}`;
  }

  function showResults(job) {
    if (state.renderedResultId === job.id && !els.resultsPanel.hidden) {
      return;
    }
    els.processingPanel.hidden = true;
    els.resultsPanel.hidden = false;
    state.renderedResultId = job.id;
    destroyPlayers();

    const isDenoise = job.kind === 'denoise';
    const isPolish = job.kind === 'polish';
    const polishMode = job.mode || (job.residual ? 'voice' : 'hum');
    els.resultName.textContent = job.name;
    els.resultName.title = job.name;
    els.resultEyebrow.innerHTML = `${icon('check')} ${
      isDenoise ? 'Pulizia AI completata'
      : isPolish
      ? (polishMode === 'hum' ? 'Ronzio rimosso'
        : polishMode === 'voice' ? 'Residui vocali ridotti'
        : 'Pulizia stem completata')
      : 'Separazione completata'
    }`;

    const meta = els.resultMeta;
    meta.textContent = '';
    const addMeta = (label, value) => {
      if (!value) return;
      const span = el('span');
      span.append(el('b', '', label + ' '), document.createTextNode(String(value)));
      meta.appendChild(span);
    };
    addMeta('Durata', job.duration ? fmtTime(job.duration) : '');
    if (isDenoise) {
      addMeta('Motore', job.model_label);
      addMeta(
        'Intensità',
        [job.quality_label, job.strength_db ? `${job.strength_db} dB` : '']
          .filter(Boolean)
          .join(' · '),
      );
    } else if (isPolish) {
      addMeta('Motore', job.model_label);
      addMeta(polishMode === 'hum' ? 'Ronzio' : 'Residui voce', job.quality_label);
    } else {
      addMeta('Modello', job.model_label);
      addMeta('Qualità', job.quality_label);
    }
    addMeta('Formato', job.format_label);
    addMeta(
      'Elaborato in',
      job.result?.elapsed ? fmtHuman(job.result.elapsed) : job.elapsed ? fmtHuman(job.elapsed) : '',
    );
    addMeta(
      'Dimensione',
      fmtSize(Object.values(job.files || {}).reduce((sum, f) => sum + (f.size || 0), 0)),
    );

    if (isDenoise) renderDenoiseResults(job);
    else if (isPolish) renderPolishResults(job);
    else renderSeparationResults(job);

    els.deleteJobBtn.dataset.id = job.id;
  }

  function renderSeparationResults(job) {
    els.denoiseResult.hidden = true;
    els.separatedFeatured.hidden = false;

    const files = job.files || {};
    const instrumental = files.instrumental;
    const vocals = files.vocals;

    const instCard = els.playerInstrumental.closest('.stem-card');
    if (instrumental) {
      instCard.hidden = false;
      state.instrumentalPlayer = new WavePlayer(els.playerInstrumental, {
        name: 'Strumentale',
        url: fileUrl(job.id, 'instrumental'),
        downloadUrl: fileUrl(job.id, 'instrumental', true),
        downloadName: instrumental.name,
        peaksUrl: peaksUrl(job.id, 'instrumental'),
        duration: job.duration,
        accent: 'violet',
        alt: job.has_source
          ? {
              name: 'Originale',
              url: `/api/jobs/${encodeURIComponent(job.id)}/source`,
              downloadUrl: `/api/jobs/${encodeURIComponent(job.id)}/source?download=1`,
              downloadName: job.name,
              peaksUrl: peaksUrl(job.id, 'original'),
            }
          : null,
      });
      state.players.push(state.instrumentalPlayer);
    } else {
      instCard.hidden = true;
      state.instrumentalPlayer = null;
    }

    const vocalsCard = els.playerVocals.closest('.stem-card');
    if (vocals) {
      vocalsCard.hidden = false;
      const player = new WavePlayer(els.playerVocals, {
        name: 'Voce',
        url: fileUrl(job.id, 'vocals'),
        downloadUrl: fileUrl(job.id, 'vocals', true),
        downloadName: vocals.name,
        peaksUrl: peaksUrl(job.id, 'vocals'),
        duration: job.duration,
        accent: 'pink',
      });
      state.players.push(player);
    } else {
      vocalsCard.hidden = true;
    }

    // Altri stem
    const extras = Object.keys(files).filter((key) => key !== 'instrumental' && key !== 'vocals');
    els.extraStems.textContent = '';
    if (extras.length) {
      els.extraStemsWrap.hidden = false;
      for (const key of extras) {
        const info = files[key];
        const cfg = STEM_META[key] || STEM_META.other;
        const card = el('article', `stem-card mini accent-${cfg.accent}`);
        const head = el('div', 'stem-head');
        const badge = el('span', 'stem-badge');
        badge.innerHTML = icon(cfg.icon);
        const titleBox = el('div');
        titleBox.append(el('h3', '', info.label || key), el('p', '', fmtSize(info.size)));
        head.append(badge, titleBox);
        const slot = el('div', 'player-slot');
        card.append(head, slot);
        els.extraStems.appendChild(card);

        const player = new WavePlayer(slot, {
          name: info.label || key,
          url: fileUrl(job.id, key),
          downloadUrl: fileUrl(job.id, key, true),
          downloadName: info.name,
          peaksUrl: peaksUrl(job.id, key),
          duration: job.duration,
          accent: cfg.accent,
          compact: true,
        });
        state.players.push(player);
      }
    } else {
      els.extraStemsWrap.hidden = true;
    }

    els.compareBtn.hidden = !job.has_source || !instrumental;
    els.compareBtn.setAttribute('aria-pressed', 'false');
    els.compareBtn.querySelector('span').textContent = 'Confronta originale';
    els.polishBtn.hidden = !instrumental;
    state.compareHandler = async () => {
      if (!state.instrumentalPlayer) return false;
      const usingAlt = await state.instrumentalPlayer.toggleAlternative();
      return Boolean(usingAlt);
    };

    els.zipBtn.href = `/api/jobs/${encodeURIComponent(job.id)}/bundle.zip`;
    els.zipBtn.setAttribute('download', '');
    els.zipBtn.querySelector('span').textContent = 'Scarica tutto';
  }

  function renderDenoiseResults(job) {
    els.separatedFeatured.hidden = true;
    els.extraStemsWrap.hidden = true;
    els.extraStems.textContent = '';
    els.denoiseResult.hidden = false;
    els.polishBtn.hidden = true;

    const clean = (job.files || {}).clean;
    const outputMedia = job.result?.output_media || clean?.media || job.output_media || 'audio';
    const db = job.strength_db || CFG.denoise[job.strength]?.atten_db;
    const cleanUrl = fileUrl(job.id, 'clean');
    const sourceUrl = `/api/jobs/${encodeURIComponent(job.id)}/source`;

    els.cleanTitle.textContent = outputMedia === 'video' ? 'Clip video pulita' : 'Clip pulita';
    els.cleanSubtitle.textContent = [
      job.model_label,
      db ? `fino a ${db} dB` : '',
      outputMedia === 'video' ? 'video originale' : job.format_label,
    ]
      .filter(Boolean)
      .join(' · ');

    els.playerClean.hidden = outputMedia === 'video';
    els.cleanVideoWrap.hidden = outputMedia !== 'video';

    if (!clean) {
      els.denoiseNote.textContent = 'Il file pulito non è più disponibile.';
      els.compareBtn.hidden = true;
      return;
    }

    if (outputMedia === 'video') {
      els.cleanVideo.src = cleanUrl;
      els.cleanVideo.dataset.altActive = '0';
      els.cleanVideo.load();
      state.compareHandler = () => {
        const altActive = els.cleanVideo.dataset.altActive === '1';
        const time = els.cleanVideo.currentTime;
        const playing = !els.cleanVideo.paused;
        els.cleanVideo.dataset.altActive = altActive ? '0' : '1';
        els.cleanVideo.src = altActive ? cleanUrl : sourceUrl;
        els.cleanVideo.load();
        els.cleanVideo.addEventListener(
          'loadedmetadata',
          () => {
            const duration = els.cleanVideo.duration;
            els.cleanVideo.currentTime = Number.isFinite(duration)
              ? Math.min(time, duration)
              : time;
            if (playing) els.cleanVideo.play().catch(() => {});
          },
          { once: true },
        );
        els.cleanTitle.textContent = altActive ? 'Clip video pulita' : 'Originale';
        return !altActive;
      };
    } else {
      state.instrumentalPlayer = new WavePlayer(els.playerClean, {
        name: 'Clip pulita',
        url: cleanUrl,
        downloadUrl: fileUrl(job.id, 'clean', true),
        downloadName: clean.name,
        peaksUrl: peaksUrl(job.id, 'clean'),
        duration: job.duration,
        accent: 'cyan',
        alt: job.has_source
          ? {
              name: 'Originale',
              url: sourceUrl,
              downloadUrl: `${sourceUrl}?download=1`,
              downloadName: job.name,
              peaksUrl: peaksUrl(job.id, 'original'),
            }
          : null,
      });
      state.players.push(state.instrumentalPlayer);
      state.compareHandler = async () => {
        if (!state.instrumentalPlayer) return false;
        const usingAlt = await state.instrumentalPlayer.toggleAlternative();
        els.cleanTitle.textContent = usingAlt ? 'Originale' : 'Clip pulita';
        return Boolean(usingAlt);
      };
    }

    els.compareBtn.hidden = !job.has_source;
    els.compareBtn.setAttribute('aria-pressed', 'false');
    els.compareBtn.querySelector('span').textContent = 'Confronta originale';

    els.zipBtn.href = fileUrl(job.id, 'clean', true);
    els.zipBtn.setAttribute('download', clean.name);
    els.zipBtn.querySelector('span').textContent =
      outputMedia === 'video' ? 'Scarica video' : 'Scarica audio';

    const notes = [
      'Riduzione conservativa: post-filter disattivato, nessun effetto robotico.',
      'DeepFilterNet3 è ottimizzato per voce e parlato; per la musica usa la separazione stem.',
    ];
    if (job.result?.video_reencoded) {
      notes.push('Video ricodificato in H.264 per compatibilità con il nuovo audio.');
    }
    els.denoiseNote.textContent = notes.join(' ');
  }

  function renderPolishResults(job) {
    els.separatedFeatured.hidden = true;
    els.extraStemsWrap.hidden = true;
    els.extraStems.textContent = '';
    els.denoiseResult.hidden = false;
    els.polishBtn.hidden = true;
    els.cleanVideoWrap.hidden = true;
    els.playerClean.hidden = false;

    const clean = (job.files || {}).clean;
    const mode = job.mode || 'voice';
    const pct = Math.round((job.residual || job.result?.residual || 0) * 100);
    const humResult = job.result || {};
    const humHz = humResult.hum_hz || (job.hum_mode === '50' ? 50 : job.hum_mode === '60' ? 60 : null);
    const humApplied = Boolean(humResult.hum_applied);
    const targetLabel = mode === 'hum'
      ? 'Strumentale senza ronzio'
      : job.target === 'instrumental'
      ? 'Strumentale pulita'
      : 'Stem pulito';

    els.cleanTitle.textContent = targetLabel;
    els.cleanSubtitle.textContent = [
      job.model_label,
      mode === 'hum' ? (humApplied && humHz ? `ronzio ${humHz} Hz` : 'nessun ronzio rilevato') : `residui −${pct}%`,
      mode === 'both' && humApplied && humHz ? `ronzio ${humHz} Hz` : '',
      job.format_label,
    ]
      .filter(Boolean)
      .join(' · ');

    if (!clean) {
      els.denoiseNote.textContent = 'Il file pulito non è più disponibile.';
      els.compareBtn.hidden = true;
      return;
    }

    state.instrumentalPlayer = new WavePlayer(els.playerClean, {
      name: targetLabel,
      url: fileUrl(job.id, 'clean'),
      downloadUrl: fileUrl(job.id, 'clean', true),
      downloadName: clean.name,
      peaksUrl: peaksUrl(job.id, 'clean'),
      duration: job.duration,
      accent: 'cyan',
      alt: job.has_source
        ? {
            name: 'Originale',
            url: `/api/jobs/${encodeURIComponent(job.id)}/source`,
            downloadUrl: `/api/jobs/${encodeURIComponent(job.id)}/source?download=1`,
            downloadName: job.name,
            peaksUrl: peaksUrl(job.id, 'original'),
          }
        : null,
    });
    state.players.push(state.instrumentalPlayer);
    state.compareHandler = async () => {
      if (!state.instrumentalPlayer) return false;
      const usingAlt = await state.instrumentalPlayer.toggleAlternative();
      els.cleanTitle.textContent = usingAlt ? 'Originale' : targetLabel;
      return Boolean(usingAlt);
    };

    els.compareBtn.hidden = !job.has_source;
    els.compareBtn.setAttribute('aria-pressed', 'false');
    els.compareBtn.querySelector('span').textContent = 'Confronta originale';

    els.zipBtn.href = fileUrl(job.id, 'clean', true);
    els.zipBtn.setAttribute('download', clean.name);
    els.zipBtn.querySelector('span').textContent = 'Scarica stem';

    const notes = [];
    if (mode !== 'hum') {
      notes.push(
        `Secondo passaggio AI: la voce residua stimata è stata attenuata del ${pct}%.`,
      );
    }
    if (humApplied) {
      notes.push(
        `Ronzio a ${humHz || '?'} Hz ridotto con ${humResult.hum_bands || '—'} filtri notch stretti (${Math.abs(humResult.hum_gain_db || 20)} dB).`,
      );
    } else if (mode === 'hum' || job.hum_mode === 'auto') {
      notes.push('Nessun ronzio stabile rilevato: lo stem non è stato alterato dai notch.');
    }
    notes.push(
      'Se noti artefatti, confronta con l’originale o riprova con un’intensità più leggera.',
    );
    els.denoiseNote.textContent = notes.join(' ');
  }

  function destroyPlayers() {
    for (const player of state.players) player.destroy();
    state.players = [];
    state.instrumentalPlayer = null;
    state.compareHandler = null;
    els.playerInstrumental.textContent = '';
    els.playerVocals.textContent = '';
    els.playerClean.textContent = '';
    if (els.extraStems) els.extraStems.textContent = '';

    const video = els.cleanVideo;
    if (video) {
      try { video.pause(); } catch { /* noop */ }
      video.removeAttribute('src');
      try { video.load(); } catch { /* noop */ }
    }
    els.cleanVideoWrap.hidden = true;
  }

  // ── Wave player ────────────────────────────────────────────────────────────
  const GRADIENTS = {
    violet: ['#c4b5fd', '#818cf8', '#22d3ee'],
    pink: ['#fbcfe8', '#f472b6', '#be185d'],
    amber: ['#fde68a', '#f59e0b', '#b45309'],
    cyan: ['#a5f3fc', '#22d3ee', '#0e7490'],
  };

  class WavePlayer {
    constructor(host, opts) {
      this.host = host;
      this.opts = opts;
      this.compact = Boolean(opts.compact);
      this.accent = opts.accent || 'violet';
      this.srcNode = { ...opts };
      this.usingAlt = false;
      this.peaks = null;
      this.duration = opts.duration || 0;
      this.destroyed = false;
      this.raf = null;

      this.host.classList.add('wave-player', `accent-${this.accent}`);
      if (this.compact) this.host.classList.add('compact');
      this.host.innerHTML = `
        <button class="play-btn" type="button" aria-label="Riproduci">
          ${icon('play', 'js-play')}${icon('pause', 'js-pause')}
        </button>
        <div class="wave-body">
          <div class="wave-top">
            <span class="wave-name"></span>
            <span class="wave-time">0:00 / 0:00</span>
          </div>
          <canvas class="wave-canvas" tabindex="0" role="slider"
            aria-label="Forma d'onda, usa le frecce per spostarti" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"></canvas>
        </div>
        <a class="wave-dl" href="#" download aria-label="Scarica questo stem">${icon('download')}</a>
      `;

      this.playBtn = $('.play-btn', this.host);
      this.playIcon = $('.js-play', this.host);
      this.pauseIcon = $('.js-pause', this.host);
      this.nameEl = $('.wave-name', this.host);
      this.timeEl = $('.wave-time', this.host);
      this.canvas = $('.wave-canvas', this.host);
      this.downloadEl = $('.wave-dl', this.host);
      this.audio = new Audio();
      this.audio.preload = 'metadata';
      this.pauseIcon.style.display = 'none';

      this.setSource(this.srcNode, false);
      this.nameEl.textContent = opts.name;

      this.onClick = () => this.toggle();
      this.onTime = () => this.updateTime();
      this.onMeta = () => {
        if (Number.isFinite(this.audio.duration) && this.audio.duration > 0) {
          this.duration = this.audio.duration;
        }
        this.updateTime();
      };
      this.onEnded = () => {
        this.host.classList.remove('playing');
        this.playIcon.style.display = '';
        this.pauseIcon.style.display = 'none';
        this.draw(0);
      };
      this.onError = () => {
        this.host.classList.add('error');
        this.nameEl.textContent = `${opts.name} · anteprima non disponibile`;
      };
      this.onResize = () => this.draw();

      this.playBtn.addEventListener('click', this.onClick);
      this.audio.addEventListener('timeupdate', this.onTime);
      this.audio.addEventListener('loadedmetadata', this.onMeta);
      this.audio.addEventListener('ended', this.onEnded);
      this.audio.addEventListener('error', this.onError);

      this.onPointerDown = (event) => {
        event.preventDefault();
        this.dragging = true;
        this.canvas.setPointerCapture?.(event.pointerId);
        this.seekFromEvent(event);
      };
      this.onPointerMove = (event) => {
        if (!this.dragging) return;
        this.seekFromEvent(event);
      };
      this.onPointerUp = (event) => {
        this.dragging = false;
        this.canvas.releasePointerCapture?.(event.pointerId);
      };
      this.onKeyDown = (event) => {
        const step = event.shiftKey ? 30 : 5;
        if (event.key === 'ArrowRight') { this.seekBy(step); event.preventDefault(); }
        else if (event.key === 'ArrowLeft') { this.seekBy(-step); event.preventDefault(); }
        else if (event.key === 'Home') { this.seekTo(0); event.preventDefault(); }
        else if (event.key === 'End') { this.seekTo(this.getDuration()); event.preventDefault(); }
        else if (event.key === ' ') { this.toggle(); event.preventDefault(); }
      };

      this.canvas.addEventListener('pointerdown', this.onPointerDown);
      this.canvas.addEventListener('pointermove', this.onPointerMove);
      this.canvas.addEventListener('pointerup', this.onPointerUp);
      this.canvas.addEventListener('pointercancel', this.onPointerUp);
      this.canvas.addEventListener('keydown', this.onKeyDown);

      this.resizeObserver = new ResizeObserver(() => this.draw());
      this.resizeObserver.observe(this.canvas);

      this.loadPeaks();
      WavePlayer.instances.push(this);
    }

    static pauseOthers(current) {
      for (const player of WavePlayer.instances) {
        if (player !== current && !player.audio.paused) player.audio.pause();
      }
    }

    setSource(source, reset = true) {
      this.srcNode = source;
      this.nameEl.textContent = source.name;
      this.audio.src = source.url;
      this.downloadEl.href = source.downloadUrl;
      this.downloadEl.setAttribute('download', source.downloadName || source.name);
      if (reset) {
        this.peaks = null;
        this.loadPeaks();
      }
    }

    getDuration() {
      return Number.isFinite(this.audio.duration) && this.audio.duration > 0
        ? this.audio.duration
        : this.duration || 0;
    }

    async loadPeaks() {
      this.host.classList.add('loading');
      const source = this.srcNode;
      try {
        const response = await fetch(source.peaksUrl, { cache: 'no-store' });
        if (!response.ok) throw new Error('no peaks');
        const data = await response.json();
        if (this.destroyed) return;
        this.peaks = Array.isArray(data.peaks) && data.peaks.length ? data.peaks : null;
        if (data.duration) this.duration = data.duration;
      } catch {
        if (!this.destroyed) this.peaks = WavePlayer.placeholder(source.name);
      } finally {
        if (!this.destroyed) {
          this.host.classList.remove('loading');
          this.updateTime();
        }
      }
    }

    static placeholder(seed = 'x') {
      let hash = 0;
      for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) | 0;
      const values = [];
      const n = 220;
      for (let i = 0; i < n; i++) {
        const wave = Math.sin(i * 0.21 + hash) * 0.28 + Math.sin(i * 0.07 + hash * 0.5) * 0.24;
        const envelope = Math.sin((i / n) * Math.PI);
        const noise = (Math.sin(i * 12.9898 + hash) * 43758.5453) % 1;
        values.push(Math.max(0.06, Math.min(1, 0.3 + Math.abs(wave) + Math.abs(noise) * 0.3 * envelope)));
      }
      return values;
    }

    toggle() {
      if (this.audio.paused) {
        WavePlayer.pauseOthers(this);
        const promise = this.audio.play();
        if (promise) promise.catch(() => toast('Impossibile avviare la riproduzione.', 'error'));
      } else {
        this.audio.pause();
      }
    }

    updateTime() {
      const duration = this.getDuration();
      const current = Number.isFinite(this.audio.currentTime) ? this.audio.currentTime : 0;
      this.timeEl.textContent = `${fmtTime(current)} / ${fmtTime(duration)}`;
      const pct = duration ? Math.round((current / duration) * 100) : 0;
      this.canvas.setAttribute('aria-valuenow', String(pct));
      const playing = !this.audio.paused;
      this.host.classList.toggle('playing', playing);
      this.playIcon.style.display = playing ? 'none' : '';
      this.pauseIcon.style.display = playing ? '' : 'none';
      this.playBtn.setAttribute('aria-label', playing ? 'Metti in pausa' : 'Riproduci');
      this.draw();
    }

    draw() {
      if (this.destroyed) return;
      const canvas = this.canvas;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.floor(rect.width * dpr);
      const height = Math.floor(rect.height * dpr);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);

      const peaks = this.peaks || WavePlayer.placeholder(this.opts.name);
      const bars = Math.max(28, Math.min(240, Math.floor(rect.width / 4.2)));
      const sampled = resamplePeaks(peaks, bars);
      const duration = this.getDuration();
      const progress = duration > 0 ? Math.min(1, Math.max(0, this.audio.currentTime / duration)) : 0;
      const playedBars = Math.floor(progress * bars);

      const colors = GRADIENTS[this.accent] || GRADIENTS.violet;
      const gradient = ctx.createLinearGradient(0, 0, rect.width, 0);
      gradient.addColorStop(0, colors[0]);
      gradient.addColorStop(0.55, colors[1]);
      gradient.addColorStop(1, colors[2]);

      const slot = rect.width / bars;
      const barWidth = Math.max(1, slot * 0.58);
      const radius = Math.min(1.5, barWidth / 2);
      const centerY = rect.height / 2;
      const maxHeight = rect.height - 8;

      for (let i = 0; i < bars; i++) {
        const value = Math.max(0.03, sampled[i]);
        const barHeight = Math.max(2, value * maxHeight);
        const x = i * slot + (slot - barWidth) / 2;
        const y = centerY - barHeight / 2;
        ctx.fillStyle = i < playedBars ? gradient : 'rgba(255,255,255,0.16)';
        roundedRect(ctx, x, y, barWidth, barHeight, radius);
        ctx.fill();
      }

      if (progress > 0) {
        const x = progress * rect.width;
        ctx.fillStyle = 'rgba(255,255,255,0.65)';
        ctx.fillRect(Math.min(rect.width - 1, x), 4, 1.5, rect.height - 8);
      }
    }

    seekFromEvent(event) {
      const rect = this.canvas.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      this.seekTo(ratio * this.getDuration());
    }

    seekTo(seconds) {
      const duration = this.getDuration();
      if (!duration) return;
      this.audio.currentTime = Math.max(0, Math.min(duration, seconds));
      this.updateTime();
    }

    seekBy(delta) {
      this.seekTo(this.audio.currentTime + delta);
    }

    async toggleAlternative() {
      if (!this.opts.alt) return false;
      const time = this.audio.currentTime;
      const wasPlaying = !this.audio.paused;
      this.usingAlt = !this.usingAlt;
      const next = this.usingAlt ? this.opts.alt : this.opts;
      this.setSource({ ...this.opts, ...next, name: next.name });
      const restore = () => {
        const duration = this.getDuration();
        this.audio.currentTime = Math.min(time, duration || time);
        if (wasPlaying) {
          const promise = this.audio.play();
          if (promise) promise.catch(() => {});
        }
      };
      if (this.audio.readyState >= 1) restore();
      else this.audio.addEventListener('loadedmetadata', restore, { once: true });
      return this.usingAlt;
    }

    destroy() {
      this.destroyed = true;
      try { this.audio.pause(); } catch { /* noop */ }
      this.audio.removeAttribute('src');
      try { this.audio.load(); } catch { /* noop */ }
      this.resizeObserver?.disconnect();
      this.playBtn?.removeEventListener('click', this.onClick);
      this.audio.removeEventListener('timeupdate', this.onTime);
      this.audio.removeEventListener('loadedmetadata', this.onMeta);
      this.audio.removeEventListener('ended', this.onEnded);
      this.audio.removeEventListener('error', this.onError);
      this.canvas?.removeEventListener('pointerdown', this.onPointerDown);
      this.canvas?.removeEventListener('pointermove', this.onPointerMove);
      this.canvas?.removeEventListener('pointerup', this.onPointerUp);
      this.canvas?.removeEventListener('pointercancel', this.onPointerUp);
      this.canvas?.removeEventListener('keydown', this.onKeyDown);
      const index = WavePlayer.instances.indexOf(this);
      if (index >= 0) WavePlayer.instances.splice(index, 1);
      this.host.textContent = '';
    }
  }

  WavePlayer.instances = [];

  function resamplePeaks(peaks, count) {
    const out = new Float32Array(count);
    if (!peaks || !peaks.length) return out;
    const step = peaks.length / count;
    for (let i = 0; i < count; i++) {
      const start = Math.floor(i * step);
      const end = Math.max(start + 1, Math.min(peaks.length, Math.floor((i + 1) * step)));
      let max = 0;
      for (let j = start; j < end; j++) {
        const value = peaks[j] || 0;
        if (value > max) max = value;
      }
      out[i] = max;
    }
    return out;
  }

  function roundedRect(ctx, x, y, width, height, radius) {
    const r = Math.max(0, Math.min(radius, width / 2, height / 2));
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + width - r, y);
    ctx.quadraticCurveTo(x + width, y, x + width, y + r);
    ctx.lineTo(x + width, y + height - r);
    ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    ctx.lineTo(x + r, y + height);
    ctx.quadraticCurveTo(x, y + height, x, y + height - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  // ── Upload ─────────────────────────────────────────────────────────────────
  function validateFile(file, mode) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    const allowed = mode === 'denoise' ? DENOISE_EXT : ALLOWED_EXT;
    if (!allowed.includes(ext)) {
      toast(`«${file.name}»: formato .${ext || '?'} non supportato.`, 'error');
      return false;
    }
    if (file.size > CFG.maxMb * 1024 * 1024) {
      toast(`«${file.name}» supera il limite di ${CFG.maxMb} MB.`, 'error');
      return false;
    }
    if (file.size === 0) {
      toast(`«${file.name}» è vuoto.`, 'error');
      return false;
    }
    return true;
  }

  function enqueueFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const mode = state.mode;
    const options = currentOptions();
    let added = 0;
    for (const file of files) {
      if (!validateFile(file, mode)) continue;
      state.uploadQueue.push({ file, options: { ...options }, mode });
      added += 1;
    }
    if (added) runUploadQueue();
  }

  async function runUploadQueue() {
    if (state.uploadRunning) return;
    state.uploadRunning = true;
    const uploaded = [];
    try {
      while (state.uploadQueue.length) {
        const item = state.uploadQueue.shift();
        try {
          const job = await uploadOne(item.file, item.options, item.mode, item.key);
          uploaded.push(job);
        } catch (error) {
          toast(`«${item.file.name}»: ${error.message}`, 'error', 7000);
        }
      }
    } finally {
      state.uploadRunning = false;
    }
    if (uploaded.length) {
      await refresh();
      const current = state.selectedId ? state.jobs.get(state.selectedId) : null;
      if (!current || !ACTIVE.has(current.status)) {
        selectJob(uploaded[0].id, { scroll: true });
      }
    }
  }

  function uploadOne(file, options, mode, providedKey) {
    return new Promise((resolve, reject) => {
      const key = providedKey || Math.random().toString(36).slice(2, 10);
      const upload = { key, name: file.name, progress: 0, mode };
      state.uploads.push(upload);
      renderJobs();

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload', true);
      xhr.responseType = 'json';
      xhr.timeout = 30 * 60 * 1000;

      const finish = (fn, value) => {
        const index = state.uploads.indexOf(upload);
        if (index >= 0) state.uploads.splice(index, 1);
        renderJobs();
        fn(value);
      };

      xhr.upload.addEventListener('progress', (event) => {
        if (!event.lengthComputable) return;
        const pct = Math.round((event.loaded / event.total) * 100);
        upload.progress = Math.max(upload.progress, Math.min(99, pct));
        const row = state.rows.get(uploadRowKey(key));
        if (row) updateUploadRow(row, upload);
      });

      xhr.addEventListener('load', () => {
        const data = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300 && data.job) {
          finish(resolve, data.job);
        } else {
          finish(reject, new Error(data.error || `Errore ${xhr.status}`));
        }
      });
      xhr.addEventListener('error', () => finish(reject, new Error('Connessione persa durante il caricamento')));
      xhr.addEventListener('timeout', () => finish(reject, new Error('Caricamento scaduto, riprova')));

      const form = new FormData();
      form.append('file', file, file.name);
      form.append('kind', mode === 'denoise' ? 'denoise' : 'separate');
      if (mode === 'denoise') {
        form.append('strength', options.strength);
        form.append('format', options.format);
      } else {
        form.append('model', options.model);
        form.append('quality', options.quality);
        form.append('format', options.format);
      }
      xhr.send(form);
    });
  }

  // ── Azioni sui job ─────────────────────────────────────────────────────────
  async function cancelJob(id) {
    try {
      await fetch(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      await refresh();
    } catch {
      toast('Impossibile annullare il job.', 'error');
    }
  }

  async function retryJob(id, attempt = 0) {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 409 && attempt === 0) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
        return retryJob(id, 1);
      }
      if (!response.ok) throw new Error(data.error || 'Errore');
      selectJob(id);
      toast('Brano rimesso in coda.', 'success');
      await refresh();
    } catch (error) {
      toast(error.message || 'Impossibile riprovare.', 'error');
    }
  }

  async function deleteJob(id) {
    const job = state.jobs.get(id);
    const ok = await confirmAction({
      title: 'Eliminare questo job?',
      text: `«${job?.name || 'Brano'}» e tutti i file generati verranno rimossi dal disco.`,
      okLabel: 'Elimina',
    });
    if (!ok) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Errore');
      if (state.selectedId === id) clearSelection();
      toast('Job eliminato.', 'success');
      await refresh();
    } catch (error) {
      toast(error.message || 'Impossibile eliminare il job.', 'error');
    }
  }

  function updatePolishHint() {
    const mode = els.polishMode.value;
    const preset = CFG.polish[els.polishStrength.value];
    const hum = els.polishHum.value;
    const pct = Math.round((preset?.residual || 0) * 100);
    const humLabels = { auto: 'Auto (50/60 Hz)', off: 'non attivo', 50: '50 Hz', 60: '60 Hz' };

    els.polishStrength.disabled = mode === 'hum';
    els.polishHum.disabled = mode === 'voice';
    if (mode === 'voice' && hum !== 'off') els.polishHum.value = 'off';
    if (mode === 'hum' && hum === 'off') els.polishHum.value = 'auto';

    if (mode === 'hum') {
      els.polishHint.textContent =
        `Solo filtri notch sul ronzio · ${humLabels[els.polishHum.value]}. Nessuna modifica alla voce o agli strumenti.`;
    } else if (mode === 'voice') {
      els.polishHint.textContent = `${preset?.description || ''} · riduzione ${pct}% · ronzio non toccato.`;
    } else {
      const h = els.polishHum.value;
      els.polishHint.textContent =
        `${preset?.description || ''} · voce −${pct}% · ronzio: ${humLabels[h]}.`;
    }
  }

  function askPolish(job) {
    return new Promise((resolve) => {
      if (job?.format) els.polishFormat.value = job.format;
      els.polishMode.value = 'both';
      els.polishHum.value = 'auto';
      updatePolishHint();
      els.polishDialog.returnValue = 'cancel';
      const onClose = () => {
        els.polishDialog.removeEventListener('close', onClose);
        if (els.polishDialog.returnValue !== 'confirm') {
          resolve(null);
          return;
        }
        resolve({
          mode: els.polishMode.value,
          strength: els.polishStrength.value,
          hum: els.polishHum.value,
          format: els.polishFormat.value,
        });
      };
      els.polishDialog.addEventListener('close', onClose);
      els.polishDialog.showModal();
    });
  }

  async function startPolish(job) {
    const choice = await askPolish(job);
    if (!choice) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/polish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'instrumental',
          mode: choice.mode,
          strength: choice.strength,
          hum: choice.hum,
          format: choice.format,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Errore');
      toast('Pulizia accodata.', 'success');
      await refresh();
      if (data.job?.id) selectJob(data.job.id, { scroll: true });
    } catch (error) {
      toast(error.message || 'Impossibile avviare la pulizia.', 'error');
    }
  }

  // ── Polling ────────────────────────────────────────────────────────────────
  function hasActiveJobs() {
    for (const job of state.jobs.values()) if (ACTIVE.has(job.status)) return true;
    return false;
  }

  function schedulePoll(delay) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(refresh, delay);
  }

  async function refresh(options = {}) {
    if (state.refreshing) return;
    state.refreshing = true;
    els.refreshBtn.classList.add('spinning');
    try {
      const response = await fetch('/api/jobs', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.online = true;
      applyServer(data.server);
      syncJobs(data.jobs || []);

      if (!state.initialSelectionDone) {
        state.initialSelectionDone = true;
        const hash = /^#job=([a-f0-9]+)$/i.exec(location.hash);
        if (hash && state.jobs.has(hash[1])) {
          const job = state.jobs.get(hash[1]);
          selectJob(hash[1]);
          window.setTimeout(() => {
            const target = job.status === 'completed' ? els.resultsPanel : els.processingPanel;
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 250);
        } else {
          const active = state.order.map((id) => state.jobs.get(id)).find((job) => ACTIVE.has(job.status));
          if (active) selectJob(active.id);
        }
      }
    } catch {
      setOffline();
    } finally {
      state.refreshing = false;
      els.refreshBtn.classList.remove('spinning');
      const delay = hasActiveJobs() || state.uploads.length || state.uploadRunning ? 1300 : 5000;
      schedulePoll(delay);
    }
  }

  // ── Eventi ─────────────────────────────────────────────────────────────────
  function bindEvents() {
    els.modelSelect.addEventListener('change', updateHints);
    els.qualitySelect.addEventListener('change', updateHints);
    els.formatSelect.addEventListener('change', updateHints);
    els.strengthSelect.addEventListener('change', updateHints);
    els.denoiseFormatSelect.addEventListener('change', updateHints);

    els.modeSeparate.addEventListener('click', () => setMode('separate'));
    els.modeDenoise.addEventListener('click', () => setMode('denoise'));

    els.chooseBtn.addEventListener('click', () => els.fileInput.click());
    els.fileInput.addEventListener('change', () => {
      enqueueFiles(els.fileInput.files);
      els.fileInput.value = '';
    });

    let dragDepth = 0;
    els.dropzone.addEventListener('dragenter', (event) => {
      event.preventDefault();
      dragDepth += 1;
      els.dropzone.classList.add('drag');
    });
    els.dropzone.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    });
    els.dropzone.addEventListener('dragleave', (event) => {
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) els.dropzone.classList.remove('drag');
    });
    els.dropzone.addEventListener('drop', (event) => {
      event.preventDefault();
      dragDepth = 0;
      els.dropzone.classList.remove('drag');
      if (event.dataTransfer?.files?.length) enqueueFiles(event.dataTransfer.files);
    });
    els.dropzone.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        els.fileInput.click();
      }
    });

    // Evita che il browser apra i file trascinati fuori dalla dropzone.
    window.addEventListener('dragover', (event) => {
      if (event.dataTransfer?.types?.includes('Files')) event.preventDefault();
    });
    window.addEventListener('drop', (event) => {
      if (event.dataTransfer?.types?.includes('Files')) event.preventDefault();
    });

    els.jobList.addEventListener('click', (event) => {
      const actionButton = event.target.closest('[data-action]');
      const row = event.target.closest('.job');
      if (!row) return;
      const jobId = row.dataset.id;
      if (!jobId) return;
      if (actionButton) {
        event.stopPropagation();
        const action = actionButton.dataset.action;
        if (action === 'cancel') cancelJob(jobId);
        else if (action === 'delete') deleteJob(jobId);
        else if (action === 'retry') retryJob(jobId);
        else if (action === 'open') selectJob(jobId, { scroll: true });
        return;
      }
      selectJob(jobId, { scroll: true });
    });

    els.refreshBtn.addEventListener('click', () => refresh());

    els.cancelBtn.addEventListener('click', () => {
      if (state.selectedId) cancelJob(state.selectedId);
    });
    els.retryBtn.addEventListener('click', () => {
      if (state.selectedId) retryJob(state.selectedId);
    });
    els.deleteJobBtn.addEventListener('click', () => {
      if (state.selectedId) deleteJob(state.selectedId);
    });

    els.polishBtn.addEventListener('click', () => {
      const job = state.selectedId ? state.jobs.get(state.selectedId) : null;
      if (job) startPolish(job);
    });
    els.polishStrength.addEventListener('change', updatePolishHint);
    els.polishMode.addEventListener('change', updatePolishHint);
    els.polishHum.addEventListener('change', updatePolishHint);

    els.compareBtn.addEventListener('click', async () => {
      if (!state.compareHandler) return;
      const usingAlt = await state.compareHandler();
      els.compareBtn.setAttribute('aria-pressed', String(Boolean(usingAlt)));
      els.compareBtn.querySelector('span').textContent = usingAlt
        ? "Torna all'originale"
        : 'Confronta originale';
    });

    els.extrasToggle.addEventListener('click', () => {
      const expanded = els.extrasToggle.getAttribute('aria-expanded') === 'true';
      els.extrasToggle.setAttribute('aria-expanded', String(!expanded));
      els.extraStems.hidden = expanded;
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh();
    });

    window.addEventListener('hashchange', () => {
      const match = /^#job=([a-f0-9]+)$/i.exec(location.hash);
      if (match && state.jobs.has(match[1])) {
        selectJob(match[1], { scroll: true, hash: false });
      }
    });

    window.addEventListener('beforeunload', () => {
      for (const player of WavePlayer.instances) player.destroy();
    });
  }

  // ── Avvio ──────────────────────────────────────────────────────────────────
  function init() {
    bindEvents();
    let savedMode = 'separate';
    try { savedMode = localStorage.getItem('avr-mode') || 'separate'; } catch { /* noop */ }
    setMode(savedMode === 'denoise' && CFG.denoiseAvailable ? 'denoise' : 'separate', {
      persist: false,
    });
    updateHints();
    els.extraStems.hidden = false;
    els.extrasToggle.setAttribute('aria-expanded', 'true');
    refresh();
  }

  init();
})();
