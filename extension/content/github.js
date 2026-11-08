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
  let pullRequestCard = null;
  let prContainer = null;
  let scanCompleteNotice = null;
  let scanOverlay = null;
  let prToggle = null;
  let prExpanded = false;
  let lastRepoKey = "";
  let mounted = false;
  let closeTimer = null;
  let mountObserver = null;
  let noticeTimer = null;

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
      ".ref-selector-button-text",
      'button[data-hotkey="w"] .Text-sc-17v1xeu-0',
      'button[data-hotkey="w"] span',
      '[data-testid="anchor-button"] span'
    ];

    for (const selector of selectors) {
      const text = readText(selector);
      if (text && text.length < 48) return text;
    }
    return "main";
  }

  function ensureUI() {
    if (mounted && host && host.isConnected) return;

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
    pullRequestCard = document.createElement("section");
    pullRequestCard.className = "devstart-pr-card";
    pullRequestCard.dataset.devstartPrWidget = "true";
    pullRequestCard.setAttribute("aria-labelledby", "devstart-pr-title");
    prContainer = document.createElement("span");
    prContainer.className = "devstart-pr-container";
    prContainer.hidden = true;

    scanCompleteNotice = document.createElement("aside");
    scanCompleteNotice.className = "devstart-scan-notice";
    scanCompleteNotice.hidden = true;
    scanCompleteNotice.setAttribute("role", "status");
    scanCompleteNotice.addEventListener("mouseenter", clearNoticeTimer);
    scanCompleteNotice.addEventListener("mouseleave", () => {
      if (scanCompleteNotice && !scanCompleteNotice.hidden) scheduleNoticeHide();
    });

    scanOverlay = document.createElement("div");
    scanOverlay.className = "devstart-scan-overlay";
    scanOverlay.hidden = true;
    scanOverlay.setAttribute("aria-hidden", "true");
    scanOverlay.innerHTML = '<span class="devstart-scan-line"></span>';

    prToggle = document.createElement("button");
    prToggle.type = "button";
    prToggle.className = "devstart-pr-toggle";
    prToggle.textContent = "Vulnerability issues";
    prToggle.setAttribute("aria-expanded", "false");
    prToggle.setAttribute("aria-label", "Show automated pull request");
    prToggle.addEventListener("click", () => {
      prExpanded = !prExpanded;
      prToggle.setAttribute("aria-expanded", String(prExpanded));
      const total = prToggle.dataset.total || "0";
      prToggle.textContent = prExpanded
        ? `Hide vulnerability issues ${total} ▲`
        : `Vulnerability issues ${total} ▼`;
      prToggle.setAttribute(
        "aria-label",
        prExpanded ? "Hide vulnerability issues" : "Show vulnerability issues"
      );
      if (pullRequestCard) pullRequestCard.hidden = !prExpanded;
    });

    prContainer.append(prToggle, pullRequestCard);
    host.append(scanOverlay, scanCompleteNotice, prContainer, launcher, shell);
    (document.body || document.documentElement).appendChild(host);
    mountPrToggle();
    mounted = true;
    mountObserver = new MutationObserver(() => {
      if (!host || !host.isConnected) {
        mounted = false;
        ensureUI();
      } else {
        mountPrToggle();
      }
    });
    mountObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  function mountPullRequestCard() {
    if (!pullRequestCard) return;
    pullRequestCard.hidden = !prExpanded;
  }

  function mountPrToggle() {
    if (!prToggle || !host) return;
    if (prContainer.parentElement !== host) {
      host.appendChild(prContainer);
    }
  }

  function setAnalyzing(active) {
    if (!scanOverlay) return;
    scanOverlay.hidden = !active;
  }

  function retryHeaderMount(repo) {
    [100, 500, 1200, 2500].forEach((delay) => {
      setTimeout(() => {
        const currentRepo = readRepo();
        if (currentRepo && currentRepo.fullName === repo) {
          mountPrToggle();
          restoreCompletedScan(repo);
        }
      }, delay);
    });
  }

  function showPullRequest(data, options = {}) {
    if (!pullRequestCard) return;
    const { notify = true } = options;
    mountPrToggle();
    prContainer.hidden = false;
    prExpanded = false;
    pullRequestCard.innerHTML = "";

    const header = document.createElement("div");
    header.className = "devstart-pr-head";
    const heading = document.createElement("div");
    const kicker = document.createElement("span");
    kicker.className = "devstart-pr-kicker";
    kicker.textContent = "DevStart AI · Repository scan";
    const title = document.createElement("h2");
    title.id = "devstart-pr-title";
    title.textContent = "Vulnerability issues";
    heading.append(kicker, title);
    const badge = document.createElement("span");
    badge.className = "devstart-pr-badge";
    badge.textContent = "Scanned";
    header.append(heading, badge);

    const summary = document.createElement("p");
    summary.className = "devstart-pr-summary";
    const counts = data.vulnerabilities || {};
    const total = ["critical", "high", "medium", "low"].reduce(
      (sum, key) => sum + (Number.isFinite(counts[key]) ? counts[key] : 0),
      0
    );
    prToggle.textContent = `Vulnerability issues ${total}`;
    prToggle.dataset.total = String(total);
    prToggle.setAttribute("aria-expanded", "false");
    prToggle.setAttribute("aria-label", "Show vulnerability issues");
    launcher.innerHTML =
      `${BRAND_MARK_SVG}` +
      `<span>DevStart AI</span><span class="devstart-analysis-label">Analysis</span>`;
    launcher.removeAttribute("aria-expanded");
    launcher.setAttribute("aria-label", "Open DevStart AI repository assistant");
    summary.textContent = `Scanned: ${total} vulnerability issue${total === 1 ? "" : "s"}`;
    const severityGrid = document.createElement("div");
    severityGrid.className = "devstart-vulnerability-grid";
    [
      ["Critical", counts.critical],
      ["High", counts.high],
      ["Medium", counts.medium],
      ["Low", counts.low]
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      item.className = `devstart-vulnerability-item severity-${label.toLowerCase()}`;
      const valueText = document.createElement("strong");
      valueText.textContent = String(Number.isFinite(value) ? value : 0);
      const labelText = document.createElement("span");
      labelText.textContent = label;
      item.append(valueText, labelText);
      severityGrid.appendChild(item);
    });

    const detailsToggle = document.createElement("button");
    detailsToggle.type = "button";
    detailsToggle.className = "devstart-pr-expand";
    detailsToggle.textContent = "Expand scan details +";
    detailsToggle.setAttribute("aria-expanded", "false");

    const details = document.createElement("div");
    details.className = "devstart-pr-details";
    details.hidden = true;
    detailsToggle.addEventListener("click", () => {
      details.hidden = !details.hidden;
      detailsToggle.textContent = details.hidden ? "Expand scan details +" : "Hide scan details −";
      detailsToggle.setAttribute("aria-expanded", String(!details.hidden));
    });
    [
      ["Why this change", data.why],
      ["Proposed scope", data.scope]
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      const itemLabel = document.createElement("span");
      itemLabel.className = "devstart-pr-label";
      itemLabel.textContent = label;
      const itemText = document.createElement("p");
      itemText.textContent = value || "";
      item.append(itemLabel, itemText);
      details.appendChild(item);
    });

    pullRequestCard.append(header, summary, severityGrid, detailsToggle, details);
    pullRequestCard.hidden = !prExpanded;
    mountPullRequestCard();
    mountPrToggle();

    if (notify) showScanCompleteNotice(data.repo);
  }

  function clearNoticeTimer() {
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
  }

  function hideScanCompleteNotice() {
    if (!scanCompleteNotice) return;
    clearNoticeTimer();
    scanCompleteNotice.hidden = true;
    scanCompleteNotice.classList.remove("is-leaving");
  }

  function scheduleNoticeHide() {
    clearNoticeTimer();
    noticeTimer = setTimeout(() => {
      if (!scanCompleteNotice || scanCompleteNotice.hidden) return;
      scanCompleteNotice.classList.add("is-leaving");
      noticeTimer = setTimeout(() => {
        if (scanCompleteNotice) {
          scanCompleteNotice.hidden = true;
          scanCompleteNotice.classList.remove("is-leaving");
        }
        noticeTimer = null;
      }, 180);
    }, 7000);
  }

  function showScanCompleteNotice(repo) {
    if (!scanCompleteNotice || !repo) return;
    const [owner, name] = repo.split("/");
    if (!owner || !name) return;

    scanCompleteNotice.innerHTML = "";
    const copy = document.createElement("span");
    copy.textContent = "Automated PR is complete. Please check.";
    const link = document.createElement("a");
    link.href = `/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pulls`;
    link.textContent = "Go to Pull Request";
    link.setAttribute("aria-label", "Go to Pull Requests");
    link.addEventListener("click", hideScanCompleteNotice);
    scanCompleteNotice.append(copy, link);
    scanCompleteNotice.hidden = false;
    scanCompleteNotice.classList.remove("is-leaving");

    // Auto-dismiss, but give the user time to read and click through.
    scheduleNoticeHide();
  }

  function restoreStoredPrDraft(repo) {
    if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local || !repo) return;
    const key = `prDraft:${repo}`;
    chrome.storage.local.get([key], (result) => {
      if (chrome.runtime && chrome.runtime.lastError) return;
      const draft = result && result[key];
      const currentRepo = readRepo();
      if (draft && currentRepo && currentRepo.fullName === repo)
        showPullRequest(draft, { notify: false });
    });
  }

  function restoreCompletedScan(repo) {
    if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local || !repo) return;
    const keys = [`prDraft:${repo}`, `scanComplete:${repo}`];
    chrome.storage.local.get(keys, (result) => {
      if (chrome.runtime && chrome.runtime.lastError) return;
      const currentRepo = readRepo();
      if (!currentRepo || currentRepo.fullName !== repo) return;
      const draft = result && result[`prDraft:${repo}`];
      if (result && result[`scanComplete:${repo}`] && draft)
        showPullRequest(draft, { notify: false });
    });
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
    clearNoticeTimer();
    if (prContainer && prContainer.isConnected) prContainer.remove();
    if (host && host.isConnected) host.remove();
    host = null;
    launcher = null;
    shell = null;
    frame = null;
    pullRequestCard = null;
    prContainer = null;
    scanCompleteNotice = null;
    scanOverlay = null;
    prToggle = null;
    prExpanded = false;
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
      prExpanded = false;
      hideScanCompleteNotice();
      if (prToggle) {
        prToggle.textContent = "Vulnerability issues ▼";
        prToggle.setAttribute("aria-expanded", "false");
      }
      if (pullRequestCard) pullRequestCard.innerHTML = "";
      if (prContainer) prContainer.hidden = true;
      setAnalyzing(false);
      sendContext();
    }
    restoreCompletedScan(repo.fullName);
    retryHeaderMount(repo.fullName);

    maybeAutoOpen();
  }

  // Messages from the embedded panel
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.source !== "devstart-panel") return;
    if (!frame || event.source !== frame.contentWindow) return;

    if (data.type === "CLOSE" || data.type === "MINIMIZE") {
      closePanel();
    } else if (data.type === "PR_DRAFT_READY") {
      showPullRequest(data);
    } else if (data.type === "ANALYSIS_STARTED") {
      setAnalyzing(true);
    } else if (data.type === "ANALYSIS_FINISHED") {
      setAnalyzing(false);
    } else if (data.type === "PANEL_READY") {
      sendContext();
    }
  });

  if (chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName !== "local") return;
      const repo = readRepo();
      if (!repo) return;
      const change = changes[`prDraft:${repo.fullName}`];
      if (change && change.newValue) showPullRequest(change.newValue);
      if (changes[`scanComplete:${repo.fullName}`] && changes[`scanComplete:${repo.fullName}`].newValue) {
        restoreCompletedScan(repo.fullName);
      }
    });
  }

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
