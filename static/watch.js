let player = null;
let ytApiReady = false;
let pendingVideoId = null;

let wordEvents = [];
let imageCache = {};
let lastTriggeredIdx = -1;
let lastKnownTime = -1;
let slotIndex = 0;
let pollingTimer = null;

// ── YouTube IFrame API ─────────────────────────────────────────────────────

function onYouTubeIframeAPIReady() {
  ytApiReady = true;
  if (pendingVideoId) {
    createPlayer(pendingVideoId);
    pendingVideoId = null;
  }
}

function createPlayer(videoId) {
  if (player && typeof player.loadVideoById === "function") {
    player.loadVideoById(videoId);
    startPolling();
    return;
  }
  player = new YT.Player("yt-player", {
    videoId,
    playerVars: { rel: 0, modestbranding: 1, playsinline: 1 },
    events: { onReady: () => startPolling() },
  });
}

// ── Polling / sync ─────────────────────────────────────────────────────────

function startPolling() {
  if (pollingTimer) clearInterval(pollingTimer);
  pollingTimer = setInterval(tick, 300);
}

function tick() {
  if (!player || typeof player.getCurrentTime !== "function") return;
  const t = player.getCurrentTime();
  if (lastKnownTime >= 0 && Math.abs(t - lastKnownTime) > 3) {
    lastTriggeredIdx = -1;
    for (let i = 0; i < wordEvents.length; i++) {
      if (wordEvents[i].time > t) break;
      lastTriggeredIdx = i;
    }
  }
  lastKnownTime = t;
  checkTriggers(t);
}

function checkTriggers(currentTime) {
  for (let i = lastTriggeredIdx + 1; i < wordEvents.length; i++) {
    const ev = wordEvents[i];
    if (ev.time > currentTime) break;
    lastTriggeredIdx = i;

    let imgData = imageCache[i];
    if (!imgData) {
      if (ev.emoji)      imgData = { type: "emoji", char: ev.emoji };
      else if (ev.number)  imgData = { type: "number", text: ev.number };
      else if (ev.image_url) imgData = { type: "image", url: ev.image_url };
      if (imgData) imageCache[i] = imgData;
    }

    if (imgData) showImage(imgData, ev.word);
  }
}

// ── Image display ──────────────────────────────────────────────────────────

function showImage(imgData, spanishWord) {
  for (let i = 0; i < 3; i++) document.getElementById(`slot-${i}`).classList.remove("current");
  const slot = document.getElementById(`slot-${slotIndex}`);
  slotIndex = (slotIndex + 1) % 3;

  const label = `<div class="word-label">${escHtml(spanishWord)}</div>`;

  if (imgData.type === "number") {
    slot.innerHTML = `<div class="number-display">${escHtml(imgData.text)}</div>${label}`;
    slot.classList.add("active", "current", "just-updated");
    setTimeout(() => slot.classList.remove("just-updated"), 600);
  } else if (imgData.type === "emoji") {
    slot.innerHTML = `<div class="emoji-display">${imgData.char}</div>${label}`;
    slot.classList.add("active", "current", "just-updated");
    setTimeout(() => slot.classList.remove("just-updated"), 600);
  } else if (imgData.type === "image") {
    const display = () => {
      slot.innerHTML = `<img src="${imgData.url}" alt="${escHtml(spanishWord)}" />${label}`;
      slot.classList.add("active", "current", "just-updated");
      setTimeout(() => slot.classList.remove("just-updated"), 600);
    };
    const img = new Image();
    img.onload = display;
    img.src = imgData.url;
    if (img.complete) display();
  }
}

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Preview cards ──────────────────────────────────────────────────────────

function addPreviewCard(imgData, spanishWord) {
  const preview = document.getElementById("loading-preview");
  if (!preview || preview.children.length >= 30) return;
  const card = document.createElement("div");
  card.className = "preview-card";
  if (imgData.type === "number") {
    card.innerHTML = `<span class="preview-number">${escHtml(imgData.text)}</span><span class="preview-word">${escHtml(spanishWord)}</span>`;
  } else if (imgData.type === "emoji") {
    card.innerHTML = `<span class="preview-emoji">${imgData.char}</span><span class="preview-word">${escHtml(spanishWord)}</span>`;
  } else {
    card.innerHTML = `<img class="preview-img" src="${imgData.url}" alt="" /><span class="preview-word">${escHtml(spanishWord)}</span>`;
  }
  preview.appendChild(card);
}

function buildImageCache(events) {
  const targets = events.slice(0, 60);
  targets.forEach((ev, i) => {
    let imgData = null;
    if (ev.emoji)       imgData = { type: "emoji", char: ev.emoji };
    else if (ev.number)  imgData = { type: "number", text: ev.number };
    else if (ev.image_url) {
      imgData = { type: "image", url: ev.image_url };
      new Image().src = ev.image_url; // warm browser cache
    }
    if (imgData) {
      imageCache[i] = imgData;
      addPreviewCard(imgData, ev.word);
    }
  });
}

// ── UI helpers ─────────────────────────────────────────────────────────────

function resetSlots() {
  for (let i = 0; i < 3; i++) {
    const slot = document.getElementById(`slot-${i}`);
    slot.innerHTML = '<div class="slot-placeholder"></div>';
    slot.className = "image-slot";
  }
}

function setLevelBadge(level) {
  const badge = document.getElementById("level-badge");
  badge.textContent = `Level: ${level}`;
  badge.className = `level-badge level-${level.toLowerCase().replace(/ /g, "-")}`;
}

function showError(msg) {
  document.getElementById("loading").classList.add("hidden");
  const sec = document.getElementById("error-section");
  document.getElementById("error-msg").textContent = msg;
  sec.classList.remove("hidden");
}

// ── Fake progress bar ──────────────────────────────────────────────────────

let _fakeTimer = null;
let _fakeVal = 0;

function startFakeProgress() {
  _fakeVal = 0;
  if (_fakeTimer) clearInterval(_fakeTimer);
  _fakeTimer = setInterval(() => {
    _fakeVal += (92 - _fakeVal) * 0.05;
    const bar = document.getElementById("loading-bar");
    if (bar) bar.style.width = `${_fakeVal}%`;
  }, 100);
}

function finishProgress() {
  if (_fakeTimer) { clearInterval(_fakeTimer); _fakeTimer = null; }
  const bar = document.getElementById("loading-bar");
  if (bar) bar.style.width = "100%";
}

// ── Main load ──────────────────────────────────────────────────────────────

async function loadVideo(vid) {
  // Show loading UI
  const sv = document.getElementById("skeleton-video");
  sv.style.backgroundImage = `url(https://img.youtube.com/vi/${vid}/mqdefault.jpg)`;
  sv.classList.add("has-thumbnail");
  document.getElementById("loading-bar").style.width = "0%";
  document.getElementById("loading-preview").innerHTML = "";
  document.getElementById("loading-stage").textContent = "Loading…";
  document.getElementById("loading").classList.remove("hidden");
  document.getElementById("player-section").classList.add("hidden");
  document.getElementById("error-section").classList.add("hidden");
  startFakeProgress();

  let data;
  try {
    const res = await fetch(`/api/video/${encodeURIComponent(vid)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    showError("Could not load this video.");
    return;
  }

  // Reset state
  wordEvents = data.word_events || [];
  imageCache = {};
  lastTriggeredIdx = -1;
  lastKnownTime = -1;
  slotIndex = 0;

  // Update level badge in skeleton
  const skBadge = document.getElementById("skeleton-level-badge");
  skBadge.textContent = `Level: ${data.level}`;
  skBadge.className = `level-badge level-${data.level.toLowerCase().replace(/ /g, "-")}`;

  document.getElementById("loading-stage").textContent = "Preparing vocabulary…";

  // Build image cache instantly (all data is pre-fetched)
  buildImageCache(wordEvents);

  finishProgress();
  await new Promise(r => setTimeout(r, 200));

  document.getElementById("loading").classList.add("hidden");
  resetSlots();
  document.getElementById("player-section").classList.remove("hidden");
  setLevelBadge(data.level);
  renderWatchNext(vid, data.level);

  if (ytApiReady) {
    createPlayer(vid);
  } else {
    pendingVideoId = vid;
  }
}

// ── Watch next ─────────────────────────────────────────────────────────────

function formatDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function renderWatchNext(currentVid, currentLevel) {
  let videos;
  try {
    const res = await fetch("/api/videos");
    videos = await res.json();
  } catch (_) { return; }

  const next = videos
    .filter(v => v.video_id !== currentVid && v.level === currentLevel)
    .slice(0, 3);

  if (!next.length) return;

  const grid = document.getElementById("watch-next-grid");
  next.forEach(v => {
    const a = document.createElement("a");
    a.href = `/watch?v=${encodeURIComponent(v.video_id)}`;
    a.className = "video-card";
    a.innerHTML = `
      <div class="card-thumb">
        <img src="${escHtml(v.thumbnail)}" alt="${escHtml(v.title)}" loading="lazy" />
        <span class="card-duration">${escHtml(formatDuration(v.duration))}</span>
      </div>
      <div class="card-info">
        <h3 class="card-title">${escHtml(v.title)}</h3>
        <div class="card-meta">
          <span class="level-badge level-${escHtml(v.level.toLowerCase().replace(/ /g, "-"))}">${escHtml(v.level)}</span>
          <span class="card-channel">Easy Spanish</span>
        </div>
      </div>
    `;
    grid.appendChild(a);
  });

  document.getElementById("watch-next").classList.remove("hidden");
}

// ── Boot ───────────────────────────────────────────────────────────────────

const _vid = new URLSearchParams(window.location.search).get("v");
if (_vid) {
  loadVideo(_vid);
} else {
  showError("No video specified.");
}

fetch("/api/version").then(r => r.json()).then(d => {
  const el = document.getElementById("app-version");
  if (el) el.textContent = `v${d.version}`;
}).catch(() => {});
