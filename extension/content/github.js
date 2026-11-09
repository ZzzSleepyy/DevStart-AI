// DevStart AI — GitHub repository overlay
//
// Injects a launcher and a floating assistant window on repository pages.
// The window is an extension iframe so the panel UI stays in one place, and
// this script feeds it repository context scraped from the GitHub page.

(() => {
  const HOST_ID = "devstart-host";
  const AUTO_OPEN_KEY = "devstart-auto-opened";
  const BRAND_MARK_SVG =
    '<svg class="devstart-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true">' +
    '<defs>' +
    '<linearGradient id="dsBrandBowl" x1="15" y1="5" x2="34" y2="35" gradientUnits="userSpaceOnUse">' +
    '<stop stop-color="#8b6cf6"/>' +
    '<stop offset="0.45" stop-color="#3b82f6"/>' +
    '<stop offset="1" stop-color="#22d3ee"/>' +
    "</linearGradient>" +
    '<linearGradient id="dsBrandStem" x1="10" y1="3" x2="15" y2="38" gradientUnits="userSpaceOnUse">' +
    '<stop stop-color="#e2ebf9"/>' +
    '<stop offset="0.5" stop-color="#9cc2ff"/>' +
    '<stop offset="1" stop-color="#4f8df7"/>' +
    "</linearGradient>" +
    "</defs>" +
    '<path fill-rule="evenodd" clip-rule="evenodd" fill="url(#dsBrandBowl)" ' +
    'd="M17.2 2.5C12.4 9.2 9.2 16 9 22.5c-.2 6.5 2.2 11.9 7.2 15.1 5.8.2 11.4-2.4 14.6-7.1 2.8-4 3.8-7.5 3.8-10.7 0-6.2-3.6-11.8-9.6-14.6-2.6-1.2-5.2-1.8-7.8-2.7ZM14 9.5c3.8.7 7.4 1.3 10.2 2.2 4 1.4 5.6 4.5 5.6 8.3s-1.6 6.9-5.6 8.3c-2.8.9-6.4 1.5-10.2 2.2-1.4-1.5-2-3.7-2-5.8v-9c0-2.3.6-4.5 2-6.2Z"/>' +
    '<path fill="url(#dsBrandStem)" ' +
    'd="M17.2 2.5C12.4 9.2 9.2 16 9 22.5c-.2 6.5 2.2 11.9 7.2 15.1-2.5-3.8-3.3-7.6-2.8-12 .5-5.4 2-15.6 3.8-23.1Z"/>' +
    "</svg>";
  const RESERVED_ROOTS = new Set([
    "settings",
    "notifications",
    "marketplace",
    "explore",
    "topics",
    "trending",
    "collections",
    "events",
    "sponsors",
    "login",
    "signup",
    "join",
    "new",
    "organizations",
    "orgs",
    "users",
    "search",
    "features",
    "enterprise",
    "pricing",
    "about",
    "security",
    "site",
    "apps",
    "codespaces",
    "issues",
    "pulls",
    "dashboard",
    "account",
    "sessions"
  ]);

  let host = null;
  let launcher = null;
  let shell = null;
  let frame = null;
  let scanOverlay = null;
  let lastRepoKey = "";
  let mounted = false;
  let closeTimer = null;
  let mountObserver = null;

  function readRepo() {
    const meta = document.querySelector('meta[name="octolytics-dimension-repository_nwo"]');
    const parts = location.pathname.split("/").filter(Boolean);

    let fullName = meta && meta.content ? meta.content : "";
    if (!fullName && parts.length >= 2 && !RESERVED_ROOTS.has(parts[0])) {
      fullName = `${parts[0]}/${parts[1]}`;
    }

    function readMetric(selectors) {
      for (const selector of selectors) {
        const text = readText(selector);
        if (!text) continue;
        const match = text.match(/(\d[\d,.]*)(?:\s|$)/);
        if (match) return match[1].replace(/,/g, "");
        if (/^(star|fork|issue)s?$/i.test(text)) return "0";
      }
      return "";
    }

    if (!fullName || !fullName.includes("/")) return null;

    const [owner, name] = fullName.split("/");
    const publicMeta = document.querySelector('meta[name="octolytics-dimension-repository_public"]');
    const visibility = publicMeta && publicMeta.content === "false" ? "Private" : "Public";

    return {
      owner,
      name,
      fullName: `${owner}/${name}`,
      visibility,
      branch: readBranch(),
      stars: readMetric([
        "#repo-stars-counter-star",
        'a[href$="/stargazers"]',
        'a[href*="/stargazers"]',
        '[aria-label*="star" i]'
      ]) || "—",
      forks: readMetric([
        "#repo-network-counter",
        'a[href*="/network/members"]',
        '[aria-label*="fork" i]'
      ]) || "—",
      issues: readMetric([
        "#issues-repo-tab-count",
        'a[href$="/issues"]',
        'a[href*="/issues"]',
        '[aria-label*="issue" i]'
      ]) || "—"
    };
  }

  function readText(selector) {
    const el = document.querySelector(selector);
    if (!el) return "";
    return (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 24);
  }

  function readBranch() {
    const selectors = [
      "#branch-picker-repos-header-ref-selector span",
      ".ref-selector-button-text",
      'button[data-hotkey="w"] .Text-sc-17v1xeu-0',
      'button[data-hotkey="w"] span'
    ];

    for (const selector of selectors) {
      const text = readText(selector);
      if (text && text.length < 48) return text;
    }
    return "main";
  }

  function removeLegacyWidgets() {
    document
      .querySelectorAll(
        ".devstart-pr-container, .devstart-pr-card, .devstart-pr-toggle, .devstart-scan-notice, [data-devstart-pr-widget]"
      )
      .forEach((el) => el.remove());

    document.querySelectorAll(`#${HOST_ID}`).forEach((el) => {
      if (host && el !== host) el.remove();
    });
  }

  function ensureUI() {
    removeLegacyWidgets();
    if (mounted && host && host.isConnected) return;

    document.querySelectorAll(`#${HOST_ID}`).forEach((el) => el.remove());

    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText =
      "position:fixed;inset:0;z-index:2147483000;pointer-events:none;";

    launcher = document.createElement("button");
    launcher.type = "button";
    launcher.className = "devstart-launcher";
    launcher.setAttribute("aria-label", "Open DevStart AI repository assistant");
    launcher.innerHTML =
      BRAND_MARK_SVG +
      '<span>DevStart AI</span><span class="devstart-analysis-label">Analysis</span>';
    launcher.addEventListener("click", () => {
      openPanel();
    });

    shell = document.createElement("div");
    shell.className = "devstart-shell";
    shell.hidden = true;

    frame = document.createElement("iframe");
    frame.title = "DevStart AI repository assistant";
    frame.setAttribute("allow", "microphone; clipboard-write");
    frame.addEventListener("load", () => {
      sendContext();
      sendShowChat();
    });

    shell.appendChild(frame);

    scanOverlay = document.createElement("div");
    scanOverlay.className = "devstart-scan-overlay";
    scanOverlay.hidden = true;
    scanOverlay.setAttribute("aria-hidden", "true");
    scanOverlay.innerHTML = '<span class="devstart-scan-line"></span>';

    host.append(scanOverlay, launcher, shell);
    (document.body || document.documentElement).appendChild(host);
    mounted = true;
    mountObserver = new MutationObserver(() => {
      removeLegacyWidgets();
      if (!host || !host.isConnected) {
        mounted = false;
        ensureUI();
      }
    });
    mountObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  function setAnalyzing(active) {
    if (!scanOverlay) return;
    scanOverlay.hidden = !active;
  }

  function openPanel() {
    if (!readRepo()) return;
    ensureUI();
    if (closeTimer) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
    if (!frame.src) {
      frame.src = chrome.runtime.getURL("sidepanel/sidepanel.html?embed=1");
    }

    shell.hidden = false;
    launcher.hidden = true;
    shell.classList.remove("is-closing");

    requestAnimationFrame(() => {
      if (shell) shell.classList.add("is-open");
    });

    try {
      sessionStorage.setItem(AUTO_OPEN_KEY, "1");
    } catch {
      /* storage may be unavailable */
    }

    setTimeout(sendContext, 120);
    setTimeout(sendShowChat, 180);
  }

  function closePanel() {
    if (!shell) return;
    if (closeTimer) clearTimeout(closeTimer);
    shell.classList.remove("is-open");
    shell.classList.add("is-closing");
    if (launcher) launcher.hidden = false;
    closeTimer = setTimeout(() => {
      if (shell && !shell.classList.contains("is-open")) shell.hidden = true;
      if (shell) shell.classList.remove("is-closing");
      closeTimer = null;
    }, 220);
  }

  function togglePanel() {
    if (!readRepo()) return;
    ensureUI();
    if (!shell || shell.hidden || !shell.classList.contains("is-open")) {
      openPanel();
    } else {
      closePanel();
    }
  }

  function sendContext() {
    if (!frame || !frame.contentWindow) return;
    const repo = readRepo();
    if (!repo) return;
    frame.contentWindow.postMessage(
      { source: "devstart-content", type: "REPO_CONTEXT", repo },
      "*"
    );
  }

  function sendShowChat() {
    if (!frame || !frame.contentWindow) return;
    frame.contentWindow.postMessage(
      { source: "devstart-content", type: "SHOW_CHAT" },
      "*"
    );
  }

  function maybeAutoOpen() {
    try {
      if (sessionStorage.getItem(AUTO_OPEN_KEY) === "1") return;
    } catch {
      return;
    }
    setTimeout(openPanel, 750);
  }

  function unmount() {
    if (host && host.isConnected) host.remove();
    host = null;
    launcher = null;
    shell = null;
    frame = null;
    scanOverlay = null;
    mounted = false;
    lastRepoKey = "";
    if (closeTimer) clearTimeout(closeTimer);
    closeTimer = null;
    if (mountObserver) mountObserver.disconnect();
    mountObserver = null;
  }

  function boot() {
    const repo = readRepo();

    if (!repo) {
      unmount();
      return;
    }

    ensureUI();

    if (repo.fullName !== lastRepoKey) {
      lastRepoKey = repo.fullName;
      setAnalyzing(false);
      sendContext();
    }

    maybeAutoOpen();
  }

  // Messages from the embedded panel
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.source !== "devstart-panel") return;
    if (!frame || event.source !== frame.contentWindow) return;

    if (data.type === "CLOSE" || data.type === "MINIMIZE") {
      closePanel();
    } else if (data.type === "ANALYSIS_STARTED") {
      setAnalyzing(true);
    } else if (data.type === "ANALYSIS_FINISHED") {
      setAnalyzing(false);
    } else if (data.type === "PANEL_READY") {
      sendContext();
    }
  });

  // Toolbar action toggles the on-page assistant when it is available
  if (chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message) => {
      if (message && message.type === "DEVSTART_TOGGLE") {
        togglePanel();
      }
    });
  }

  document.addEventListener("turbo:load", boot);
  document.addEventListener("pjax:end", boot);
  window.addEventListener("popstate", boot);
  window.addEventListener("hashchange", boot);
  window.addEventListener("pageshow", boot);
  document.addEventListener("DOMContentLoaded", boot, { once: true });

  boot();
  setTimeout(boot, 250);
  setTimeout(boot, 1000);
  setTimeout(boot, 2500);
})();
