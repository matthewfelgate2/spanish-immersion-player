function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const LEVEL_ORDER = ["Super Beginner", "Beginner", "Intermediate", "Advanced"];

let allVideos = [];
let activeLevel = "all";

function renderGrid() {
  const grid = document.getElementById("video-grid");
  const filtered = activeLevel === "all"
    ? allVideos
    : allVideos.filter(v => v.level === activeLevel);

  if (!filtered.length) {
    grid.innerHTML = '<p class="grid-empty">No videos at this level yet.</p>';
    return;
  }

  grid.innerHTML = "";
  filtered.forEach(v => {
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
}

fetch("/api/videos")
  .then(r => r.json())
  .then(videos => {
    if (!videos.length) {
      document.getElementById("video-grid").innerHTML = '<p class="grid-empty">No videos available yet.</p>';
      return;
    }

    allVideos = videos.sort((a, b) => LEVEL_ORDER.indexOf(a.level) - LEVEL_ORDER.indexOf(b.level));

    // Only show filter buttons for levels that actually exist
    const existingLevels = new Set(allVideos.map(v => v.level));
    const filtersEl = document.getElementById("level-filters");
    filtersEl.querySelectorAll("[data-level]").forEach(btn => {
      if (btn.dataset.level !== "all" && !existingLevels.has(btn.dataset.level)) {
        btn.style.display = "none";
      }
    });
    filtersEl.style.display = "flex";

    // Filter button clicks
    filtersEl.addEventListener("click", e => {
      const btn = e.target.closest("[data-level]");
      if (!btn) return;
      filtersEl.querySelectorAll("[data-level]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeLevel = btn.dataset.level;
      renderGrid();
    });

    renderGrid();
  })
  .catch(() => {
    document.getElementById("video-grid").innerHTML = '<p class="grid-empty">Could not load videos.</p>';
  });

fetch("/api/version").then(r => r.json()).then(d => {
  const el = document.getElementById("app-version");
  if (el) el.textContent = `v${d.version}`;
}).catch(() => {});
