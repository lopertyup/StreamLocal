const state = {
  providers: [],
  home: null,
  details: null,
  currentView: "home",
  currentPlayback: null,
  currentEpisodeContext: null,
  pendingResumeChoice: null,
  appFullscreenMode: null,
  videoFullscreenPromoting: false,
  videoFullscreenToggleAt: 0,
  videoNativeFullscreenSuppressUntil: 0,
  scanProviders: [],
  scanResults: [],
  scanDetails: null,
  scanReader: null,
  scanPan: null,
  scanSuppressClick: false,
  scanQuery: "",
  scanProvider: "all",
  scanLanguage: "fr",
  scanProgressSentAt: 0,
  scanObserver: null,
  scanRestoringScroll: false,
  hls: null,
  progressSentAt: 0,
  qualityLevels: [],
  qualityMode: "max", // "max" | "auto" | level index as number
  subtitleTracks: [],
  subtitleMode: "off",
  subtitleTrackElement: null,
  subtitleOffsetMs: 0,
  subtitleFirstCueBaseTime: null,
  subtitleCalibrationDraft: null,
  subtitleCalibrationHidden: true,
  subtitleTrackLoadNonce: 0,
  preferredQuality: "max",
  downloads: [],
  downloadPath: "",
  downloadFilter: "all",
  ffmpegAvailable: true,
  pollTimer: null,
  iframePlayer: null,
  iframeMessageHandler: null,
  iframeProgress: null,
};

const app = document.getElementById("app");
const toast = document.getElementById("toast");
const video = document.getElementById("video-player");
const playerView = document.getElementById("player-view");
const playerTitle = document.getElementById("player-title");
const qualityToggle = document.getElementById("quality-toggle");
const qualityMenu = document.getElementById("quality-menu");
const subtitleToggle = document.getElementById("subtitle-toggle");
const subtitleMenu = document.getElementById("subtitle-menu");
const subtitleCalibration = document.getElementById("subtitle-calibration");
const subtitleCalibrationStart = document.getElementById("subtitle-calibration-start");
const subtitleCalibrationConfirm = document.getElementById("subtitle-calibration-confirm");
const subtitleCalibrationOffset = document.getElementById("subtitle-calibration-offset");
const downloadCurrentBtn = document.getElementById("download-current");
const nextEpisodeBtn = document.getElementById("next-episode");
const resumeModal = document.getElementById("resume-modal");
const resumeModalPosition = document.getElementById("resume-modal-position");
const downloadsBadge = document.getElementById("downloads-badge");

const PLAYER_VOLUME_KEY = "autoflix.player.volume";
const PLAYER_MUTED_KEY = "autoflix.player.muted";
const SCAN_READER_MIN_ZOOM = 1;
const SCAN_READER_MAX_ZOOM = 3;
const SCAN_READER_ZOOM_STEP = 0.25;
const SCAN_READER_PAN_THRESHOLD = 5;
const VIDEO_FULLSCREEN_TOGGLE_GUARD_MS = 350;
const VIDEO_NATIVE_FULLSCREEN_SUPPRESS_MS = 900;
const DOWNLOAD_ACTIVE_STATES = new Set(["queued", "resolving", "downloading"]);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function devLog(action, details = {}, status = "ok") {
  fetch("/api/devlog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, status, details }),
    keepalive: true,
  }).catch(() => {});
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
  } catch (error) {
    devLog("fetch_error", { path, message: error.message }, "error");
    throw error;
  }

  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    devLog("fetch_error", { path, message: "invalid_json", http_status: response.status }, "error");
    throw error;
  }
  if (!response.ok) {
    devLog("fetch_error", { path, message: payload.message || "Erreur AutoFlix", http_status: response.status }, "error");
    throw new Error(payload.message || "Erreur AutoFlix");
  }
  return payload;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast("Copié");
  } catch (error) {
    showToast("Impossible de copier");
  }
}

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement || null;
}

function fullscreenOwner(element) {
  const active = fullscreenElement();
  return Boolean(active && element && (active === element || element.contains(active)));
}

function scanFullscreenStage() {
  return document.querySelector(".scan-reader-stage");
}

function desktopFullscreenApi() {
  return window.pywebview?.api?.set_fullscreen || null;
}

function appFullscreenActive(mode = null) {
  return mode ? state.appFullscreenMode === mode : Boolean(state.appFullscreenMode);
}

function syncAppFullscreenClasses() {
  const mode = state.appFullscreenMode;
  const scanMode = mode === "scan" ? state.scanReader?.mode || "vertical" : null;
  document.body.classList.toggle("app-fullscreen-active", Boolean(mode));
  document.body.classList.toggle("app-video-fullscreen", mode === "video");
  document.body.classList.toggle("app-scan-fullscreen", mode === "scan");
  document.body.classList.toggle("app-scan-single-fullscreen", scanMode === "single");
  document.body.classList.toggle("app-scan-vertical-fullscreen", scanMode === "vertical");
}

function applyAppFullscreenMode(mode) {
  state.appFullscreenMode = mode || null;
  syncAppFullscreenClasses();
  updateFullscreenButtons();
}

async function requestFullscreenFor(element) {
  if (!element) return false;
  const request =
    element.requestFullscreen
    || element.webkitRequestFullscreen
    || element.msRequestFullscreen;
  if (!request) {
    showToast("Plein écran non supporté");
    return false;
  }
  try {
    await request.call(element);
    return true;
  } catch (error) {
    devLog("fullscreen_error", { message: error.message }, "error");
    showToast("Impossible d'activer le plein écran");
    return false;
  }
}

async function exitFullscreenMode() {
  const exit =
    document.exitFullscreen
    || document.webkitExitFullscreen
    || document.msExitFullscreen;
  if (!exit || !fullscreenElement()) return;
  try {
    await exit.call(document);
  } catch (error) {
    devLog("fullscreen_exit_error", { message: error.message }, "warning");
  }
}

async function setNativeAppFullscreen(enabled) {
  const desktopApi = desktopFullscreenApi();
  if (desktopApi) {
    try {
      const result = await window.pywebview.api.set_fullscreen(Boolean(enabled));
      if (result?.supported === false) {
        devLog("desktop_fullscreen_unsupported", { error: result.error || "" }, "warning");
      } else {
        return true;
      }
    } catch (error) {
      devLog("desktop_fullscreen_error", { message: error.message }, "warning");
    }
  }

  if (enabled) {
    return requestFullscreenFor(document.documentElement);
  }
  await exitFullscreenMode();
  return true;
}

async function enterAppFullscreen(mode) {
  if (!mode) return;
  if (appFullscreenActive(mode)) {
    await exitAppFullscreen();
    return;
  }

  const previousMode = state.appFullscreenMode;
  const scanScrollAnchor = mode === "scan" && state.scanReader?.mode !== "single"
    ? captureScanScrollAnchor()
    : null;
  state.scanRestoringScroll = Boolean(scanScrollAnchor);
  applyAppFullscreenMode(mode);
  restoreScanScrollAnchor(scanScrollAnchor);

  const enabled = await setNativeAppFullscreen(true);
  if (!enabled) {
    applyAppFullscreenMode(previousMode);
    restoreScanScrollAnchor(scanScrollAnchor);
    state.scanRestoringScroll = false;
    return;
  }

  if (scanScrollAnchor) {
    window.setTimeout(() => {
      state.scanRestoringScroll = false;
    }, 250);
  }
}

async function exitAppFullscreen(options = {}) {
  const previousMode = state.appFullscreenMode;
  if (!previousMode && !fullscreenElement()) return;

  const scanScrollAnchor = previousMode === "scan" && state.scanReader?.mode !== "single"
    ? captureScanScrollAnchor()
    : null;
  state.scanRestoringScroll = Boolean(scanScrollAnchor);
  applyAppFullscreenMode(null);

  if (!options.skipNative) {
    await setNativeAppFullscreen(false);
  }
  restoreScanScrollAnchor(scanScrollAnchor);
  if (scanScrollAnchor) {
    window.setTimeout(() => {
      state.scanRestoringScroll = false;
    }, 250);
  }
}

function updateFullscreenButtons() {
  const scanIsFullscreen = appFullscreenActive("scan") || fullscreenOwner(scanFullscreenStage());
  document.querySelectorAll("[data-reader-fullscreen]").forEach((button) => {
    button.textContent = scanIsFullscreen ? "Quitter plein écran" : "Plein écran";
  });
}

async function promoteVideoElementFullscreen() {
  if (state.videoFullscreenPromoting || appFullscreenActive("video")) return;
  state.videoFullscreenPromoting = true;
  try {
    if (fullscreenOwner(video)) {
      await exitFullscreenMode();
    }
    await enterAppFullscreen("video");
  } finally {
    state.videoFullscreenPromoting = false;
  }
}

async function demoteVideoElementFullscreen() {
  if (state.videoFullscreenPromoting) return;
  state.videoFullscreenPromoting = true;
  try {
    if (fullscreenOwner(video)) {
      await exitFullscreenMode();
    }
    await exitAppFullscreen();
  } finally {
    state.videoFullscreenPromoting = false;
  }
}

function handleFullscreenChange() {
  const suppressVideoNativeFullscreen = Date.now() < state.videoNativeFullscreenSuppressUntil;
  if (fullscreenOwner(video)) {
    if (suppressVideoNativeFullscreen) {
      exitFullscreenMode().catch((error) => devLog("fullscreen_exit_error", { message: error.message }, "warning"));
      updateFullscreenButtons();
      return;
    }
    if (appFullscreenActive("video")) {
      demoteVideoElementFullscreen().catch((error) => showToast(error.message));
    } else {
      promoteVideoElementFullscreen().catch((error) => showToast(error.message));
    }
    return;
  }
  if (state.videoFullscreenPromoting) {
    updateFullscreenButtons();
    return;
  }
  if (appFullscreenActive() && !desktopFullscreenApi() && !fullscreenElement()) {
    applyAppFullscreenMode(null);
    return;
  }
  updateFullscreenButtons();
}

function setActiveView(view) {
  state.currentView = view;
  devLog("navigation", { view });
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
}

function imageHtml(src, alt, className = "poster") {
  if (src) {
    return `<img class="${className}" src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy">`;
  }
  const initial = escapeHtml((alt || "A").trim().slice(0, 1).toUpperCase());
  return `<div class="${className} poster-fallback">${initial}</div>`;
}

function labelType(type) {
  return {
    anime: "Anime",
    movie: "Film",
    series: "Série",
    manga: "Manga",
    scan: "Scan",
  }[type] || "Titre";
}

function percent(entry) {
  const value = Number(entry?.percent || 0);
  return Math.max(0, Math.min(100, value));
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function readStoredPlayerState() {
  try {
    const rawVolume = window.localStorage.getItem(PLAYER_VOLUME_KEY);
    const volume = rawVolume === null ? null : Number(rawVolume);
    const muted = window.localStorage.getItem(PLAYER_MUTED_KEY) === "true";
    return {
      volume: Number.isFinite(volume) ? clamp(volume, 0, 1) : null,
      muted,
    };
  } catch (_error) {
    return { volume: null, muted: false };
  }
}

function applyStoredPlayerState() {
  const stored = readStoredPlayerState();
  if (stored.volume !== null) video.volume = stored.volume;
  video.muted = stored.muted;
}

function saveStoredPlayerState() {
  try {
    window.localStorage.setItem(PLAYER_VOLUME_KEY, String(video.volume));
    window.localStorage.setItem(PLAYER_MUTED_KEY, video.muted ? "true" : "false");
  } catch (_error) {
    // localStorage can be unavailable in hardened browser contexts.
  }
}

function formatSeconds(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function mediaCard(item) {
  const isScan = item.media_kind === "scan" || item.content_type === "manga" || item.content_type === "scan";
  const openAttr = isScan ? "data-open-scan" : "data-open-content";
  return `
    <button class="media-card" type="button" ${openAttr}="${escapeHtml(item.provider_id)}:${escapeHtml(item.content_id)}">
      ${imageHtml(item.image, item.title)}
      <div class="card-body">
        <h3>${escapeHtml(item.title)}</h3>
        <div class="meta-line">
          <span>${escapeHtml(item.provider_name)}</span>
          <span>${escapeHtml(labelType(item.content_type))}</span>
        </div>
      </div>
    </button>
  `;
}

function historyItem(item, index) {
  const historyKey = item.history_key || "";
  return `
    <article class="history-item">
      <button class="history-main" type="button" data-history-index="${index}">
        ${imageHtml(item.logo_url, item.series_title, "history-thumb")}
        <div>
          <strong>${escapeHtml(item.series_title)}</strong>
          <div class="meta-line">
            <span>${escapeHtml(item.provider)}</span>
            <span>${escapeHtml(item.season_title)}</span>
            <span>${escapeHtml(item.episode_title)}</span>
          </div>
          <div class="progress-bar"><span style="width:${percent(item)}%"></span></div>
        </div>
      </button>
      <div class="history-actions">
        <span class="pill">${Math.round(percent(item))}%</span>
        ${historyKey ? `<button class="tiny-button danger" type="button" data-history-delete="${escapeHtml(historyKey)}">Supprimer</button>` : ""}
      </div>
    </article>
  `;
}

async function loadHome() {
  setActiveView("home");
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  state.home = await api("/api/home");
  renderHome();
}

function renderHome() {
  const home = state.home || { favorites: [], history: [] };
  const resume = home.resume;
  app.innerHTML = `
    <section>
      <div class="section-header">
        <div>
          <p class="eyebrow">AutoFlix Desktop</p>
          <h1>Accueil</h1>
        </div>
      </div>
      ${
        resume
          ? `<article class="history-item">
              <button class="history-main" type="button" data-resume-index="resume">
                ${imageHtml(resume.logo_url, resume.series_title, "history-thumb")}
                <div>
                  <strong>${escapeHtml(resume.series_title)}</strong>
                  <div class="meta-line">
                    <span>${escapeHtml(resume.provider)}</span>
                    <span>${escapeHtml(resume.season_title)}</span>
                    <span>${escapeHtml(resume.episode_title)}</span>
                  </div>
                  <div class="progress-bar"><span style="width:${percent(resume)}%"></span></div>
                </div>
              </button>
              <div class="history-actions">
                <span class="pill">Reprendre</span>
              </div>
            </article>`
          : `<div class="empty-state">Aucune reprise locale.</div>`
      }
    </section>

    <section>
      <div class="section-header">
        <h2>Favoris</h2>
      </div>
      <div class="grid">${home.favorites.slice(0, 12).map(mediaCard).join("") || `<div class="empty-state">Aucun favori.</div>`}</div>
    </section>

    <section>
      <div class="section-header">
        <h2>Historique récent</h2>
      </div>
      <div class="history-list">${home.history.slice(0, 12).map(historyItem).join("") || `<div class="empty-state">Aucun historique.</div>`}</div>
    </section>
  `;
}

async function runSearch() {
  const query = document.getElementById("search-input").value.trim();
  if (!query) return;
  const type = document.getElementById("type-filter").value;
  if (type === "manga" || type === "scan") {
    state.scanQuery = query;
    await runScanSearch();
    return;
  }
  setActiveView("search");
  app.innerHTML = `<div class="loading-state">Recherche</div>`;
  try {
    const provider = document.getElementById("provider-filter").value;
    devLog("search", { query, provider, type });
    const results = await api(`/api/search?q=${encodeURIComponent(query)}&provider=${encodeURIComponent(provider)}&type=${encodeURIComponent(type)}`);
    devLog("search_results", { query, result_count: results.results.length });
    app.innerHTML = `
      <div class="section-header">
        <div>
          <p class="eyebrow">Recherche</p>
          <h1>${escapeHtml(query)}</h1>
        </div>
        <span class="pill">${results.results.length} résultats</span>
      </div>
      <div class="grid">${results.results.map(mediaCard).join("") || `<div class="empty-state">Aucun résultat.</div>`}</div>
    `;
  } catch (error) {
    devLog("search_error", { query, message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function loadScanProviders() {
  if (state.scanProviders.length) return state.scanProviders;
  const payload = await api("/api/scans/providers");
  state.scanProviders = payload.providers || [];
  return state.scanProviders;
}

function scanProviderOptions(selected = "all") {
  return `<option value="all" ${selected === "all" ? "selected" : ""}>Tous</option>` + state.scanProviders.map((provider) => (
    `<option value="${escapeHtml(provider.id)}" ${selected === provider.id ? "selected" : ""}>${escapeHtml(provider.label)}</option>`
  )).join("");
}

async function renderScans() {
  setActiveView("scans");
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    await loadScanProviders();
    renderScanSearch();
  } catch (error) {
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderScanSearch() {
  app.innerHTML = `
    <section>
      <div class="section-header">
        <div>
          <p class="eyebrow">Manga</p>
          <h1>Scans</h1>
        </div>
      </div>
      <form id="scan-search-form" class="scan-search-form">
        <input id="scan-search-input" type="search" autocomplete="off" placeholder="Rechercher un manga" value="${escapeHtml(state.scanQuery)}">
        <select id="scan-provider-filter" aria-label="Provider scan">
          ${scanProviderOptions(state.scanProvider)}
        </select>
        <select id="scan-language-filter" aria-label="Langue scan">
          <option value="fr" ${state.scanLanguage === "fr" ? "selected" : ""}>FR</option>
          <option value="en" ${state.scanLanguage === "en" ? "selected" : ""}>EN</option>
        </select>
        <button class="primary-button" type="submit">Rechercher</button>
      </form>
    </section>
    <section>
      <div class="section-header">
        <h2>Resultats</h2>
        <span class="pill">${state.scanResults.length}</span>
      </div>
      <div class="grid">${state.scanResults.map(scanCard).join("") || `<div class="empty-state">Aucun resultat.</div>`}</div>
    </section>
  `;
}

async function runScanSearch() {
  await loadScanProviders();
  const queryInput = document.getElementById("scan-search-input");
  const providerInput = document.getElementById("scan-provider-filter");
  const languageInput = document.getElementById("scan-language-filter");
  const query = (queryInput?.value || state.scanQuery || "").trim();
  if (!query) {
    await renderScans();
    return;
  }
  state.scanQuery = query;
  state.scanProvider = providerInput?.value || state.scanProvider || "all";
  state.scanLanguage = languageInput?.value || state.scanLanguage || "fr";
  setActiveView("scans");
  app.innerHTML = `<div class="loading-state">Recherche</div>`;
  try {
    const result = await api(`/api/scans/search?q=${encodeURIComponent(query)}&provider=${encodeURIComponent(state.scanProvider)}&language=${encodeURIComponent(state.scanLanguage)}`);
    state.scanResults = result.results || [];
    devLog("scan_search_results", { query, result_count: state.scanResults.length, language: state.scanLanguage });
    renderScanSearch();
  } catch (error) {
    devLog("scan_search_error", { query, message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function scanCard(item) {
  return `
    <button class="media-card" type="button" data-open-scan="${escapeHtml(item.provider_id)}:${escapeHtml(item.content_id)}">
      ${imageHtml(item.image, item.title)}
      <div class="card-body">
        <h3>${escapeHtml(item.title)}</h3>
        <div class="meta-line">
          <span>${escapeHtml(item.provider_name)}</span>
          <span>${escapeHtml((item.languages || []).join("/").toUpperCase())}</span>
          ${item.status ? `<span>${escapeHtml(item.status)}</span>` : ""}
        </div>
      </div>
    </button>
  `;
}

async function openScanDetails(providerId, contentId, language = null) {
  setActiveView("scans");
  disconnectScanObserver();
  state.scanReader = null;
  const selectedLanguage = language || state.scanLanguage || "fr";
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    const details = await api(`/api/scans/${providerId}/${contentId}?language=${encodeURIComponent(selectedLanguage)}`);
    details.language = selectedLanguage;
    state.scanDetails = details;
    state.scanLanguage = selectedLanguage;
    devLog("scan_loaded", { provider: providerId, title: details.title, chapters: details.chapters.length, language: selectedLanguage });
    renderScanDetails();
  } catch (error) {
    devLog("scan_error", { provider: providerId, message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderScanDetails() {
  const detail = state.scanDetails;
  if (!detail) return;
  const languages = (detail.languages || []).length ? detail.languages : ["fr"];
  app.innerHTML = `
    <section class="hero">
      <div>${imageHtml(detail.image, detail.title)}</div>
      <div>
        <p class="eyebrow">${escapeHtml(detail.provider_name)} - ${escapeHtml(labelType(detail.content_type))}</p>
        <h1>${escapeHtml(detail.title)}</h1>
        <div class="meta-line">
          ${(detail.year ? [`<span>${escapeHtml(detail.year)}</span>`] : []).join("")}
          ${detail.status ? `<span class="pill">${escapeHtml(detail.status)}</span>` : ""}
          ${(detail.genres || []).slice(0, 8).map((genre) => `<span class="pill">${escapeHtml(genre)}</span>`).join("")}
        </div>
        <div class="scan-description">${escapeHtml(detail.description || "")}</div>
        <div class="hero-actions">
          <button id="scan-favorite-button" class="${detail.favorite ? "danger-button" : "primary-button"}" type="button">
            ${detail.favorite ? "Retirer des favoris" : "Ajouter aux favoris"}
          </button>
          <button class="secondary-button" type="button" data-view="scans">Recherche scans</button>
          <button id="tracking-follow-button" class="secondary-button" type="button">Suivre les chapitres</button>
          <select id="scan-detail-language" aria-label="Langue scan">
            ${languages.map((language) => `<option value="${escapeHtml(language)}" ${detail.language === language ? "selected" : ""}>${escapeHtml(language.toUpperCase())}</option>`).join("")}
          </select>
        </div>
      </div>
    </section>
    <section>
      <div class="section-header">
        <h2>Chapitres</h2>
        <span class="pill">${detail.chapters.length}</span>
      </div>
      <div class="chapter-tools">
        <input id="chapter-filter" type="search" autocomplete="off" placeholder="Filtrer ou taper un numero de chapitre">
        <button class="secondary-button" type="button" data-chapter-jump>Ouvrir</button>
      </div>
      <div class="chapter-list">
        ${detail.chapters.map(scanChapterHtml).join("") || `<div class="empty-state">Aucun chapitre disponible.</div>`}
      </div>
    </section>
  `;
  loadTrackingForDetails(true).catch(() => {});
}

function scanChapterHtml(chapter) {
  const progress = chapter.progress;
  const progressText = progress ? `${Math.round(percent(progress))}%` : "";
  const groups = (chapter.scanlation_groups || []).slice(0, 2).join(", ");
  const unavailable = !chapter.id;
  return `
    <button class="chapter-button" type="button" data-open-scan-chapter="${escapeHtml(chapter.id)}" data-chapter-number="${escapeHtml(chapter.chapter || "")}" data-chapter-label="${escapeHtml(chapter.title)}" ${unavailable ? "disabled" : ""}>
      <div>
        <strong>${escapeHtml(chapter.title)}</strong>
        <div class="meta-line">
          ${chapter.pages ? `<span>${escapeHtml(chapter.pages)} pages</span>` : "<span>Pages a charger</span>"}
          ${chapter.language ? `<span>${escapeHtml(chapter.language.toUpperCase())}</span>` : ""}
          ${groups ? `<span>${escapeHtml(groups)}</span>` : ""}
        </div>
        ${progress ? `<div class="progress-bar"><span style="width:${percent(progress)}%"></span></div>` : ""}
      </div>
      ${progressText ? `<span class="pill">${escapeHtml(progressText)}</span>` : ""}
    </button>
  `;
}

async function openScanChapter(chapterId, pageIndex = 0, mode = null) {
  const detail = state.scanDetails;
  if (!detail) return;
  const previousMode = state.scanReader?.mode;
  const previousZoom = state.scanReader?.zoom;
  disconnectScanObserver();
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    const result = await api(`/api/scans/${detail.provider_id}/${detail.content_id}/chapters/${chapterId}/pages`);
    const chapter = (detail.chapters || []).find((item) => item.id === chapterId) || { id: chapterId, title: "Chapitre" };
    const pages = result.pages || [];
    state.scanReader = {
      mode: mode || previousMode || "vertical",
      pageIndex: Math.max(0, Math.min(Number(pageIndex) || 0, Math.max(0, pages.length - 1))),
      zoom: clamp(Number(previousZoom) || SCAN_READER_MIN_ZOOM, SCAN_READER_MIN_ZOOM, SCAN_READER_MAX_ZOOM),
      chapter,
      pages,
    };
    devLog("scan_chapter_loaded", { provider: detail.provider_id, title: detail.title, pages: pages.length });
    renderScanReader();
    saveScanProgress(false).catch(() => {});
  } catch (error) {
    devLog("scan_pages_error", { provider: detail.provider_id, message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function readableScanChapters() {
  const chapters = state.scanDetails?.chapters || [];
  return chapters.filter((chapter) => chapter.id);
}

function currentScanChapterIndex() {
  const reader = state.scanReader;
  if (!reader) return -1;
  return readableScanChapters().findIndex((chapter) => chapter.id === reader.chapter.id);
}

function captureScanScrollAnchor() {
  if (!state.scanReader || state.scanReader.mode === "single") return null;
  const pages = Array.from(document.querySelectorAll(".scan-page[data-reader-page]"));
  if (!pages.length) return null;
  const stage = scanFullscreenStage();
  const viewport = appFullscreenActive("scan") && stage
    ? stage.getBoundingClientRect()
    : { top: 0, bottom: window.innerHeight };
  let best = null;
  pages.forEach((page) => {
    const rect = page.getBoundingClientRect();
    const visible = Math.min(rect.bottom, viewport.bottom) - Math.max(rect.top, viewport.top);
    if (visible <= 0) return;
    if (!best || visible > best.visible) {
      best = { page, rect, visible };
    }
  });
  if (!best) return null;
  return {
    index: Number(best.page.dataset.readerPage || 0),
    offset: Math.max(0, viewport.top - best.rect.top),
  };
}

function restoreScanScrollAnchor(anchor) {
  if (!anchor) return;
  window.requestAnimationFrame(() => {
    const page = document.querySelector(`[data-reader-page="${anchor.index}"]`);
    if (!page) return;
    if (appFullscreenActive("scan")) {
      const stage = scanFullscreenStage();
      if (!stage) return;
      const stageRect = stage.getBoundingClientRect();
      const pageTop = page.getBoundingClientRect().top - stageRect.top + stage.scrollTop;
      stage.scrollTop = Math.max(0, pageTop + anchor.offset);
      return;
    }
    const pageTop = page.getBoundingClientRect().top + window.scrollY;
    window.scrollTo({ top: Math.max(0, pageTop + anchor.offset), behavior: "auto" });
  });
}

function scanChapterOptions(selectedId) {
  return readableScanChapters().map((chapter) => `
    <option value="${escapeHtml(chapter.id)}" ${chapter.id === selectedId ? "selected" : ""}>
      ${escapeHtml(chapter.title)}
    </option>
  `).join("");
}

async function openAdjacentScanChapter(delta) {
  const chapters = readableScanChapters();
  const currentIndex = currentScanChapterIndex();
  const nextIndex = currentIndex + delta;
  if (nextIndex < 0 || nextIndex >= chapters.length) return;
  await saveScanProgress(false).catch(() => {});
  await openScanChapter(chapters[nextIndex].id, 0, state.scanReader?.mode || "vertical");
}

function filterScanChapters(value) {
  const query = String(value || "").trim().toLowerCase();
  document.querySelectorAll("[data-open-scan-chapter]").forEach((button) => {
    const number = String(button.dataset.chapterNumber || "").toLowerCase();
    const label = String(button.dataset.chapterLabel || "").toLowerCase();
    const visible = !query || number === query || number.includes(query) || label.includes(query);
    button.classList.toggle("hidden", !visible);
  });
}

async function jumpToFilteredScanChapter() {
  const input = document.getElementById("chapter-filter");
  const query = String(input?.value || "").trim().toLowerCase();
  if (!query) return;
  const chapter = readableScanChapters().find((item) => (
    String(item.chapter || "").toLowerCase() === query
    || String(item.chapter || "").toLowerCase().includes(query)
    || String(item.title || "").toLowerCase().includes(query)
  ));
  if (!chapter) {
    showToast("Chapitre introuvable");
    return;
  }
  await openScanChapter(chapter.id);
}

function renderScanReader() {
  const detail = state.scanDetails;
  const reader = state.scanReader;
  if (!detail || !reader) return;
  disconnectScanObserver();
  const pageCount = reader.pages.length;
  const pageIndex = Math.max(0, Math.min(reader.pageIndex || 0, Math.max(0, pageCount - 1)));
  reader.pageIndex = pageIndex;
  const isSingle = reader.mode === "single";
  reader.zoom = isSingle
    ? clamp(Number(reader.zoom) || SCAN_READER_MIN_ZOOM, SCAN_READER_MIN_ZOOM, SCAN_READER_MAX_ZOOM)
    : SCAN_READER_MIN_ZOOM;
  const chapterIndex = currentScanChapterIndex();
  const chapterCount = readableScanChapters().length;
  syncAppFullscreenClasses();
  app.innerHTML = `
    <section class="scan-reader">
      <header class="scan-reader-header">
        <div>
          <p class="eyebrow">${escapeHtml(detail.title)}</p>
          <h1>${escapeHtml(reader.chapter.title)}</h1>
          <div class="meta-line">
            <span>${escapeHtml(pageCount ? `${pageIndex + 1}/${pageCount}` : "0 page")}</span>
            ${reader.chapter.language ? `<span>${escapeHtml(reader.chapter.language.toUpperCase())}</span>` : ""}
          </div>
        </div>
        <div class="reader-actions">
          <button class="secondary-button" type="button" data-reader-back-details>Chapitres</button>
          <button class="secondary-button" type="button" data-reader-fullscreen>Plein écran</button>
          <select id="reader-chapter-picker" class="reader-chapter-picker" aria-label="Chapitre">
            ${scanChapterOptions(reader.chapter.id)}
          </select>
          <button class="secondary-button" type="button" data-reader-prev-chapter ${chapterIndex <= 0 ? "disabled" : ""}>Chapitre precedent</button>
          <button class="secondary-button" type="button" data-reader-next-chapter ${chapterIndex < 0 || chapterIndex >= chapterCount - 1 ? "disabled" : ""}>Chapitre suivant</button>
          <button class="${isSingle ? "secondary-button active" : "secondary-button"}" type="button" data-reader-mode="single">Page</button>
          <button class="${!isSingle ? "secondary-button active" : "secondary-button"}" type="button" data-reader-mode="vertical">Vertical</button>
          <button class="secondary-button" type="button" data-reader-prev ${pageIndex <= 0 ? "disabled" : ""}>Page prec.</button>
          <button class="secondary-button" type="button" data-reader-next ${pageIndex >= pageCount - 1 ? "disabled" : ""}>Page suiv.</button>
          <button class="primary-button" type="button" data-reader-complete>Termine</button>
        </div>
      </header>
      <div class="scan-reader-stage" style="--scan-reader-zoom:${reader.zoom}">
        ${
          isSingle
            ? `<div class="single-page">${singleScanPageHtml(reader.pages[pageIndex])}</div>`
            : `<div class="scan-page-list">${reader.pages.map((page) => singleScanPageHtml(page)).join("")}</div>`
        }
      </div>
    </section>
  `;
  updateFullscreenButtons();
  applyScanZoom();
  if (!isSingle) {
    setupScanObserver();
    if (!appFullscreenActive("scan")) {
      scrollScanPageIntoView(reader.pageIndex);
    }
  }
}

function singleScanPageHtml(page) {
  if (!page) return `<div class="empty-state">Page indisponible.</div>`;
  return `
    <figure class="scan-page" data-reader-page="${page.index}">
      <img src="${escapeHtml(page.url)}" alt="Page ${escapeHtml(page.index + 1)}" loading="lazy">
    </figure>
  `;
}

function disconnectScanObserver() {
  if (state.scanObserver) {
    state.scanObserver.disconnect();
    state.scanObserver = null;
  }
}

function setupScanObserver() {
  if (!("IntersectionObserver" in window)) return;
  const pages = document.querySelectorAll(".scan-page[data-reader-page]");
  state.scanObserver = new IntersectionObserver((entries) => {
    if (state.scanRestoringScroll) return;
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible || !state.scanReader) return;
    const nextIndex = Number(visible.target.dataset.readerPage || 0);
    if (Number.isFinite(nextIndex) && nextIndex !== state.scanReader.pageIndex) {
      state.scanReader.pageIndex = nextIndex;
      saveScanProgress(false).catch(() => {});
    }
  }, { threshold: [0.55, 0.75] });
  pages.forEach((page) => state.scanObserver.observe(page));
}

function scrollScanPageIntoView(index = state.scanReader?.pageIndex || 0) {
  const pageIndex = Math.max(0, Number(index) || 0);
  if (pageIndex <= 0) return;
  window.requestAnimationFrame(() => {
    document.querySelector(`[data-reader-page="${pageIndex}"]`)?.scrollIntoView({ block: "start" });
  });
}

function scanStageFromEvent(event) {
  const target = event.target instanceof Element ? event.target : null;
  return target?.closest(".scan-reader-stage") || null;
}

function scanFullscreenActive() {
  const stage = scanFullscreenStage();
  return Boolean(stage && (appFullscreenActive("scan") || fullscreenOwner(stage)));
}

function applyScanZoom() {
  const reader = state.scanReader;
  const stage = scanFullscreenStage();
  if (!reader || !stage) return;
  const zoom = clamp(Number(reader.zoom) || SCAN_READER_MIN_ZOOM, SCAN_READER_MIN_ZOOM, SCAN_READER_MAX_ZOOM);
  reader.zoom = zoom;
  stage.style.setProperty("--scan-reader-zoom", String(zoom));
  stage.classList.toggle("scan-reader-zoomed", zoom > SCAN_READER_MIN_ZOOM);
}

function scanReaderCanPan() {
  const reader = state.scanReader;
  if (!reader) return false;
  if (reader.mode === "single") return true;
  const zoom = Number(reader.zoom) || SCAN_READER_MIN_ZOOM;
  return zoom > SCAN_READER_MIN_ZOOM + 0.001;
}

function setScanZoom(nextZoom, originEvent = null) {
  const reader = state.scanReader;
  const stage = scanFullscreenStage();
  if (!reader || !stage || !scanFullscreenActive()) return;
  const currentZoom = clamp(Number(reader.zoom) || SCAN_READER_MIN_ZOOM, SCAN_READER_MIN_ZOOM, SCAN_READER_MAX_ZOOM);
  const zoom = clamp(nextZoom, SCAN_READER_MIN_ZOOM, SCAN_READER_MAX_ZOOM);
  if (Math.abs(zoom - currentZoom) < 0.001) return;

  const rect = stage.getBoundingClientRect();
  const pivotX = originEvent ? originEvent.clientX - rect.left : stage.clientWidth / 2;
  const pivotY = originEvent ? originEvent.clientY - rect.top : stage.clientHeight / 2;
  const scrollLeft = stage.scrollLeft + pivotX;
  const scrollTop = stage.scrollTop + pivotY;
  reader.zoom = zoom;
  applyScanZoom();
  window.requestAnimationFrame(() => {
    const scale = zoom / currentZoom;
    stage.scrollLeft = Math.max(0, scrollLeft * scale - pivotX);
    stage.scrollTop = Math.max(0, scrollTop * scale - pivotY);
  });
}

function handleScanReaderWheel(event) {
  const stage = scanStageFromEvent(event);
  const reader = state.scanReader;
  if (!stage || !reader || !scanFullscreenActive()) return;
  if (reader.mode !== "single") return;
  event.preventDefault();
  const direction = event.deltaY < 0 ? 1 : -1;
  const currentZoom = Number(reader.zoom) || SCAN_READER_MIN_ZOOM;
  setScanZoom(currentZoom + direction * SCAN_READER_ZOOM_STEP, event);
}

function handleScanPanStart(event) {
  if (event.button !== 0 || !state.scanReader) return;
  const stage = scanStageFromEvent(event);
  if (!stage || !scanFullscreenActive()) return;
  if (!scanReaderCanPan()) return;
  state.scanPan = {
    pointerId: event.pointerId,
    stage,
    startX: event.clientX,
    startY: event.clientY,
    startScrollLeft: stage.scrollLeft,
    startScrollTop: stage.scrollTop,
    dragged: false,
  };
  stage.setPointerCapture?.(event.pointerId);
}

function handleScanPanMove(event) {
  const pan = state.scanPan;
  if (!pan || pan.pointerId !== event.pointerId) return;
  const deltaX = event.clientX - pan.startX;
  const deltaY = event.clientY - pan.startY;
  if (!pan.dragged && Math.hypot(deltaX, deltaY) >= SCAN_READER_PAN_THRESHOLD) {
    pan.dragged = true;
    pan.stage.classList.add("is-panning");
  }
  if (!pan.dragged) return;
  event.preventDefault();
  pan.stage.scrollLeft = pan.startScrollLeft - deltaX;
  pan.stage.scrollTop = pan.startScrollTop - deltaY;
}

function handleScanPanEnd(event) {
  const pan = state.scanPan;
  if (!pan || pan.pointerId !== event.pointerId) return;
  if (pan.dragged) {
    event.preventDefault();
    state.scanSuppressClick = true;
    window.setTimeout(() => {
      state.scanSuppressClick = false;
    }, 250);
  }
  pan.stage.releasePointerCapture?.(event.pointerId);
  pan.stage.classList.remove("is-panning");
  state.scanPan = null;
}

function handleScanPageClick(event) {
  if (state.scanReader?.mode !== "single") return false;
  const stage = scanStageFromEvent(event);
  if (!stage) return false;
  event.preventDefault();
  if (state.scanSuppressClick) {
    state.scanSuppressClick = false;
    return true;
  }
  const rect = stage.getBoundingClientRect();
  moveScanPage(event.clientX < rect.left + rect.width / 2 ? -1 : 1);
  return true;
}

async function saveScanProgress(completed = false) {
  const detail = state.scanDetails;
  const reader = state.scanReader;
  if (!detail || !reader) return;
  const now = Date.now();
  if (!completed && now - state.scanProgressSentAt < 3500) return;
  state.scanProgressSentAt = now;
  await api("/api/scans/progress", {
    method: "POST",
    body: JSON.stringify({
      provider_id: detail.provider_id,
      provider_name: detail.provider_name,
      provider: detail.provider_name,
      content_id: detail.content_id,
      title: detail.title,
      image: detail.image,
      logo_url: detail.image,
      language: detail.language || state.scanLanguage,
      chapter_id: reader.chapter.id,
      chapter_title: reader.chapter.title,
      chapter_number: reader.chapter.chapter,
      page_index: reader.pageIndex || 0,
      current_page: (reader.pageIndex || 0) + 1,
      page_count: reader.pages.length,
      completed,
    }),
  });
  if (completed) showToast("Progression enregistree");
}

function setScanReaderMode(mode) {
  if (!state.scanReader) return;
  if (state.scanReader.mode !== mode) {
    state.scanReader.zoom = SCAN_READER_MIN_ZOOM;
  }
  state.scanReader.mode = mode;
  renderScanReader();
  saveScanProgress(false).catch(() => {});
}

function moveScanPage(delta) {
  const reader = state.scanReader;
  if (!reader) return;
  const wasSingle = reader.mode === "single";
  reader.mode = "single";
  if (!wasSingle) {
    reader.zoom = SCAN_READER_MIN_ZOOM;
  }
  reader.pageIndex = Math.max(0, Math.min((reader.pageIndex || 0) + delta, Math.max(0, reader.pages.length - 1)));
  renderScanReader();
  saveScanProgress(false).catch(() => {});
}

async function toggleScanFavorite() {
  const detail = state.scanDetails;
  if (!detail) return;
  if (detail.favorite) {
    await api(`/api/favorites/${detail.provider_id}/${detail.content_id}`, { method: "DELETE" });
    detail.favorite = false;
    showToast("Favori retire");
  } else {
    await api("/api/favorites", {
      method: "POST",
      body: JSON.stringify({ summary: detail.summary }),
    });
    detail.favorite = true;
    showToast("Favori ajoute");
  }
  renderScanDetails();
}

async function openContent(providerId, contentId) {
  setActiveView("details");
  state.currentEpisodeContext = null;
  updateNextEpisodeButton();
  devLog("open_content", { provider: providerId });
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    const details = await api(`/api/content/${providerId}/${contentId}`);
    state.details = details;
    devLog("content_loaded", { provider: providerId, title: details.title, seasons: details.seasons.length });
    renderDetails();
  } catch (error) {
    devLog("content_error", { provider: providerId, message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderDetails() {
  const detail = state.details;
  const isAnime = detail.content_type === "anime";
  app.innerHTML = `
    <section class="hero">
      <div>${imageHtml(detail.image, detail.title)}</div>
      <div>
        <p class="eyebrow">${escapeHtml(detail.provider_name)} · ${escapeHtml(labelType(detail.content_type))}</p>
        <h1>${escapeHtml(detail.title)}</h1>
        <div class="meta-line">
          ${(detail.year ? [`<span>${escapeHtml(detail.year)}</span>`] : []).join("")}
          ${(detail.genres || []).slice(0, 8).map((genre) => `<span class="pill">${escapeHtml(genre)}</span>`).join("")}
        </div>
        <div class="hero-actions">
          <button id="favorite-button" class="${detail.favorite ? "danger-button" : "primary-button"}" type="button">
            ${detail.favorite ? "Retirer des favoris" : "Ajouter aux favoris"}
          </button>
          ${isAnime ? `<button id="tracking-follow-button" class="secondary-button" type="button">Suivre</button>` : ""}
          ${isAnime ? `<label class="toggle"><input type="checkbox" id="auto-download-toggle"> <span>Auto-télécharger nouveaux épisodes</span></label>` : ""}
        </div>
      </div>
    </section>
    <section>
      <div class="section-header">
        <h2>Saisons et épisodes</h2>
      </div>
      <div class="season-list">
        ${detail.seasons.map(seasonHtml).join("") || `<div class="empty-state">Aucun épisode disponible.</div>`}
      </div>
      <div id="source-panel"></div>
    </section>
  `;
  if (isAnime) loadTrackingForDetails(false).catch(() => {});
}

function seasonHtml(season) {
  const episodes = season.episodes || [];
  return `
    <article class="season" data-season-id="${escapeHtml(season.id)}">
      <div class="season-title">
        <h3>${escapeHtml(season.title)}</h3>
        ${
          episodes.length
            ? `<span class="pill">${episodes.length} épisodes</span>`
            : `<button class="secondary-button" type="button" data-load-season="${escapeHtml(season.id)}">Charger</button>`
        }
      </div>
      ${
        episodes.length
          ? `<div class="episode-grid">${episodes.map((episode) => episodeHtml(episode)).join("")}</div>`
          : ""
      }
    </article>
  `;
}

function episodeHtml(episode) {
  const language = episode.language ? `<span class="pill">${escapeHtml(episode.language.toUpperCase())}</span>` : "";
  return `
    <button class="episode-button" type="button" data-episode-id="${escapeHtml(episode.id)}">
      <strong>${escapeHtml(episode.title)}</strong>
      ${language}
    </button>
  `;
}

async function loadSeason(seasonId) {
  const detail = state.details;
  devLog("season_load", { provider: detail.provider_id, title: detail.title });
  const season = await api(`/api/content/${detail.provider_id}/${detail.content_id}/seasons/${seasonId}`);
  detail.seasons = detail.seasons.map((item) => (item.id === seasonId ? season : item));
  devLog("season_loaded", { provider: detail.provider_id, title: season.title, episodes: season.episodes.length });
  renderDetails();
}

function sourceBadges(source) {
  const badges = [];
  if (source.quality) {
    badges.push(`<span class="badge badge-quality">${escapeHtml(source.quality)}</span>`);
  }
  if (source.has_subtitles) {
    badges.push(`<span class="badge badge-st">ST ✓</span>`);
  }
  if (source.source_type) {
    badges.push(`<span class="badge badge-type">${escapeHtml(String(source.source_type).toUpperCase())}</span>`);
  }
  if (source.provider_name) {
    badges.push(`<span class="badge badge-provider">${escapeHtml(source.provider_name)}</span>`);
  }
  return badges.join("");
}

function guessEpisodeNumber(title) {
  const match = String(title || "").match(/\d+/);
  return match ? Number(match[0]) : null;
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function buildEpisodeContext(episodeId) {
  const detail = state.details;
  if (!detail) return null;
  for (const season of detail.seasons || []) {
    const episodes = season.episodes || [];
    const index = episodes.findIndex((episode) => episode.id === episodeId);
    if (index < 0) continue;
    const episode = episodes[index];
    const nextEpisode = episodes[index + 1] || null;
    return {
      provider_id: detail.provider_id,
      content_id: detail.content_id,
      content_type: detail.content_type || "anime",
      season_id: season.id,
      season_title: season.title || "",
      season_number: optionalNumber(season.number),
      episode_id: episode.id,
      episode_title: episode.title || "",
      episode_number: optionalNumber(episode.number) || guessEpisodeNumber(episode.title) || index + 1,
      season_episode_index: index,
      season_episode_count: episodes.length,
      next_episode_id: nextEpisode?.id || "",
      next_episode_title: nextEpisode?.title || "",
      selected_source_name: state.currentEpisodeContext?.selected_source_name || "",
    };
  }
  return null;
}

function playbackEpisodeContext() {
  if (!state.currentEpisodeContext) return {};
  return {
    content_type: state.currentEpisodeContext.content_type,
    season_id: state.currentEpisodeContext.season_id,
    season_title: state.currentEpisodeContext.season_title,
    season_number: state.currentEpisodeContext.season_number,
    episode_id: state.currentEpisodeContext.episode_id,
    episode_title: state.currentEpisodeContext.episode_title,
    episode_number: state.currentEpisodeContext.episode_number,
    season_episode_index: state.currentEpisodeContext.season_episode_index,
    season_episode_count: state.currentEpisodeContext.season_episode_count,
  };
}

function updateNextEpisodeButton() {
  if (!nextEpisodeBtn) return;
  const nextEpisodeId = state.currentEpisodeContext?.next_episode_id || "";
  const enabled = Boolean(nextEpisodeId && state.currentPlayback && !state.currentPlayback.local);
  nextEpisodeBtn.disabled = !enabled;
  nextEpisodeBtn.title = enabled
    ? (state.currentEpisodeContext.next_episode_title || "Episode suivant")
    : "Aucun episode suivant";
}

function normalizeSourceName(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function pickEquivalentSource(sources, preferredName) {
  const normalized = normalizeSourceName(preferredName);
  if (!normalized) return sources[0] || null;
  return sources.find((source) => normalizeSourceName(source.name) === normalized)
    || sources.find((source) => normalizeSourceName(source.name).includes(normalized))
    || sources.find((source) => normalized.includes(normalizeSourceName(source.name)))
    || sources[0]
    || null;
}

async function loadSources(episodeId, options = {}) {
  const detail = state.details;
  const panel = document.getElementById("source-panel");
  devLog("episode_click", { provider: detail.provider_id, title: detail.title });
  state.currentEpisodeContext = buildEpisodeContext(episodeId);
  updateNextEpisodeButton();
  if (panel) {
    panel.innerHTML = `<div class="source-panel">Chargement des sources</div>`;
  }
  try {
    const result = await api(`/api/content/${detail.provider_id}/${detail.content_id}/episodes/${episodeId}/sources`);
    const sources = result.sources || [];
    devLog("sources_loaded", { provider: detail.provider_id, title: detail.title, sources: sources.length });
    if (panel) {
      panel.innerHTML = `
      <div class="source-panel">
        <div class="section-header">
          <h2>Sources</h2>
          <span class="pill">${sources.length}</span>
        </div>
        <div class="source-list">
          ${sources.map((source) => `
            <div class="source-button" data-source-row="${escapeHtml(source.id)}">
              <strong>${escapeHtml(source.name)}</strong>
              <div class="badge-row">${sourceBadges(source)}</div>
              <div class="source-actions">
                <button class="tiny-button" type="button" data-source-id="${escapeHtml(source.id)}" data-source-name="${escapeHtml(source.name)}">Lire</button>
                ${String(source.source_type || "").toLowerCase() === "iframe" ? "" : `<button class="tiny-button" type="button" data-download-source="${escapeHtml(source.id)}" data-source-name="${escapeHtml(source.name)}">Télécharger</button>`}
              </div>
            </div>
          `).join("")}
        </div>
      </div>
    `;
      if (options.scroll !== false) {
        panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
    return sources;
  } catch (error) {
    devLog("sources_error", { provider: detail.provider_id, title: detail.title, message: error.message }, "error");
    if (panel) {
      panel.innerHTML = `
      <div class="source-panel">
        <div class="empty-state">${escapeHtml(error.message)}</div>
        <button class="tiny-button" type="button" data-copy-text="${escapeHtml(error.message)}">Copier l'erreur</button>
      </div>
    `;
    }
    return [];
  }
}

async function startPlayback(sourceId, sourceName = "") {
  showToast("Préparation du lecteur");
  devLog("source_click", { source_name: sourceName });
  if (state.currentEpisodeContext) {
    state.currentEpisodeContext.selected_source_name = sourceName;
  }
  try {
    const playback = await api("/api/playback/start", {
      method: "POST",
      body: JSON.stringify({
        source_id: sourceId,
        episode_context: playbackEpisodeContext(),
      }),
    });
    state.currentPlayback = { ...playback, source_id: sourceId };
    devLog("player_open", { title: playback.title, media_kind: playback.media_kind, source_name: playback.source_name });
    if ((playback.subtitles || []).length) {
      devLog("subtitle_tracks_available", {
        title: playback.title,
        source_name: playback.source_name,
        count: playback.subtitles.length,
        tracks: playback.subtitles.map((track, index) => ({
          index,
          label: subtitleLabel(track, index),
          lang: track?.lang || track?.language || "",
          subtitle_source: track?.subtitle_source || track?.source || "",
          url_length: String(track?.url || "").length,
        })),
      });
    }
    openPlayer(playback);
    updateNextEpisodeButton();
  } catch (error) {
    showToast(error.message);
    devLog("player_open_error", { message: error.message }, "error");
  }
}

function setQualityLabel(text) {
  if (qualityToggle) qualityToggle.textContent = `Qualité: ${text}`;
}

function renderQualityMenu() {
  if (!qualityMenu) return;
  if (!state.qualityLevels.length) {
    qualityMenu.innerHTML = `<div class="quality-option">Aucun niveau</div>`;
    return;
  }
  const sorted = [...state.qualityLevels].sort((a, b) => (b.height || 0) - (a.height || 0));
  const options = [
    { mode: "max", label: "Auto max (recommandé)" },
    { mode: "auto", label: "Auto adaptive" },
    ...sorted.map((level) => ({
      mode: level.index,
      label: `${level.height || "?"}p${level.bitrate ? ` · ${(level.bitrate / 1000) | 0} kbps` : ""}`,
    })),
  ];
  qualityMenu.innerHTML = options
    .map((opt) => `
      <button class="quality-option ${state.qualityMode === opt.mode ? "active" : ""}" type="button" data-quality-mode="${opt.mode}">
        <span>${escapeHtml(opt.label)}</span>
        ${state.qualityMode === opt.mode ? "<span>✓</span>" : ""}
      </button>
    `)
    .join("");
}

function applyQualityMode(mode) {
  if (!state.hls) return;
  state.qualityMode = mode;
  if (mode === "max") {
    const last = state.qualityLevels.length - 1;
    if (last >= 0) {
      state.hls.currentLevel = last;
      const level = state.qualityLevels[last];
      setQualityLabel(`${level.height || "?"}p (max)`);
    } else {
      setQualityLabel("Auto");
    }
  } else if (mode === "auto") {
    state.hls.currentLevel = -1;
    setQualityLabel("Auto");
  } else if (typeof mode === "number") {
    state.hls.currentLevel = mode;
    const level = state.qualityLevels[mode];
    setQualityLabel(level ? `${level.height || "?"}p` : `${mode}`);
  }
  renderQualityMenu();
}

function toggleQualityMenu(force) {
  if (!qualityMenu) return;
  const willOpen = force !== undefined ? force : qualityMenu.classList.contains("hidden");
  qualityMenu.classList.toggle("hidden", !willOpen);
  qualityToggle?.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

function resetQualityState() {
  state.qualityLevels = [];
  state.qualityMode = state.preferredQuality || "max";
  setQualityLabel("Auto");
  if (qualityMenu) qualityMenu.classList.add("hidden");
}

function subtitleLabel(track, index = 0) {
  const label = String(track?.label || track?.name || "").trim();
  const lang = String(track?.lang || track?.language || "").trim();
  if (label && lang && label.toLowerCase() !== lang.toLowerCase()) {
    return `${label} (${lang})`;
  }
  return label || lang || `Piste ${index + 1}`;
}

function setSubtitleLabel(text) {
  if (subtitleToggle) subtitleToggle.textContent = `Sous-titres: ${text}`;
}

function normaliseSubtitleOffsetMs(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(number) : 0;
}

function subtitleOffsetLabel(offsetMs) {
  const seconds = normaliseSubtitleOffsetMs(offsetMs) / 1000;
  const sign = seconds > 0 ? "+" : "";
  const precision = Math.abs(seconds) >= 10 ? 0 : 1;
  return `${sign}${seconds.toFixed(precision)}s`;
}

function subtitleUrlWithOffset(url, offsetMs, cacheBust = "") {
  const offset = normaliseSubtitleOffsetMs(offsetMs);
  try {
    const parsed = new URL(url, window.location.origin);
    if (offset) {
      parsed.searchParams.set("offset_ms", String(offset));
    } else {
      parsed.searchParams.delete("offset_ms");
    }
    if (cacheBust) parsed.searchParams.set("subtitle_cb", String(cacheBust));
    if (parsed.origin === window.location.origin) {
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
    return parsed.toString();
  } catch (error) {
    const params = [];
    if (offset) params.push(`offset_ms=${encodeURIComponent(offset)}`);
    if (cacheBust) params.push(`subtitle_cb=${encodeURIComponent(cacheBust)}`);
    if (!params.length) return url;
    const separator = String(url).includes("?") ? "&" : "?";
    return `${url}${separator}${params.join("&")}`;
  }
}

function activeSubtitleTrackIndex() {
  const index = Number(state.subtitleMode);
  return Number.isInteger(index) && index >= 0 ? index : -1;
}

function activeSubtitleTrackMeta() {
  const index = activeSubtitleTrackIndex();
  return index >= 0 ? state.subtitleTracks[index] : null;
}

function firstCueStartFromElement(trackElement = state.subtitleTrackElement) {
  const cues = trackElement?.track?.cues;
  if (!cues || !cues.length) return null;
  let firstStart = Number.POSITIVE_INFINITY;
  for (let index = 0; index < cues.length; index += 1) {
    const cueStart = Number(cues[index]?.startTime);
    if (Number.isFinite(cueStart)) firstStart = Math.min(firstStart, cueStart);
  }
  return Number.isFinite(firstStart) ? firstStart : null;
}

function updateSubtitleFirstCueBaseTime(trackElement = state.subtitleTrackElement) {
  const firstStart = firstCueStartFromElement(trackElement);
  if (!Number.isFinite(firstStart)) {
    state.subtitleFirstCueBaseTime = null;
    return null;
  }
  const baseStart = firstStart - normaliseSubtitleOffsetMs(state.subtitleOffsetMs) / 1000;
  state.subtitleFirstCueBaseTime = Number.isFinite(baseStart) ? Math.max(0, baseStart) : null;
  renderSubtitleCalibration();
  return state.subtitleFirstCueBaseTime;
}

function currentSubtitleFirstStart() {
  if (Number.isFinite(state.subtitleFirstCueBaseTime)) {
    return state.subtitleFirstCueBaseTime;
  }
  const loadedStart = firstCueStartFromElement();
  if (!Number.isFinite(loadedStart)) return null;
  const baseStart = loadedStart - normaliseSubtitleOffsetMs(state.subtitleOffsetMs) / 1000;
  return Number.isFinite(baseStart) ? Math.max(0, baseStart) : null;
}

function subtitleCalibrationFields(draft = state.subtitleCalibrationDraft) {
  const trackIndex = Number.isInteger(draft?.trackIndex) ? draft.trackIndex : activeSubtitleTrackIndex();
  const trackMeta = trackIndex >= 0 ? state.subtitleTracks[trackIndex] : activeSubtitleTrackMeta();
  const firstSubtitleStart = Number(draft?.firstSubtitleStart);
  const videoTime = Number(draft?.videoTime);
  const offsetMs = normaliseSubtitleOffsetMs(draft?.offsetMs ?? state.subtitleOffsetMs);
  return {
    title: state.currentPlayback?.title || "",
    source_name: state.currentPlayback?.source_name || "",
    media_kind: state.currentPlayback?.media_kind || "",
    track_index: trackIndex,
    track_lang: trackMeta?.lang || trackMeta?.language || "",
    track_label: trackMeta ? subtitleLabel(trackMeta, Math.max(0, trackIndex)) : "",
    first_subtitle_start: Number.isFinite(firstSubtitleStart) ? Number(firstSubtitleStart.toFixed(3)) : null,
    video_time: Number.isFinite(videoTime) ? Number(videoTime.toFixed(3)) : null,
    offset_ms: offsetMs,
    offset_s: Number((offsetMs / 1000).toFixed(3)),
  };
}

function renderSubtitleCalibration() {
  if (!subtitleCalibration) return;
  const active = state.subtitleMode !== "off" && Boolean(activeSubtitleTrackMeta());
  const visible = active && !state.subtitleCalibrationHidden;
  subtitleCalibration.classList.toggle("hidden", !visible);
  if (!visible) return;

  const pending = Boolean(state.subtitleCalibrationDraft);
  subtitleCalibrationStart?.classList.toggle("hidden", pending);
  subtitleCalibrationConfirm?.classList.toggle("hidden", !pending);
  if (subtitleCalibrationOffset) {
    subtitleCalibrationOffset.textContent = pending ? subtitleOffsetLabel(state.subtitleCalibrationDraft.offsetMs) : "";
  }
}

function previewSubtitleCalibration() {
  if (state.subtitleMode === "off") return;
  const firstSubtitleStart = currentSubtitleFirstStart();
  const videoTime = Number(video.currentTime);
  if (!Number.isFinite(firstSubtitleStart) || !Number.isFinite(videoTime)) {
    showToast("Sous-titres pas encore charges");
    devLog("subtitle_calibration_unavailable", {
      title: state.currentPlayback?.title || "",
      source_name: state.currentPlayback?.source_name || "",
      reason: "missing_first_cue",
    }, "warning");
    return;
  }

  const offsetMs = Math.round((videoTime - firstSubtitleStart) * 1000);
  const previousOffsetMs = normaliseSubtitleOffsetMs(state.subtitleOffsetMs);
  const draft = {
    previousOffsetMs,
    offsetMs,
    firstSubtitleStart,
    videoTime,
    trackIndex: activeSubtitleTrackIndex(),
  };
  state.subtitleCalibrationDraft = draft;
  state.subtitleOffsetMs = offsetMs;
  applySubtitleTrack(state.subtitleMode, { keepCalibrationVisibility: true });
  devLog("subtitle_calibration_preview", subtitleCalibrationFields(draft));
  showToast(`Calage ST ${subtitleOffsetLabel(offsetMs)}`);
}

function validateSubtitleCalibration() {
  if (!state.subtitleCalibrationDraft) return;
  devLog("subtitle_calibration_validated", subtitleCalibrationFields());
  state.subtitleCalibrationDraft = null;
  state.subtitleCalibrationHidden = true;
  renderSubtitleCalibration();
  showToast("Calage ST valide");
}

function cancelSubtitleCalibration() {
  if (!state.subtitleCalibrationDraft) return;
  const draft = state.subtitleCalibrationDraft;
  devLog("subtitle_calibration_rejected", subtitleCalibrationFields(draft), "warning");
  state.subtitleOffsetMs = normaliseSubtitleOffsetMs(draft.previousOffsetMs);
  state.subtitleCalibrationDraft = null;
  state.subtitleCalibrationHidden = true;
  if (state.subtitleMode !== "off") {
    applySubtitleTrack(state.subtitleMode, { keepCalibrationVisibility: true });
  } else {
    renderSubtitleCalibration();
  }
  showToast("Calage ST annule");
}

function clearLazySubtitleTrack() {
  if (state.subtitleTrackElement) {
    state.subtitleTrackElement.remove();
    state.subtitleTrackElement = null;
  }
  state.subtitleFirstCueBaseTime = null;
  for (let index = 0; index < video.textTracks.length; index += 1) {
    video.textTracks[index].mode = "disabled";
  }
}

function renderSubtitleMenu() {
  if (!subtitleMenu) return;
  const labels = state.subtitleTracks.map((track, index) => subtitleLabel(track, index));
  const duplicateLabels = labels.filter((label, index) => labels.indexOf(label) !== index);
  const options = [
    { mode: "off", label: "Off" },
    ...state.subtitleTracks.map((track, index) => ({
      mode: String(index),
      label: duplicateLabels.includes(labels[index]) ? `${labels[index]} · Piste ${index + 1}` : labels[index],
    })),
  ];
  subtitleMenu.innerHTML = options
    .map((opt) => `
      <button class="subtitle-option ${String(state.subtitleMode) === opt.mode ? "active" : ""}" type="button" data-subtitle-mode="${opt.mode}">
        <span>${escapeHtml(opt.label)}</span>
        ${String(state.subtitleMode) === opt.mode ? "<span>✓</span>" : ""}
      </button>
    `)
    .join("");
}

function toggleSubtitleMenu(force) {
  if (!subtitleMenu || !subtitleToggle || subtitleToggle.disabled) return;
  const willOpen = force !== undefined ? force : subtitleMenu.classList.contains("hidden");
  subtitleMenu.classList.toggle("hidden", !willOpen);
  subtitleToggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

function resetSubtitleState(tracks = []) {
  clearLazySubtitleTrack();
  state.subtitleTracks = tracks.filter((track) => track?.url);
  state.subtitleMode = "off";
  state.subtitleOffsetMs = 0;
  state.subtitleCalibrationDraft = null;
  state.subtitleCalibrationHidden = true;
  state.subtitleTrackLoadNonce += 1;
  if (state.hls) {
    state.hls.subtitleDisplay = false;
    state.hls.subtitleTrack = -1;
  }
  setSubtitleLabel("Off");
  if (subtitleToggle) {
    subtitleToggle.disabled = state.subtitleTracks.length === 0;
    subtitleToggle.setAttribute("aria-expanded", "false");
  }
  if (subtitleMenu) subtitleMenu.classList.add("hidden");
  renderSubtitleMenu();
  renderSubtitleCalibration();
}

function applySubtitleTrack(mode, options = {}) {
  clearLazySubtitleTrack();
  state.subtitleMode = mode;
  if (state.hls) {
    state.hls.subtitleDisplay = false;
    state.hls.subtitleTrack = -1;
  }
  if (mode === "off") {
    state.subtitleOffsetMs = 0;
    state.subtitleCalibrationDraft = null;
    state.subtitleCalibrationHidden = true;
    setSubtitleLabel("Off");
    renderSubtitleMenu();
    renderSubtitleCalibration();
    return;
  }

  const index = Number(mode);
  const trackMeta = Number.isInteger(index) ? state.subtitleTracks[index] : null;
  if (!trackMeta?.url) {
    state.subtitleMode = "off";
    state.subtitleOffsetMs = 0;
    state.subtitleCalibrationDraft = null;
    state.subtitleCalibrationHidden = true;
    setSubtitleLabel("Off");
    renderSubtitleMenu();
    renderSubtitleCalibration();
    return;
  }
  if (!options.keepCalibrationVisibility) {
    state.subtitleOffsetMs = 0;
    state.subtitleCalibrationDraft = null;
    state.subtitleCalibrationHidden = false;
  }

  const track = document.createElement("track");
  track.kind = "subtitles";
  track.label = subtitleLabel(trackMeta, index);
  if (trackMeta.lang) track.srclang = trackMeta.lang;
  state.subtitleTrackLoadNonce += 1;
  track.src = subtitleUrlWithOffset(trackMeta.url, state.subtitleOffsetMs, state.subtitleTrackLoadNonce);
  track.default = true;
  track.addEventListener(
    "load",
    () => {
      for (let textTrackIndex = 0; textTrackIndex < video.textTracks.length; textTrackIndex += 1) {
        video.textTracks[textTrackIndex].mode = video.textTracks[textTrackIndex].label === track.label ? "showing" : "disabled";
      }
      updateSubtitleFirstCueBaseTime(track);
    },
    { once: true },
  );
  video.appendChild(track);
  state.subtitleTrackElement = track;
  const offsetText = state.subtitleOffsetMs ? ` (${subtitleOffsetLabel(state.subtitleOffsetMs)})` : "";
  setSubtitleLabel(`${trackMeta.lang || trackMeta.label || "On"}${offsetText}`);
  renderSubtitleMenu();
  renderSubtitleCalibration();
}

function cleanupIframePlayer() {
  if (state.iframeMessageHandler) {
    window.removeEventListener("message", state.iframeMessageHandler);
    state.iframeMessageHandler = null;
  }
  if (state.iframePlayer) {
    state.iframePlayer.remove();
    state.iframePlayer = null;
  }
  state.iframeProgress = null;
  video.style.display = "";
}

function parseVideasyMessage(event) {
  if (event.origin !== "https://player.videasy.net") return null;
  let data = event.data;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch (error) {
      return null;
    }
  }
  if (!data || typeof data !== "object") return null;
  const timestamp = Number(data.timestamp ?? data.current_time ?? data.currentTime);
  if (!Number.isFinite(timestamp)) return null;
  const duration = Number(data.duration);
  const progress = Number(data.progress);
  return {
    currentTime: Math.max(0, timestamp),
    duration: Number.isFinite(duration) && duration > 0 ? duration : null,
    progress: Number.isFinite(progress) ? progress : null,
  };
}

function savedVideoPosition(playback) {
  const progress = playback?.progress || {};
  if (progress.completed) return 0;
  const position = Number(progress.position || 0);
  return Number.isFinite(position) && position > 0 ? position : 0;
}

function iframeUrlWithProgress(url, position) {
  if (!position || position <= 0) return url;
  const value = Number.isInteger(position) ? String(position) : position.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  try {
    const parsed = new URL(url, window.location.href);
    parsed.searchParams.set("progress", value);
    return parsed.toString();
  } catch (_error) {
    const separator = String(url || "").includes("?") ? "&" : "?";
    return `${url}${separator}progress=${encodeURIComponent(value)}`;
  }
}

function queueVideoSeek(position) {
  if (!position || position <= 0) return;
  video.addEventListener(
    "loadedmetadata",
    () => {
      try {
        const upperBound = Number.isFinite(video.duration) && video.duration > 2
          ? Math.max(0, video.duration - 2)
          : position;
        video.currentTime = Math.min(position, upperBound);
      } catch (error) {
        devLog("resume_seek_error", { message: error.message }, "warning");
      }
    },
    { once: true },
  );
}

function settleResumeChoice(choice) {
  const pending = state.pendingResumeChoice;
  if (!pending) return;
  state.pendingResumeChoice = null;
  resumeModal?.classList.add("hidden");
  pending.resolve(choice);
}

function promptResumeChoice(playback) {
  const position = savedVideoPosition(playback);
  if (!resumeModal || position <= 0) return Promise.resolve("restart");
  settleResumeChoice("cancel");
  if (resumeModalPosition) {
    resumeModalPosition.textContent = `Position sauvegardee: ${formatSeconds(position)}`;
  }
  resumeModal.classList.remove("hidden");
  return new Promise((resolve) => {
    state.pendingResumeChoice = { playbackId: playback.playback_id, resolve };
  });
}

function cancelResumeChoice() {
  settleResumeChoice("cancel");
}

function openPlayer(playback) {
  playerTitle.textContent = playback.title || "AutoFlix";
  playerView.classList.remove("hidden");
  updateFullscreenButtons();
  updateNextEpisodeButton();
  state.progressSentAt = 0;
  cancelResumeChoice();
  cleanupIframePlayer();
  video.pause();
  video.removeAttribute("src");
  while (video.firstChild) video.removeChild(video.firstChild);
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  resetQualityState();
  if (downloadCurrentBtn) {
    downloadCurrentBtn.disabled = playback.media_kind === "iframe";
  }
  resetSubtitleState(playback.subtitles || []);
  applyStoredPlayerState();

  const resumePosition = savedVideoPosition(playback);
  if (resumePosition > 0) {
    promptResumeChoice(playback).then((choice) => {
      if (choice === "cancel") return;
      if (state.currentPlayback?.playback_id !== playback.playback_id) return;
      loadPlaybackMedia(playback, choice === "resume" ? resumePosition : 0, true);
    });
    return;
  }

  loadPlaybackMedia(playback, 0, true);
}

function loadPlaybackMedia(playback, resumePosition = 0, autoplay = true) {
  cleanupIframePlayer();
  video.pause();
  video.removeAttribute("src");
  while (video.firstChild) video.removeChild(video.firstChild);
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  resetQualityState();
  resetSubtitleState(playback.subtitles || []);
  applyStoredPlayerState();
  queueVideoSeek(resumePosition);

  if (playback.media_kind === "iframe") {
    video.style.display = "none";
    const iframe = document.createElement("iframe");
    iframe.className = "player-iframe";
    iframe.src = iframeUrlWithProgress(playback.stream_url, resumePosition);
    iframe.allowFullscreen = true;
    iframe.setAttribute("allowfullscreen", "true");
    iframe.setAttribute("allow", "autoplay; fullscreen; encrypted-media; picture-in-picture");
    iframe.referrerPolicy = "strict-origin-when-cross-origin";
    state.iframePlayer = iframe;
    state.iframeMessageHandler = (event) => {
      const progress = parseVideasyMessage(event);
      if (!progress) return;
      state.iframeProgress = progress;
      postProgress(false).catch(() => {});
    };
    window.addEventListener("message", state.iframeMessageHandler);
    playerView.appendChild(iframe);
    setQualityLabel(playback.quality || "Iframe");
    return;
  }

  if (playback.media_kind === "hls" && window.Hls && Hls.isSupported()) {
    const proxiedSource = playback.stream_url;
    const passthroughSource = playback.passthrough_stream_url || "";
    let activeSource = passthroughSource || proxiedSource;
    let triedProxyFallback = !passthroughSource;
    state.hls = new Hls({ enableWorker: true, capLevelToPlayerSize: false });
    state.hls.subtitleDisplay = false;
    state.hls.subtitleTrack = -1;
    state.hls.loadSource(activeSource);
    state.hls.attachMedia(video);
    state.hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
      state.qualityLevels = (data.levels || []).map((level, index) => ({
        index,
        height: level.height,
        bitrate: level.bitrate,
        name: level.name,
      }));
      applyQualityMode(state.preferredQuality || "max");
      if (autoplay) video.play().catch(() => {});
    });
    state.hls.on(Hls.Events.LEVEL_SWITCHED, (_event, data) => {
      if (state.qualityMode === "auto") {
        const lvl = state.qualityLevels[data.level];
        if (lvl) setQualityLabel(`Auto · ${lvl.height || "?"}p`);
      }
    });
    state.hls.on(Hls.Events.ERROR, (_event, data) => {
      devLog(
        "hls_error",
        {
          type: data.type,
          details: data.details,
          fatal: Boolean(data.fatal),
          response_code: data.response?.code,
          mode: activeSource === passthroughSource ? "passthrough" : "proxy",
        },
        data.fatal ? "error" : "warning",
      );
      if (
        data.fatal &&
        passthroughSource &&
        proxiedSource &&
        !triedProxyFallback &&
        data.type === Hls.ErrorTypes.NETWORK_ERROR
      ) {
        triedProxyFallback = true;
        activeSource = proxiedSource;
        devLog("hls_fallback_proxy", { source_name: playback.source_name }, "warning");
        state.hls.stopLoad();
        state.hls.loadSource(proxiedSource);
        state.hls.startLoad();
        return;
      }
    });
  } else {
    video.src = playback.stream_url;
    if (autoplay) video.play().catch(() => {});
    setQualityLabel(playback.quality || "Direct");
  }
}

async function postProgress(completed = false, options = {}) {
  if (!state.currentPlayback) return;
  if (state.currentPlayback.local || !state.currentPlayback.playback_id) return;
  if (state.currentPlayback.media_kind === "iframe" && !state.iframeProgress && !completed) return;
  const force = Boolean(options.force);
  const now = Date.now();
  if (!completed && !force && now - state.progressSentAt < 10000) return;
  state.progressSentAt = now;
  const iframeProgress = state.currentPlayback.media_kind === "iframe" ? state.iframeProgress : null;
  const currentTime = iframeProgress
    ? iframeProgress.currentTime
    : Number.isFinite(video.currentTime) ? video.currentTime : 0;
  const duration = iframeProgress
    ? iframeProgress.duration
    : Number.isFinite(video.duration) ? video.duration : null;
  try {
    await api("/api/progress", {
      method: "POST",
      body: JSON.stringify({
        playback_id: state.currentPlayback.playback_id,
        current_time: currentTime,
        timestamp: currentTime,
        duration,
        completed,
      }),
    });
    if (completed) showToast("Progression enregistrée");
  } catch (error) {
    showToast(error.message);
  }
}

function closePlayer() {
  devLog("playback_closed", { title: state.currentPlayback?.title });
  cancelResumeChoice();
  postProgress(false, { force: true });
  if (appFullscreenActive("video")) {
    exitAppFullscreen().catch(() => {});
  }
  if (fullscreenOwner(playerView)) {
    exitFullscreenMode().catch(() => {});
  }
  video.pause();
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  cleanupIframePlayer();
  resetQualityState();
  resetSubtitleState();
  if (downloadCurrentBtn) {
    downloadCurrentBtn.disabled = false;
  }
  playerView.classList.add("hidden");
  state.currentPlayback = null;
  updateFullscreenButtons();
  updateNextEpisodeButton();
}

function playLocalDownload(jobId, title) {
  state.currentPlayback = { playback_id: null, source_id: null, title, local: true };
  devLog("local_play", { job_id: jobId, title });
  openPlayer({
    title: title || "Téléchargement",
    media_kind: "mp4",
    stream_url: `/api/downloads/${encodeURIComponent(jobId)}/file`,
    subtitles: [],
    quality: "Local",
  });
}

async function downloadCurrentPlayback() {
  if (!state.currentPlayback?.source_id) {
    showToast("Aucune source active");
    return;
  }
  if (state.currentPlayback.media_kind === "iframe") {
    showToast("Téléchargement indisponible pour ce lecteur");
    return;
  }
  await submitDownload(state.currentPlayback.source_id, state.currentPlayback.title || "");
}

async function playNextEpisode() {
  const context = state.currentEpisodeContext;
  const nextEpisodeId = context?.next_episode_id || "";
  if (!nextEpisodeId) {
    showToast("Aucun episode suivant");
    updateNextEpisodeButton();
    return;
  }

  const preferredSource = state.currentPlayback?.source_name
    || context.selected_source_name
    || "";
  await postProgress(false, { force: true });
  const sources = await loadSources(nextEpisodeId, { scroll: false });
  const source = pickEquivalentSource(sources, preferredSource);
  if (!source) {
    showToast("Aucune source disponible pour l'episode suivant");
    return;
  }
  await startPlayback(source.id, source.name || preferredSource);
}

async function submitDownload(sourceId, sourceName = "") {
  try {
    const result = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({ source_id: sourceId }),
    });
    showToast(`Téléchargement ${result.download.state}: ${sourceName || result.download.title}`);
    devLog("download_submit", { state: result.download.state, title: result.download.title });
    refreshDownloadsBadge().catch(() => {});
  } catch (error) {
    devLog("download_error", { message: error.message }, "error");
    showToast(error.message);
  }
}

async function toggleFavorite() {
  const detail = state.details;
  if (!detail) return;
  if (detail.favorite) {
    await api(`/api/favorites/${detail.provider_id}/${detail.content_id}`, { method: "DELETE" });
    detail.favorite = false;
    showToast("Favori retiré");
  } else {
    await api("/api/favorites", {
      method: "POST",
      body: JSON.stringify({ summary: detail.summary }),
    });
    detail.favorite = true;
    showToast("Favori ajouté");
  }
  renderDetails();
}

function trackingDetail(isScan = false) {
  return isScan ? state.scanDetails : state.details;
}

function trackingPayload(detail, isScan, enabled, autoDownload = false) {
  return {
    enabled,
    auto_download: isScan ? false : autoDownload,
    media_kind: isScan ? "scan" : "video",
    content_type: detail.content_type || (isScan ? "manga" : "anime"),
    provider_name: detail.provider_name,
    title: detail.title,
    image: detail.image,
    language: detail.language || state.scanLanguage,
  };
}

function applyTrackingControls(tracking = {}, isScan = false) {
  const followButton = document.getElementById("tracking-follow-button");
  const enabled = Boolean(tracking.follow_enabled);
  if (followButton) {
    followButton.dataset.followEnabled = enabled ? "true" : "false";
    followButton.textContent = enabled
      ? (isScan ? "Ne plus suivre les chapitres" : "Ne plus suivre")
      : (isScan ? "Suivre les chapitres" : "Suivre");
    followButton.classList.toggle("danger-button", enabled);
    followButton.classList.toggle("secondary-button", !enabled);
  }
  const checkbox = document.getElementById("auto-download-toggle");
  if (checkbox) {
    checkbox.checked = Boolean(tracking.auto_download);
    checkbox.disabled = !enabled && !checkbox.checked;
  }
}

async function loadTrackingForDetails(isScan = false) {
  const detail = trackingDetail(isScan);
  if (!detail) return;
  try {
    const result = await api(`/api/tracking/${detail.provider_id}/${detail.content_id}`);
    applyTrackingControls(result.tracking || {}, isScan);
  } catch (_error) {
    applyTrackingControls({}, isScan);
  }
}

async function saveTrackingSettings(isScan, enabled, autoDownload = false) {
  const detail = trackingDetail(isScan);
  if (!detail) return null;
  const result = await api(`/api/tracking/${detail.provider_id}/${detail.content_id}/settings`, {
    method: "POST",
    body: JSON.stringify(trackingPayload(detail, isScan, enabled, autoDownload)),
  });
  applyTrackingControls(result.tracking || {}, isScan);
  return result.tracking;
}

async function toggleLocalTracking(isScan = false) {
  const button = document.getElementById("tracking-follow-button");
  const enabled = button?.dataset.followEnabled !== "true";
  const checkbox = document.getElementById("auto-download-toggle");
  const autoDownload = !isScan && enabled ? Boolean(checkbox?.checked) : false;
  await saveTrackingSettings(isScan, enabled, autoDownload);
  showToast(enabled ? "Suivi activé" : "Suivi désactivé");
}

async function setAutoDownload(enabled) {
  const detail = state.details;
  if (!detail) return;
  const followButton = document.getElementById("tracking-follow-button");
  const followEnabled = enabled || followButton?.dataset.followEnabled === "true";
  try {
    await saveTrackingSettings(false, followEnabled, enabled);
    showToast(enabled ? "Auto-téléchargement activé" : "Auto-téléchargement désactivé");
  } catch (error) {
    showToast(error.message);
  }
}

async function renderFavorites() {
  setActiveView("favorites");
  const result = await api("/api/favorites");
  app.innerHTML = `
    <div class="section-header">
      <div>
        <p class="eyebrow">Bibliothèque</p>
        <h1>Favoris</h1>
      </div>
    </div>
    <div class="grid">${result.favorites.map(mediaCard).join("") || `<div class="empty-state">Aucun favori.</div>`}</div>
  `;
}

async function renderTracking() {
  setActiveView("tracking");
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    await api("/api/tracking/refresh", { method: "POST" }).catch(() => null);
    const [trackResult, favResult] = await Promise.all([
      api("/api/tracking"),
      api("/api/favorites"),
    ]);
    const favIndex = new Map(favResult.favorites.map((fav) => [`${fav.provider_id}:${fav.content_id}`, fav]));
    const items = trackResult.tracking
      .filter((entry) => entry.follow_enabled || entry.auto_download)
      .map((entry) => {
        const fav = favIndex.get(`${entry.provider_id}:${entry.content_id}`);
        return { ...entry, favorite: fav };
      });
    app.innerHTML = `
      <div class="section-header">
        <div>
          <p class="eyebrow">Anime / Manga</p>
          <h1>À suivre</h1>
        </div>
        <button class="tiny-button" type="button" id="refresh-tracking">Actualiser</button>
      </div>
      <div class="tracking-list">
        ${items.map(trackingItemHtml).join("") || `<div class="empty-state">Aucun titre suivi. Active le suivi depuis une fiche anime ou manga.</div>`}
      </div>
    `;
  } catch (error) {
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function trackingItemHtml(entry) {
  const isScan = entry.media_kind === "scan" || entry.content_type === "manga";
  const fav = entry.favorite;
  const title = fav?.title || entry.title || "Titre suivi";
  const image = fav?.image || entry.image || "";
  const typeLabel = isScan ? "Manga/Scan" : labelType(entry.content_type || "anime");
  const statusLine = isScan
    ? (entry.last_available_chapter ? `Chapitre ${entry.last_available_chapter} connu` : "Suivi chapitres actif")
    : (entry.last_available_episode ? `Episode ${entry.last_available_episode} connu` : "Suivi épisodes actif");
  const statusClass = "releasing";
  const auto = entry.auto_download ? "checked" : "";
  const progressLine = isScan
    ? [
        entry.last_read_chapter ? `Local: ch. ${entry.last_read_chapter}` : "",
        entry.chapter_count ? `${entry.chapter_count} chapitres` : "",
      ].filter(Boolean).join(" · ")
    : [
        entry.last_completed_episode ? `Local: E${entry.last_completed_episode}` : "",
        entry.last_available_episode ? `Dernier épisode: E${entry.last_available_episode}` : "",
      ].filter(Boolean).join(" · ");
  const openAttr = isScan ? "data-open-scan" : "data-open-content";
  const followControl = isScan
    ? `<span class="muted">Notifications nouveaux chapitres</span>`
    : `<label class="toggle">
        <input type="checkbox" data-tracking-toggle="${escapeHtml(entry.provider_id)}:${escapeHtml(entry.content_id)}" ${auto}>
        <span>Auto-télécharger</span>
      </label>`;
  return `
    <div class="tracking-item">
      ${imageHtml(image, title, "tracking-thumb")}
      <div>
        <strong>${escapeHtml(title)}</strong>
        <div class="meta-line">
          <span class="badge">${escapeHtml(typeLabel)}</span>
          <span class="tracking-status ${statusClass}">${escapeHtml(statusLine)}</span>
          <span>${escapeHtml(entry.provider_name || entry.provider_id)}</span>
        </div>
        ${progressLine ? `<div class="meta-line">${escapeHtml(progressLine)}</div>` : ""}
        ${followControl}
      </div>
      <button class="tiny-button" type="button" ${openAttr}="${escapeHtml(entry.provider_id)}:${escapeHtml(entry.content_id)}">Voir</button>
    </div>
  `;
}

async function renderHistory() {
  setActiveView("history");
  const result = await api("/api/home");
  state.home = result;
  app.innerHTML = `
    <div class="section-header">
      <div>
        <p class="eyebrow">Lecture</p>
        <h1>Historique</h1>
      </div>
      <button class="tiny-button danger" type="button" id="clear-history" ${result.history.length ? "" : "disabled"}>Effacer l'historique</button>
    </div>
    <div class="history-list">${result.history.map(historyItem).join("") || `<div class="empty-state">Aucun historique.</div>`}</div>
  `;
}

async function deleteHistoryEntry(historyKey) {
  if (!historyKey) return;
  await api("/api/history", {
    method: "DELETE",
    body: JSON.stringify({ history_key: historyKey }),
  });
  showToast("Entree retiree de l'historique");
  if (state.currentView === "history") {
    await renderHistory();
  } else {
    await loadHome();
  }
}

async function clearHistory() {
  if (!window.confirm("Effacer tout l'historique local ?")) return;
  await api("/api/history", {
    method: "DELETE",
    body: JSON.stringify({ all: true }),
  });
  showToast("Historique efface");
  if (state.currentView === "history") {
    await renderHistory();
  } else {
    await loadHome();
  }
}

async function renderDownloads() {
  setActiveView("downloads");
  app.innerHTML = `<div class="loading-state">Chargement</div>`;
  try {
    const result = await api("/api/downloads");
    state.downloads = result.downloads;
    state.downloadPath = result.download_path || "";
    state.ffmpegAvailable = result.ffmpeg_available;
    app.innerHTML = `
      <div class="section-header">
        <div>
          <p class="eyebrow">Local</p>
          <h1>Téléchargements</h1>
        </div>
        <div class="inline-actions">
          <button class="tiny-button danger" type="button" id="clear-done-downloads">Supprimer terminés</button>
          <button class="tiny-button" type="button" id="refresh-downloads">Actualiser</button>
        </div>
      </div>
      <div class="download-summary">
        ${downloadSummaryHtml()}
      </div>
      <div class="download-filters">
        ${downloadFilterButtonsHtml()}
      </div>
      ${
        result.ffmpeg_available
          ? ""
          : `<div class="download-error">FFmpeg introuvable. Installe ffmpeg et ajoute-le au PATH (https://ffmpeg.org).</div>`
      }
      <div class="download-list">
        ${downloadListHtml()}
      </div>
    `;
  } catch (error) {
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function downloadCounts(downloads = state.downloads) {
  return {
    all: downloads.length,
    active: downloads.filter((job) => DOWNLOAD_ACTIVE_STATES.has(job.state)).length,
    done: downloads.filter((job) => job.state === "done").length,
    errors: downloads.filter((job) => job.state === "failed").length,
  };
}

function downloadSummaryHtml() {
  const counts = downloadCounts();
  return `
    <div class="download-folder">
      <span>Dossier courant</span>
      <strong>${escapeHtml(state.downloadPath || "")}</strong>
    </div>
    <div class="download-counters">
      <span class="pill">${counts.all} total</span>
      <span class="pill">${counts.active} actifs</span>
      <span class="pill">${counts.done} terminés</span>
      <span class="pill">${counts.errors} erreurs</span>
    </div>
  `;
}

function downloadFilterButtonsHtml() {
  const counts = downloadCounts();
  const filters = [
    ["all", "Tous", counts.all],
    ["active", "Actifs", counts.active],
    ["done", "Terminés", counts.done],
    ["errors", "Erreurs", counts.errors],
  ];
  return filters.map(([key, label, count]) => (
    `<button class="tiny-button download-filter ${state.downloadFilter === key ? "active" : ""}" type="button" data-download-filter="${key}">${label} (${count})</button>`
  )).join("");
}

function filteredDownloads() {
  if (state.downloadFilter === "active") {
    return state.downloads.filter((job) => DOWNLOAD_ACTIVE_STATES.has(job.state));
  }
  if (state.downloadFilter === "done") {
    return state.downloads.filter((job) => job.state === "done");
  }
  if (state.downloadFilter === "errors") {
    return state.downloads.filter((job) => job.state === "failed");
  }
  return state.downloads;
}

function downloadListHtml() {
  const jobs = filteredDownloads();
  return jobs.map(downloadItemHtml).join("") || `<div class="empty-state">Aucun téléchargement.</div>`;
}

function updateDownloadsPanel() {
  const summary = document.querySelector(".download-summary");
  if (summary) summary.innerHTML = downloadSummaryHtml();
  const filters = document.querySelector(".download-filters");
  if (filters) filters.innerHTML = downloadFilterButtonsHtml();
  const list = document.querySelector(".download-list");
  if (list) list.innerHTML = downloadListHtml();
}

function downloadItemHtml(job) {
  const jobState = job.state || "queued";
  const percentValue = Math.round(job.percent || 0);
  const sizeText = job.size ? formatBytes(job.size) : "";
  const isActive = DOWNLOAD_ACTIVE_STATES.has(jobState);
  const canRetry = jobState === "failed";
  const canPlay = jobState === "done";
  const canOpenFolder = Boolean(job.output_path) && !isActive;
  const error = job.error ? `<div class="download-error">${escapeHtml(job.error)}</div>` : "";
  const errorActions = job.error
    ? `<button class="tiny-button" type="button" data-copy-text="${escapeHtml(job.error)}">Copier l'erreur</button>`
    : "";
  return `
    <div class="download-item">
      <header>
        <div>
          <strong>${escapeHtml(job.title || "Téléchargement")}</strong>
          <div class="download-meta">
            <span>${escapeHtml(job.provider || "")}</span>
            ${job.quality ? `<span>${escapeHtml(job.quality)}</span>` : ""}
            ${job.source_name ? `<span>${escapeHtml(job.source_name)}</span>` : ""}
            ${sizeText ? `<span>${escapeHtml(sizeText)}</span>` : ""}
            ${job.auto ? `<span>Auto</span>` : ""}
          </div>
        </div>
        <span class="download-state ${jobState}">${escapeHtml(jobState)}</span>
      </header>
      ${
        isActive
          ? `<div class="progress-bar"><span style="width:${percentValue}%"></span></div>
             <div class="download-meta"><span>${percentValue}%</span></div>`
          : ""
      }
      <div class="download-meta"><span>${escapeHtml(job.output_path || "")}</span></div>
      ${error}
      <div class="download-actions">
        ${isActive ? `<button class="tiny-button danger" type="button" data-cancel-download="${escapeHtml(job.id)}">Annuler</button>` : ""}
        ${canPlay ? `<button class="tiny-button" type="button" data-play-download="${escapeHtml(job.id)}" data-job-title="${escapeHtml(job.title || "")}">Lire</button>` : ""}
        ${canRetry ? `<button class="tiny-button" type="button" data-retry-download="${escapeHtml(job.id)}">Réessayer</button>` : ""}
        ${canOpenFolder ? `<button class="tiny-button" type="button" data-open-download="${escapeHtml(job.id)}">Ouvrir le dossier</button>` : ""}
        ${!isActive ? `<button class="tiny-button danger" type="button" data-delete-download="${escapeHtml(job.id)}">Supprimer</button>` : ""}
        ${errorActions}
      </div>
    </div>
  `;
}

function formatBytes(bytes) {
  if (!bytes) return "";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

async function renderSettings() {
  setActiveView("settings");
  const result = await api("/api/settings");
  const ffmpegResult = await api("/api/system/ffmpeg").catch(() => ({ available: false, path: "" }));
  const prefs = result.preferences || {};
  state.preferredQuality = prefs.default_quality || "max";
  app.innerHTML = `
    <div class="section-header">
      <div>
        <p class="eyebrow">AutoFlix</p>
        <h1>Réglages</h1>
      </div>
    </div>
    <form id="settings-form" class="settings-panel">
      <div class="field">
        <label for="settings-language">Langue</label>
        <select id="settings-language">
          <option value="fr" ${prefs.language === "fr" ? "selected" : ""}>Français</option>
          <option value="en" ${prefs.language === "en" ? "selected" : ""}>English</option>
        </select>
      </div>
      <div class="field">
        <label for="settings-player">Lecteur</label>
        <select id="settings-player">
          <option value="integrated" selected>Intégré</option>
        </select>
      </div>
      <div class="field">
        <label for="settings-default-quality">Qualité par défaut</label>
        <select id="settings-default-quality">
          <option value="max" ${prefs.default_quality === "max" ? "selected" : ""}>Auto max (recommandé)</option>
          <option value="auto" ${prefs.default_quality === "auto" ? "selected" : ""}>Auto adaptive</option>
        </select>
      </div>
      <div class="field">
        <label for="settings-download-path">Dossier de téléchargement</label>
        <input id="settings-download-path" type="text" value="${escapeHtml(prefs.download_path || "")}" autocomplete="off">
        <span class="muted">FFmpeg: ${ffmpegResult.available ? `<span style="color:var(--accent-2)">disponible${ffmpegResult.path ? " · " + escapeHtml(ffmpegResult.path) : ""}</span>` : `<span style="color:#f48a85">introuvable</span>`}</span>
      </div>
      <div class="field">
        <label for="settings-check-interval">Intervalle de vérification du suivi local (minutes)</label>
        <input id="settings-check-interval" type="number" min="5" max="720" value="${escapeHtml(prefs.check_interval_minutes || 30)}">
      </div>
      <div class="field">
        <label class="toggle">
          <input type="checkbox" id="settings-notifications" ${prefs.notifications_enabled ? "checked" : ""}>
          <span>Notifications natives</span>
        </label>
      </div>
      <div class="field">
        <label class="toggle">
          <input type="checkbox" id="settings-start-with-windows" ${prefs.start_with_windows ? "checked" : ""}>
          <span>Lancer AutoFlix au d&eacute;marrage de Windows</span>
        </label>
      </div>
      <div class="field">
        <label class="toggle">
          <input type="checkbox" id="settings-minimize-to-tray" ${prefs.minimize_to_tray !== false ? "checked" : ""}>
          <span>Garder AutoFlix dans la zone de notification &agrave; la fermeture</span>
        </label>
      </div>
      <div class="inline-actions">
        <button class="primary-button" type="submit">Enregistrer</button>
      </div>
    </form>
  `;
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    language: document.getElementById("settings-language").value,
    player: document.getElementById("settings-player").value,
    download_path: document.getElementById("settings-download-path").value.trim(),
    check_interval_minutes: Number(document.getElementById("settings-check-interval").value),
    notifications_enabled: document.getElementById("settings-notifications").checked,
    start_with_windows: document.getElementById("settings-start-with-windows").checked,
    minimize_to_tray: document.getElementById("settings-minimize-to-tray").checked,
    default_quality: document.getElementById("settings-default-quality").value,
  };
  await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
  state.preferredQuality = payload.default_quality;
  devLog("settings_saved", { language: payload.language, default_quality: payload.default_quality });
  showToast("Réglages enregistrés");
}

async function resumeEntry(entry) {
  if (entry?.media_kind === "scan") {
    const chapterId = entry.chapter_id || entry.episode_id;
    if (!entry.provider_id || !entry.content_id || !chapterId) {
      showToast("Reprise scan indisponible");
      return;
    }
    devLog("scan_resume", { provider: entry.provider_id, title: entry.series_title, chapter: entry.episode_title });
    await openScanDetails(entry.provider_id, entry.content_id, entry.language || state.scanLanguage || "fr");
    await openScanChapter(chapterId, Number(entry.page_index || 0));
    return;
  }
  if (!entry?.provider_id || !entry?.content_id || !entry?.episode_id) {
    showToast("Reprise indisponible pour cette entrée");
    return;
  }
  devLog("resume", { provider: entry.provider_id, title: entry.series_title, episode: entry.episode_title });
  await openContent(entry.provider_id, entry.content_id);
  await loadSources(entry.episode_id);
}

function fillProviders() {
  const select = document.getElementById("provider-filter");
  select.innerHTML = `<option value="all">Tous</option>` + state.providers.map((provider) => (
    `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.label)}</option>`
  )).join("");
}

async function refreshDownloadsBadge() {
  try {
    const result = await api("/api/downloads");
    state.downloads = result.downloads;
    state.downloadPath = result.download_path || state.downloadPath || "";
    state.ffmpegAvailable = result.ffmpeg_available;
    const active = result.downloads.filter((d) => DOWNLOAD_ACTIVE_STATES.has(d.state)).length;
    if (downloadsBadge) {
      if (active > 0) {
        downloadsBadge.textContent = String(active);
        downloadsBadge.classList.remove("hidden");
      } else {
        downloadsBadge.classList.add("hidden");
      }
    }
    if (state.currentView === "downloads") {
      updateDownloadsPanel();
    }
  } catch (_error) {
    // ignore
  }
}

async function pollNotifications() {
  try {
    const result = await api("/api/notifications/pending");
    for (const note of result.notifications || []) {
      showToast(`${note.title}${note.body ? ` — ${note.body}` : ""}`);
    }
    if ((result.notifications || []).length) {
      refreshDownloadsBadge().catch(() => {});
    }
  } catch (_error) {
    // ignore
  }
}

document.addEventListener("submit", (event) => {
  if (event.target.id === "search-form") {
    event.preventDefault();
    runSearch().catch((error) => showToast(error.message));
  }
  if (event.target.id === "scan-search-form") {
    event.preventDefault();
    runScanSearch().catch((error) => showToast(error.message));
  }
  if (event.target.id === "settings-form") {
    saveSettings(event).catch((error) => showToast(error.message));
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "reader-chapter-picker") {
    saveScanProgress(false).catch(() => {});
    openScanChapter(event.target.value, 0, state.scanReader?.mode || "vertical").catch((error) => showToast(error.message));
    return;
  }
  if (event.target.id === "scan-detail-language" && state.scanDetails) {
    openScanDetails(state.scanDetails.provider_id, state.scanDetails.content_id, event.target.value).catch((error) => showToast(error.message));
    return;
  }
  if (event.target.id === "auto-download-toggle") {
    setAutoDownload(event.target.checked).catch((error) => showToast(error.message));
    return;
  }
  const trackingToggle = event.target.closest("[data-tracking-toggle]");
  if (trackingToggle) {
    const [providerId, contentId] = trackingToggle.dataset.trackingToggle.split(":");
    api(`/api/tracking/${providerId}/${contentId}/auto-download`, {
      method: "POST",
      body: JSON.stringify({ enabled: trackingToggle.checked }),
    })
      .then(() => showToast(trackingToggle.checked ? "Auto-téléchargement activé" : "Désactivé"))
      .catch((error) => showToast(error.message));
  }
});

document.addEventListener("click", (event) => {
  handleClick(event).catch((error) => showToast(error.message));
});

document.addEventListener("input", (event) => {
  if (event.target.id === "chapter-filter") {
    filterScanChapters(event.target.value);
  }
});

document.addEventListener("wheel", handleScanReaderWheel, { passive: false });
document.addEventListener("pointerdown", handleScanPanStart);
document.addEventListener("pointermove", handleScanPanMove);
document.addEventListener("pointerup", handleScanPanEnd);
document.addEventListener("pointercancel", handleScanPanEnd);

document.addEventListener("click", (event) => {
  if (qualityMenu && !qualityMenu.classList.contains("hidden")) {
    if (!event.target.closest(".quality-control")) {
      toggleQualityMenu(false);
    }
  }
  if (subtitleMenu && !subtitleMenu.classList.contains("hidden")) {
    if (!event.target.closest(".subtitle-control")) {
      toggleSubtitleMenu(false);
    }
  }
});

async function handleClick(event) {
  const copyButton = event.target.closest("[data-copy-text]");
  if (copyButton) {
    await copyText(copyButton.dataset.copyText);
    return;
  }

  if (handleScanPageClick(event)) {
    return;
  }

  const nav = event.target.closest("[data-view]");
  if (nav) {
    const view = nav.dataset.view;
    if (view !== "scans" || state.scanReader) {
      await saveScanProgress(false).catch(() => {});
      if (appFullscreenActive("scan")) {
        await exitAppFullscreen();
      } else if (fullscreenOwner(scanFullscreenStage())) {
        await exitFullscreenMode();
      }
      disconnectScanObserver();
      state.scanReader = null;
    }
    if (view === "home") await loadHome();
    if (view === "scans") await renderScans();
    if (view === "favorites") await renderFavorites();
    if (view === "tracking") await renderTracking();
    if (view === "history") await renderHistory();
    if (view === "downloads") await renderDownloads();
    if (view === "settings") await renderSettings();
    return;
  }

  const opener = event.target.closest("[data-open-content]");
  if (opener) {
    const [providerId, contentId] = opener.dataset.openContent.split(":");
    devLog("result_opened", { provider: providerId, title: opener.querySelector("h3")?.textContent || "" });
    await openContent(providerId, contentId);
    return;
  }

  const scanOpener = event.target.closest("[data-open-scan]");
  if (scanOpener) {
    const [providerId, contentId] = scanOpener.dataset.openScan.split(":");
    devLog("scan_result_opened", { provider: providerId, title: scanOpener.querySelector("h3")?.textContent || "" });
    await openScanDetails(providerId, contentId);
    return;
  }

  const scanChapterButton = event.target.closest("[data-open-scan-chapter]");
  if (scanChapterButton) {
    await openScanChapter(scanChapterButton.dataset.openScanChapter);
    return;
  }

  if (event.target.closest("[data-chapter-jump]")) {
    await jumpToFilteredScanChapter();
    return;
  }

  if (event.target.id === "scan-favorite-button") {
    await toggleScanFavorite();
    return;
  }

  if (event.target.closest("[data-reader-back-details]")) {
    await saveScanProgress(false).catch(() => {});
    if (appFullscreenActive("scan")) {
      await exitAppFullscreen();
    } else if (fullscreenOwner(scanFullscreenStage())) {
      await exitFullscreenMode();
    }
    renderScanDetails();
    return;
  }

  if (event.target.closest("[data-reader-fullscreen]")) {
    await enterAppFullscreen("scan");
    return;
  }

  const readerMode = event.target.closest("[data-reader-mode]");
  if (readerMode) {
    setScanReaderMode(readerMode.dataset.readerMode);
    return;
  }

  if (event.target.closest("[data-reader-prev]")) {
    moveScanPage(-1);
    return;
  }

  if (event.target.closest("[data-reader-next]")) {
    moveScanPage(1);
    return;
  }

  if (event.target.closest("[data-reader-prev-chapter]")) {
    await openAdjacentScanChapter(-1);
    return;
  }

  if (event.target.closest("[data-reader-next-chapter]")) {
    await openAdjacentScanChapter(1);
    return;
  }

  if (event.target.closest("[data-reader-complete]")) {
    await saveScanProgress(true);
    return;
  }

  const loadSeasonButton = event.target.closest("[data-load-season]");
  if (loadSeasonButton) {
    await loadSeason(loadSeasonButton.dataset.loadSeason);
    return;
  }

  const episodeButton = event.target.closest("[data-episode-id]");
  if (episodeButton) {
    await loadSources(episodeButton.dataset.episodeId);
    return;
  }

  const downloadSourceButton = event.target.closest("[data-download-source]");
  if (downloadSourceButton) {
    await submitDownload(downloadSourceButton.dataset.downloadSource, downloadSourceButton.dataset.sourceName || "");
    return;
  }

  const sourceButton = event.target.closest("[data-source-id]");
  if (sourceButton) {
    await startPlayback(sourceButton.dataset.sourceId, sourceButton.dataset.sourceName || sourceButton.querySelector("strong")?.textContent || "");
    return;
  }

  const deleteHistoryButton = event.target.closest("[data-history-delete]");
  if (deleteHistoryButton) {
    await deleteHistoryEntry(deleteHistoryButton.dataset.historyDelete);
    return;
  }

  if (event.target.id === "clear-history") {
    await clearHistory();
    return;
  }

  const historyButton = event.target.closest("[data-history-index]");
  if (historyButton) {
    const entry = state.home?.history?.[Number(historyButton.dataset.historyIndex)];
    await resumeEntry(entry);
    return;
  }

  const resumeButton = event.target.closest("[data-resume-index='resume']");
  if (resumeButton) {
    await resumeEntry(state.home?.resume);
    return;
  }

  if (event.target.id === "favorite-button") {
    await toggleFavorite();
    return;
  }

  if (event.target.id === "tracking-follow-button") {
    await toggleLocalTracking(state.currentView === "scans");
    return;
  }

  if (event.target.id === "refresh-tracking") {
    await renderTracking();
    return;
  }

  if (event.target.id === "refresh-downloads" || event.target.id === "clear-done-downloads") {
    if (event.target.id === "clear-done-downloads") {
      const confirmed = window.confirm("Supprimer les téléchargements terminés, annulés ou en erreur, ainsi que leurs fichiers ?");
      if (!confirmed) return;
      const result = await api("/api/downloads/clear-done", { method: "POST" });
      showToast(`${result.removed || 0} téléchargement(s) supprimé(s)`);
    }
    await renderDownloads();
    return;
  }

  const downloadFilter = event.target.closest("[data-download-filter]");
  if (downloadFilter) {
    state.downloadFilter = downloadFilter.dataset.downloadFilter || "all";
    updateDownloadsPanel();
    return;
  }

  if (event.target.id === "quality-toggle") {
    toggleQualityMenu();
    renderQualityMenu();
    return;
  }

  if (event.target.id === "subtitle-toggle") {
    toggleSubtitleMenu();
    renderSubtitleMenu();
    return;
  }

  if (event.target.id === "subtitle-calibration-start") {
    previewSubtitleCalibration();
    return;
  }

  if (event.target.id === "subtitle-calibration-validate") {
    validateSubtitleCalibration();
    return;
  }

  if (event.target.id === "subtitle-calibration-cancel") {
    cancelSubtitleCalibration();
    return;
  }

  const qualityOption = event.target.closest("[data-quality-mode]");
  if (qualityOption) {
    const raw = qualityOption.dataset.qualityMode;
    const mode = raw === "max" || raw === "auto" ? raw : Number(raw);
    applyQualityMode(mode);
    toggleQualityMenu(false);
    return;
  }

  const subtitleOption = event.target.closest("[data-subtitle-mode]");
  if (subtitleOption) {
    applySubtitleTrack(subtitleOption.dataset.subtitleMode || "off");
    toggleSubtitleMenu(false);
    return;
  }

  if (event.target.id === "download-current") {
    await downloadCurrentPlayback();
    return;
  }

  if (event.target.id === "next-episode") {
    await playNextEpisode();
    return;
  }

  const resumeChoice = event.target.closest("[data-resume-choice]");
  if (resumeChoice) {
    settleResumeChoice(resumeChoice.dataset.resumeChoice || "restart");
    return;
  }

  const cancelDownload = event.target.closest("[data-cancel-download]");
  if (cancelDownload) {
    await api(`/api/downloads/${cancelDownload.dataset.cancelDownload}/cancel`, { method: "POST" });
    showToast("Annulation demandée");
    refreshDownloadsBadge().catch(() => {});
    return;
  }

  const retryDownload = event.target.closest("[data-retry-download]");
  if (retryDownload) {
    await api(`/api/downloads/${retryDownload.dataset.retryDownload}/retry`, { method: "POST" });
    showToast("Téléchargement relancé");
    refreshDownloadsBadge().catch(() => {});
    return;
  }

  const openDownload = event.target.closest("[data-open-download]");
  if (openDownload) {
    await api(`/api/downloads/${openDownload.dataset.openDownload}/open-folder`, { method: "POST" });
    return;
  }

  const playDownload = event.target.closest("[data-play-download]");
  if (playDownload) {
    playLocalDownload(playDownload.dataset.playDownload, playDownload.dataset.jobTitle || "");
    return;
  }

  const deleteDownload = event.target.closest("[data-delete-download]");
  if (deleteDownload) {
    const confirmed = window.confirm("Supprimer ce téléchargement et son fichier du disque ?");
    if (!confirmed) return;
    const result = await api(`/api/downloads/${deleteDownload.dataset.deleteDownload}`, { method: "DELETE" });
    showToast(result.file_deleted ? "Fichier supprimé" : "Téléchargement supprimé");
    refreshDownloadsBadge().catch(() => {});
    if (state.currentView === "downloads") await renderDownloads();
    return;
  }

}

async function toggleVideoFullscreen(event) {
  event?.preventDefault();
  event?.stopPropagation();
  event?.stopImmediatePropagation?.();
  const now = Date.now();
  if (now - state.videoFullscreenToggleAt < VIDEO_FULLSCREEN_TOGGLE_GUARD_MS) return;
  state.videoFullscreenToggleAt = now;
  state.videoNativeFullscreenSuppressUntil = now + VIDEO_NATIVE_FULLSCREEN_SUPPRESS_MS;
  if (appFullscreenActive("video") || fullscreenOwner(video)) {
    if (fullscreenOwner(video)) {
      await exitFullscreenMode();
    }
    await exitAppFullscreen();
    return;
  }
  await enterAppFullscreen("video");
}

document.getElementById("player-close").addEventListener("click", closePlayer);
document.getElementById("mark-watched").addEventListener("click", () => postProgress(true));
video.addEventListener("dblclick", toggleVideoFullscreen, { capture: true });
video.addEventListener("timeupdate", () => postProgress(false));
video.addEventListener("volumechange", saveStoredPlayerState);
video.addEventListener("ended", () => {
  devLog("playback_ended", { title: state.currentPlayback?.title });
  postProgress(true);
});
video.addEventListener("error", () => {
  devLog("video_error", { title: state.currentPlayback?.title, code: video.error?.code }, "error");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && appFullscreenActive()) {
    event.preventDefault();
    exitAppFullscreen().catch((error) => showToast(error.message));
    return;
  }
  const keyTarget = event.target instanceof Element ? event.target : null;
  const editingText = keyTarget?.closest("input, textarea, select, [contenteditable='true']");
  if (state.currentView === "scans" && state.scanReader?.mode === "single" && !editingText) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveScanPage(-1);
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      moveScanPage(1);
    }
  }
});
document.addEventListener("fullscreenchange", handleFullscreenChange);
document.addEventListener("webkitfullscreenchange", handleFullscreenChange);
document.addEventListener("MSFullscreenChange", handleFullscreenChange);
window.addEventListener("beforeunload", () => {
  devLog("window_close", { view: state.currentView, title: state.currentPlayback?.title });
  saveScanProgress(false).catch(() => {});
});

async function init() {
  try {
    applyStoredPlayerState();
    updateNextEpisodeButton();
    const providerPayload = await api("/api/providers");
    state.providers = providerPayload.providers;
    fillProviders();
    loadScanProviders().catch(() => {});
    try {
      const settings = await api("/api/settings");
      state.preferredQuality = settings.preferences?.default_quality || "max";
    } catch (_error) {
      // ignore
    }
    devLog("init", { providers: state.providers.length });
    await loadHome();
    refreshDownloadsBadge().catch(() => {});
    state.pollTimer = window.setInterval(() => {
      pollNotifications().catch(() => {});
      refreshDownloadsBadge().catch(() => {});
    }, 8000);
  } catch (error) {
    devLog("init_error", { message: error.message }, "error");
    app.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

init();

