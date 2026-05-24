function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const LEVEL_ORDER = ["Super Beginner", "Beginner", "Intermediate", "Advanced"];

fetch("/api/videos")
  .then(r => r.json())
  .then(videos => {
    const grid = document.getElementById("video-grid");
    grid.innerHTML = "";

    if (!videos.length) {
      grid.innerHTML = '<p class="grid-empty">No videos available yet.</p>';
      return;
    }

    // Sort by level (easiest first)
    videos.sort((a, b) => LEVEL_ORDER.indexOf(a.level) - LEVEL_ORDER.indexOf(b.level));

    videos.forEach(v => {
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
  })
  .catch(() => {
    document.getElementById("video-grid").innerHTML =
      '<p class="grid-empty">Could not load videos.</p>';
  });

fetch("/api/version").then(r => r.json()).then(d => {
  const el = document.getElementById("app-version");
  if (el) el.textContent = `v${d.version}`;
}).catch(() => {});
