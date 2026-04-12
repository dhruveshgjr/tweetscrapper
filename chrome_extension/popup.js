const API_BASE = "http://localhost:3000";
const PAGE_SIZE = 20;

let currentOffset = 0;
let totalTweets = 0;
let isSearch = false;
let currentQuery = "";

const contentEl = document.getElementById("content");
const statsEl = document.getElementById("stats");
const searchInput = document.getElementById("searchInput");
const searchBtn = document.getElementById("searchBtn");
const downloadBtn = document.getElementById("downloadBtn");
const paginationEl = document.getElementById("pagination");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");
const pageInfoEl = document.getElementById("pageInfo");

async function apiFetch(endpoint) {
  try {
    const resp = await fetch(`${API_BASE}${endpoint}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (err) {
    contentEl.innerHTML = `<div class="error">Cannot connect to API server at ${API_BASE}.<br>Run: <code>python 4_exporter.py --serve</code></div>`;
    throw err;
  }
}

function formatNumber(n) {
  if (n === null || n === undefined) return "0";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
}

function renderTweet(tweet) {
  const date = tweet.created_at || "";
  const text = escapeHtml(tweet.text || "");
  const isRt = tweet.is_retweet;
  const rtBadge = isRt ? '<span class="badge">RT</span>' : "";
  return `
    <div class="tweet-card">
      <div style="font-size:11px;color:var(--muted);">${date}${rtBadge}</div>
      <div class="tweet-text">${text}</div>
      <div class="tweet-meta">
        <span>❤ ${formatNumber(tweet.likes)}</span>
        <span>🔁 ${formatNumber(tweet.retweets)}</span>
        <span>💬 ${formatNumber(tweet.replies)}</span>
        <span>👁 ${formatNumber(tweet.views)}</span>
      </div>
    </div>`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function updatePagination() {
  if (totalTweets === 0) {
    paginationEl.style.display = "none";
    return;
  }
  paginationEl.style.display = "flex";
  const page = Math.floor(currentOffset / PAGE_SIZE) + 1;
  const totalPages = Math.ceil(totalTweets / PAGE_SIZE);
  pageInfoEl.textContent = `${page} / ${totalPages}`;
  prevBtn.disabled = currentOffset === 0;
  nextBtn.disabled = currentOffset + PAGE_SIZE >= totalTweets;
}

async function loadTweets(offset = 0) {
  currentOffset = offset;
  contentEl.innerHTML = '<div class="loading">Loading tweets...</div>';

  let data;
  if (isSearch && currentQuery) {
    data = await apiFetch(`/tweets/search?q=${encodeURIComponent(currentQuery)}&limit=${PAGE_SIZE}`);
    totalTweets = data.total_matches;
  } else {
    data = await apiFetch(`/tweets?limit=${PAGE_SIZE}&offset=${offset}`);
    totalTweets = data.total;
  }

  if (!data || !data.tweets || data.tweets.length === 0) {
    contentEl.innerHTML = '<div class="loading">No tweets found.</div>';
    paginationEl.style.display = "none";
    return;
  }

  contentEl.innerHTML = data.tweets.map(renderTweet).join("");
  updatePagination();
}

async function loadStats() {
  try {
    const stats = await apiFetch("/stats");
    statsEl.textContent = `${stats.total_tweets} tweets | ${formatNumber(stats.total_likes)} likes`;
  } catch {
    statsEl.textContent = "";
  }
}

searchBtn.addEventListener("click", () => {
  const q = searchInput.value.trim();
  if (q) {
    isSearch = true;
    currentQuery = q;
    loadTweets(0);
  } else {
    isSearch = false;
    currentQuery = "";
    loadTweets(0);
  }
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchBtn.click();
});

prevBtn.addEventListener("click", () => {
  if (currentOffset >= PAGE_SIZE) loadTweets(currentOffset - PAGE_SIZE);
});

nextBtn.addEventListener("click", () => {
  loadTweets(currentOffset + PAGE_SIZE);
});

downloadBtn.addEventListener("click", () => {
  window.open(`${API_BASE}/download`, "_blank");
});

loadStats();
loadTweets(0);