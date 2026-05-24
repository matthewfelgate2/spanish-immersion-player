let player = null;
let ytApiReady = false;
let pendingVideoId = null;

// ── Loading stage messages ─────────────────────────────────────────────────
const LOADING_MESSAGES = [
  "Looking at YouTube…",
  "Fetching subtitles…",
  "Assessing language level…",
];
let _msgTimer = null;
let _msgIdx = 0;

function startLoadingMessages() {
  _msgIdx = 0;
  if (_msgTimer) clearInterval(_msgTimer);
  document.getElementById("loading-stage").textContent = LOADING_MESSAGES[0];
  _msgTimer = setInterval(() => {
    _msgIdx++;
    if (_msgIdx < LOADING_MESSAGES.length) {
      document.getElementById("loading-stage").textContent = LOADING_MESSAGES[_msgIdx];
    } else {
      clearInterval(_msgTimer);
      _msgTimer = null;
    }
  }, 2500);
}

function stopLoadingMessages() {
  if (_msgTimer) { clearInterval(_msgTimer); _msgTimer = null; }
}

// ── Fake progress bar ──────────────────────────────────────────────────────
let _fakeTimer = null;
let _fakeVal = 0;

function startFakeProgress() {
  _fakeVal = 0;
  if (_fakeTimer) clearInterval(_fakeTimer);
  _fakeTimer = setInterval(() => {
    _fakeVal += (92 - _fakeVal) * 0.012; // decelerates asymptotically toward 92%
    const bar = document.getElementById("loading-bar");
    if (bar) bar.style.width = `${_fakeVal}%`;
  }, 150);
}

function finishProgress() {
  if (_fakeTimer) { clearInterval(_fakeTimer); _fakeTimer = null; }
  const bar = document.getElementById("loading-bar");
  if (bar) bar.style.width = "100%";
}

let wordEvents = [];       // [{time, word, search_term}, ...]
let imageCache = {};       // index -> imgData  (keyed by array index, not time)
let lastTriggeredIdx = -1;
let lastKnownTime = -1;
let slotIndex = 0;
let pollingTimer = null;
let isFetching = false;

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

async function tick() {
  if (!player || typeof player.getCurrentTime !== "function") return;
  if (isFetching) return;
  const t = player.getCurrentTime();

  // Detect seek: any jump larger than 3 s can't be normal playback
  if (lastKnownTime >= 0 && Math.abs(t - lastKnownTime) > 3) {
    lastTriggeredIdx = -1;
    for (let i = 0; i < wordEvents.length; i++) {
      if (wordEvents[i].time > t) break;
      lastTriggeredIdx = i;
    }
  }
  lastKnownTime = t;

  await checkTriggers(t);
}

async function checkTriggers(currentTime) {
  for (let i = lastTriggeredIdx + 1; i < wordEvents.length; i++) {
    const ev = wordEvents[i];
    if (ev.time > currentTime) break;

    lastTriggeredIdx = i;

    let imgData = imageCache[i];
    if (!imgData) {
      if (ev.emoji) {
        imgData = { type: "emoji", char: ev.emoji };
        imageCache[i] = imgData;
      } else if (ev.number) {
        imgData = { type: "number", text: ev.number };
        imageCache[i] = imgData;
      } else {
        // Not pre-fetched yet — fetch on demand
        isFetching = true;
        try {
          const res = await fetch(
            `/api/image?word=${encodeURIComponent(ev.search_term)}`
          );
          const data = await res.json();
          if (data.type !== "none") {
            imgData = data;
            imageCache[i] = data;
          }
        } catch (_) {}
        isFetching = false;
      }
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
    if (img.complete) display(); // already in browser cache — show instantly
  }
}

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Pre-fetch images ───────────────────────────────────────────────────────

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

async function prefetchImages(events) {
  const targets = events.slice(0, 60);
  if (targets.length === 0) return;
  let done = 0;

  const fetchOne = async (ev, idx) => {
    if (ev.emoji) {
      const imgData = { type: "emoji", char: ev.emoji };
      imageCache[idx] = imgData;
      addPreviewCard(imgData, ev.word);
    } else if (ev.number) {
      const imgData = { type: "number", text: ev.number };
      imageCache[idx] = imgData;
      addPreviewCard(imgData, ev.word);
    } else {
      try {
        const res = await fetch(`/api/image?word=${encodeURIComponent(ev.search_term)}`);
        const data = await res.json();
        if (data.type !== "none") {
          imageCache[idx] = data;
          if (data.type === "image") { new Image().src = data.url; }
          addPreviewCard(data, ev.word);
        }
      } catch (_) {}
    }
    done++;
    const stage = document.getElementById("loading-stage");
    if (stage && done < targets.length) stage.textContent = `Finding images and emojis… [${done} / ${targets.length}]`;
  };

  // Await first batch so preview cards appear before video is shown
  await Promise.all(targets.slice(0, 20).map((ev, j) => fetchOne(ev, j)));

  // Remaining batches continue in background once video is visible
  for (let i = 20; i < targets.length; i += 20) {
    Promise.all(targets.slice(i, i + 20).map((ev, j) => fetchOne(ev, i + j)));
  }
}

// ── Level badge ────────────────────────────────────────────────────────────

function setLevelBadge(level) {
  const badge = document.getElementById("level-badge");
  badge.textContent = `Level: ${level}`;
  badge.className = `level-badge level-${level.toLowerCase().replace(/ /g, "-")}`;
}

// ── UI helpers ─────────────────────────────────────────────────────────────

function resetSlots() {
  for (let i = 0; i < 3; i++) {
    const slot = document.getElementById(`slot-${i}`);
    slot.innerHTML = '<div class="slot-placeholder"></div>';
    slot.className = "image-slot";
  }
}

function showLoading() {
  startLoadingMessages();
  const bar = document.getElementById("loading-bar");
  bar.style.width = "0%";
  document.getElementById("loading-preview").innerHTML = "";
  startFakeProgress();
  const sv = document.getElementById("skeleton-video");
  sv.style.backgroundImage = "";
  sv.classList.remove("has-thumbnail");
  const skBadge = document.getElementById("skeleton-level-badge");
  skBadge.textContent = "Checking level…";
  skBadge.className = "level-badge level-checking";
  document.getElementById("loading").classList.remove("hidden");
  document.getElementById("player-section").classList.add("hidden");
  document.getElementById("error-msg").classList.add("hidden");
}

function hideLoading() {
  document.getElementById("loading").classList.add("hidden");
}

function showError(msg) {
  stopLoadingMessages();
  hideLoading();
  const el = document.getElementById("error-msg");
  el.textContent = msg;
  el.classList.remove("hidden");
}

// ── Main entry point ───────────────────────────────────────────────────────

async function processVideo() {
  const url = document.getElementById("url-input").value.trim();
  if (!url) return;

  showLoading();
  if (pollingTimer) clearInterval(pollingTimer);

  // Show thumbnail in skeleton immediately — no server call needed
  const vidMatch = url.match(/(?:v=|youtu\.be\/|embed\/)([a-zA-Z0-9_-]{11})/);
  if (vidMatch) {
    const sv = document.getElementById("skeleton-video");
    sv.style.backgroundImage = `url(https://img.youtube.com/vi/${vidMatch[1]}/mqdefault.jpg)`;
    sv.classList.add("has-thumbnail");
  }

  let data;
  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const msg = JSON.parse(line.slice(6));
          if (msg.type === "progress") {
            stopLoadingMessages();
            document.getElementById("loading-stage").textContent = msg.message;
          } else if (msg.type === "level") {
            const skBadge = document.getElementById("skeleton-level-badge");
            skBadge.textContent = `Level: ${msg.level}`;
            skBadge.className = `level-badge level-${msg.level.toLowerCase().replace(/ /g, "-")}`;
          } else if (msg.type === "result") {
            data = msg;
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    showError("Could not reach the server. Is it running?");
    return;
  }

  if (!data) {
    showError("Could not reach the server. Is it running?");
    return;
  }

  if (!data.has_subtitles) {
    showError(
      "Sorry — no Spanish subtitles were found for this video. Try another one!"
    );
    return;
  }

  // Reset state
  wordEvents = data.word_events || [];
  imageCache = {};
  lastTriggeredIdx = -1;
  lastKnownTime = -1;
  slotIndex = 0;

  stopLoadingMessages();

  // Guarantee badge is set even if the level event was buffered with the result
  const skBadge = document.getElementById("skeleton-level-badge");
  if (skBadge.classList.contains("level-checking")) {
    skBadge.textContent = `Level: ${data.level}`;
    skBadge.className = `level-badge level-${data.level.toLowerCase().replace(/ /g, "-")}`;
  }

  const totalImages = Math.min(wordEvents.length, 60);
  document.getElementById("loading-stage").textContent = `Finding images and emojis… [0 / ${totalImages}]`;

  // Await first batch — user sees preview cards pop in before video appears
  await prefetchImages(wordEvents);

  finishProgress();
  await new Promise(r => setTimeout(r, 300)); // brief pause so 100% is visible
  hideLoading();
  resetSlots();

  document.getElementById("player-section").classList.remove("hidden");
  setLevelBadge(data.level);

  if (ytApiReady) {
    createPlayer(data.video_id);
  } else {
    pendingVideoId = data.video_id;
  }
}

// Enter key submits
document.getElementById("url-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") processVideo();
});

// Show version in footer
fetch("/api/version").then(r => r.json()).then(d => {
  const el = document.getElementById("app-version");
  if (el) el.textContent = `v${d.version}`;
}).catch(() => {});

// Auto-load from ?url= query parameter
const _paramUrl = new URLSearchParams(window.location.search).get("url");
if (_paramUrl) {
  document.getElementById("url-input").value = _paramUrl;
  processVideo();
}
