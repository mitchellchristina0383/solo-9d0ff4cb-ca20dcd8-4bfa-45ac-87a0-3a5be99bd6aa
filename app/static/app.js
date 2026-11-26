/* Four-switch ring drill frontend. No external dependencies. */
(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const POSITIONS = {
    s1: { x: 150, y: 105 },
    s2: { x: 570, y: 105 },
    s3: { x: 570, y: 365 },
    s4: { x: 150, y: 365 },
  };

  const state = window.__INITIAL_STATE__ || {
    clock_ms: 0,
    switches: [],
    links: [],
    commands: [],
    events: [],
    topology: { forwarding_link_ids: [], loop_free: true, components: [] },
  };
  let lastStateClockMs = state.clock_ms || 0;
  let lastStatePerfMs = performance.now();
  let selectedSource = state.last_probe?.source || "s1";

  const topologySvg = document.getElementById("topology");
  const linkList = document.getElementById("link-list");
  const backupSelect = document.getElementById("backup-select");
  const commandList = document.getElementById("command-list");
  const eventLog = document.getElementById("event-log");
  const broadcastResult = document.getElementById("broadcast-result");
  const broadcastForm = document.getElementById("broadcast-form");
  const broadcastButton = document.getElementById("broadcast-btn");
  const resetButton = document.getElementById("reset-btn");
  const loopBadge = document.getElementById("loop-free-badge");
  const forwardingSummary = document.getElementById("forwarding-summary");
  const clockEl = document.querySelector(".clock");
  const toast = document.getElementById("toast");

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function setState(nextState) {
    Object.assign(state, nextState);
    lastStateClockMs = state.clock_ms || 0;
    lastStatePerfMs = performance.now();
    renderAll();
  }

  function engineNowMs() {
    return lastStateClockMs + Math.max(0, performance.now() - lastStatePerfMs);
  }

  function formatClock(ms) {
    return `${(Number(ms || 0) / 1000).toFixed(2)} s`;
  }

  function remainingText(command) {
    const remaining = command.execute_at_clock_ms - engineNowMs();
    if (remaining <= 30) return "即将生效";
    return `剩余 ${(remaining / 1000).toFixed(1)} s`;
  }

  function switchName(switchId) {
    const item = state.switches.find((s) => s.id === switchId);
    return item ? item.name : switchId;
  }

  function linkById(linkId) {
    return state.links.find((l) => l.id === linkId);
  }

  function pendingCommandFor(linkId) {
    return state.commands.find((c) => c.status === "pending" && c.link_id === linkId) || null;
  }

  function stateText(link) {
    if (!link.plugged || link.state === "down") return "已拔线 · Down";
    if (link.state === "forwarding") return "实际转发";
    return "阻塞中";
  }

  function showError(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showError.timer);
    showError.timer = setTimeout(() => {
      toast.hidden = true;
    }, 4200);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || `请求失败：HTTP ${response.status}`);
    }
    return data;
  }

  async function refresh() {
    try {
      const next = await api("/api/state");
      setState(next);
    } catch (error) {
      console.error(error);
    }
  }

  function renderBadgeAndClock() {
    const loopFree = state.topology?.loop_free !== false;
    loopBadge.textContent = loopFree ? "✓ 转发图无环" : "⚠ 检测到转发环路";
    loopBadge.className = `badge ${loopFree ? "badge-ok" : "badge-bad"}`;
    clockEl.textContent = `演练时刻 ${(engineNowMs() / 1000).toFixed(1)} s`;
    const forwarding = state.topology?.forwarding_link_ids || [];
    const components = state.topology?.components?.length ?? "-";
    forwardingSummary.textContent = `实际转发：${forwarding.map((id) => id.toUpperCase()).join("、") || "无"}；连通段：${components}`;
  }

  function renderTopology() {
    if (!topologySvg) return;
    const reached = new Set(state.last_probe?.reached || []);
    const linkSvg = state.links
      .map((link) => {
        const p = POSITIONS[link.source];
        const q = POSITIONS[link.target];
        const midX = (p.x + q.x) / 2;
        const midY = (p.y + q.y) / 2;
        const pending = pendingCommandFor(link.id);
        const pendingClass = pending ? ` pending-${pending.action}` : "";
        const countdown = pending ? remainingText(pending) : "";
        const backupMark = link.designated_backup ? " · 备" : "";
        return `
          <line x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}"
                class="link-line ${link.state}${pendingClass}"
                data-testid="topology-line-${link.id}" />
          <g class="link-badge" transform="translate(${midX},${midY})" data-state="${link.state}">
            <rect x="-61" y="-32" width="122" height="64" rx="11"></rect>
            <text x="0" y="-13" class="lid">${escapeHtml(link.name)}${backupMark}</text>
            <text x="0" y="5" class="lstate">${stateText(link)}</text>
            <text x="0" y="23" class="countdown">${escapeHtml(countdown)}</text>
          </g>`;
      })
      .join("");

    const switchSvg = state.switches
      .map((sw) => {
        const p = POSITIONS[sw.id] || { x: 360, y: 235 };
        const classes = [
          "switch-node",
          sw.id === selectedSource ? "selected" : "",
          reached.has(sw.id) ? "reached" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return `
          <g class="${classes}" data-switch-id="${sw.id}" data-testid="topology-switch-${sw.id}"
             transform="translate(${p.x},${p.y})" role="button" tabindex="0"
             aria-label="选择${escapeHtml(sw.name)}为广播源">
            <rect x="-62" y="-36" width="124" height="72" rx="16"></rect>
            <text x="0" y="-7" class="sw-name">交换机 ${sw.id.slice(1)}</text>
            <text x="0" y="17" class="sw-id">${escapeHtml(sw.short || sw.id.toUpperCase())}</text>
          </g>`;
      })
      .join("");

    topologySvg.innerHTML = linkSvg + switchSvg;
    topologySvg.querySelectorAll(".switch-node").forEach((node) => {
      const select = () => selectSource(node.dataset.switchId);
      node.addEventListener("click", select);
      node.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          select();
        }
      });
    });
  }

  function selectSource(sourceId) {
    selectedSource = sourceId;
    const radio = broadcastForm?.querySelector(`input[name="source"][value="${sourceId}"]`);
    if (radio) radio.checked = true;
    renderTopology();
  }

  function linkCardHtml(link) {
    return `
      <article class="link-card" data-link-id="${link.id}" data-state="${link.state}" data-testid="link-card-${link.id}">
        <div class="link-main">
          <div>
            <span class="link-title">${escapeHtml(link.name)}</span>
            <span class="link-ports">${escapeHtml(link.source.toUpperCase())} ↔ ${escapeHtml(link.target.toUpperCase())}</span>
          </div>
          <span class="state-pill ${link.state}" data-testid="link-state-${link.id}">${stateText(link)}</span>
        </div>
        <div class="link-grid">
          <label class="field">开通延时（秒）
            <input type="number" min="0" max="600" step="0.1" class="open-delay"
                   data-delay-field="open_delay_seconds" data-testid="open-delay-${link.id}"
                   value="${(link.open_delay_ms / 1000).toString()}" />
          </label>
          <label class="field">阻塞延时（秒）
            <input type="number" min="0" max="600" step="0.1" class="block-delay"
                   data-delay-field="block_delay_seconds" data-testid="block-delay-${link.id}"
                   value="${(link.block_delay_ms / 1000).toString()}" />
          </label>
          <label class="backup-check" title="设为环网备用链路">
            <input type="radio" name="backup-link" class="backup-radio" value="${link.id}"
                   data-testid="backup-radio-${link.id}" ${link.designated_backup ? "checked" : ""} />
            设为备用
          </label>
          <button type="button" class="btn plug-button ${link.plugged ? "btn-danger" : "btn-success"}"
                  data-action="plug" data-testid="plug-button-${link.id}">
            ${link.plugged ? "拔线" : "插回"}
          </button>
        </div>
        <div class="pending-note" data-role="pending-note" hidden></div>
      </article>`;
  }

  function renderLinks() {
    if (!linkList.dataset.rendered) {
      linkList.innerHTML = state.links.map(linkCardHtml).join("");
      linkList.dataset.rendered = "1";
    }

    state.links.forEach((link) => {
      const card = linkList.querySelector(`[data-link-id="${link.id}"]`);
      if (!card) return;
      if (backupSelect && document.activeElement !== backupSelect && link.designated_backup) {
        backupSelect.value = link.id;
      }
      card.dataset.state = link.state;
      const pill = card.querySelector(".state-pill");
      pill.className = `state-pill ${link.state}`;
      pill.textContent = stateText(link);
      pill.dataset.testid = `link-state-${link.id}`;

      const button = card.querySelector(".plug-button");
      button.textContent = link.plugged ? "拔线" : "插回";
      button.className = `btn plug-button ${link.plugged ? "btn-danger" : "btn-success"}`;

      const backup = card.querySelector(".backup-radio");
      if (document.activeElement !== backup) backup.checked = !!link.designated_backup;

      const openInput = card.querySelector(".open-delay");
      if (document.activeElement !== openInput) openInput.value = String(link.open_delay_ms / 1000);
      const blockInput = card.querySelector(".block-delay");
      if (document.activeElement !== blockInput) blockInput.value = String(link.block_delay_ms / 1000);

      const note = card.querySelector('[data-role="pending-note"]');
      const pending = pendingCommandFor(link.id);
      if (pending) {
        const action = pending.action === "open" ? "开通" : "阻塞";
        note.hidden = false;
        note.dataset.testid = `pending-${link.id}`;
        note.textContent = `${action}命令 #${pending.id} 已下发，原定 ${formatClock(
          pending.execute_at_clock_ms
        )} 生效；${remainingText(pending)}。再次拔插不会取消或改期。`;
      } else {
        note.hidden = true;
        note.textContent = "";
        note.removeAttribute("data-testid");
      }
    });
  }

  const RESULT_TEXT = {
    pending: "待生效",
    forwarding: "已实际开通",
    blocked: "已阻塞",
    already_blocked: "到期时已是阻塞态",
    down: "到期时未插线，保持 Down",
    safety_blocked: "为防环被环路保护阻塞",
  };

  function renderCommands() {
    const rows = [...state.commands].slice(-40).reverse();
    commandList.innerHTML = rows
      .map((command) => {
        const pending = command.status === "pending";
        const resultClass = pending ? "pending" : command.result || "executed";
        const resultText = pending
          ? `待生效 · ${remainingText(command)}`
          : RESULT_TEXT[command.result] || command.result || "已执行";
        return `
          <tr class="${pending ? "pending-command" : ""}" data-testid="command-row-${command.id}">
            <td>${escapeHtml(command.id)}</td>
            <td><b>${escapeHtml(command.link_id.toUpperCase())}</b></td>
            <td>${command.action === "open" ? "开通" : "阻塞"}</td>
            <td>${formatClock(command.issued_at_clock_ms)}</td>
            <td>${formatClock(command.execute_at_clock_ms)}</td>
            <td><span class="result-pill ${escapeHtml(resultClass)}">${escapeHtml(resultText)}</span></td>
          </tr>`;
      })
      .join("");
  }

  function renderEvents() {
    const rows = [...state.events].slice(-100).reverse();
    eventLog.innerHTML = rows
      .map(
        (event) => `
        <div class="event-row" data-testid="event-row-${event.id}">
          <span class="event-time">${formatClock(event.at_clock_ms)}</span>
          <span><span class="event-kind">[${escapeHtml(event.kind)}]</span> <span class="event-message">${escapeHtml(
          event.message
        )}</span></span>
        </div>`
      )
      .join("");
  }

  function renderProbe() {
    const probe = state.last_probe;
    if (!probe) {
      broadcastResult.innerHTML = '<p class="muted">尚未发送探测。</p>';
      return;
    }

    const reachedSet = new Set(probe.reached);
    const chips = state.switches
      .map((sw) => {
        const reached = reachedSet.has(sw.id);
        return `<span class="chip ${reached ? "reached" : "unreached"}" data-testid="probe-chip-${sw.id}">
          ${reached ? "✓" : "✕"} ${escapeHtml(sw.name)}
        </span>`;
      })
      .join("");

    const paths = (probe.deliveries || [])
      .map((delivery) => {
        const pathNames = delivery.path_switch_ids.map(switchName).join(" → ");
        const links = delivery.path_link_ids.length
          ? `；经 ${delivery.path_link_ids.map((id) => id.toUpperCase()).join("、")}`
          : "；广播源本机";
        return `<li><b>${escapeHtml(delivery.switch_name)}</b>（${delivery.hop} 跳）：${escapeHtml(
          pathNames
        )}${escapeHtml(links)}</li>`;
      })
      .join("");

    broadcastResult.innerHTML = `
      <div class="probe-summary" data-testid="probe-summary">${escapeHtml(probe.message)}</div>
      <div class="chip-row" data-testid="reached-switches"
           data-reached-ids="${escapeHtml(probe.reached.join(","))}"
           data-unreached-ids="${escapeHtml(probe.unreached.join(","))}">${chips}</div>
      <ol class="path-list" data-testid="probe-paths">${paths}</ol>
      <p class="probe-meta">
        探测时刻 ${formatClock(probe.at_clock_ms)}；当时实际转发链路：
        ${(probe.forwarding_link_ids || []).map((id) => id.toUpperCase()).join("、") || "无"}；
        无环检查：${probe.loop_free ? "通过" : "失败"}。
      </p>`;
  }

  function renderAll() {
    renderBadgeAndClock();
    renderTopology();
    renderLinks();
    renderCommands();
    renderEvents();
    renderProbe();
  }

  linkList.addEventListener("click", async (event) => {
    const button = event.target.closest(".plug-button");
    if (!button) return;
    const card = button.closest(".link-card");
    const linkId = card.dataset.linkId;
    const link = linkById(linkId);
    if (!link) return;
    button.disabled = true;
    try {
      const next = await api(`/api/links/${linkId}/${link.plugged ? "unplug" : "plug"}`, { method: "POST" });
      setState(next);
    } catch (error) {
      showError(error.message);
      button.disabled = false;
    }
  });

  linkList.addEventListener("change", async (event) => {
    const target = event.target;
    const card = target.closest(".link-card");
    if (!card) return;
    const linkId = card.dataset.linkId;

    try {
      if (target.classList.contains("backup-radio")) {
        const next = await api("/api/backup", {
          method: "POST",
          body: JSON.stringify({ link_id: linkId }),
        });
        setState(next);
        return;
      }

      if (target.classList.contains("open-delay") || target.classList.contains("block-delay")) {
        const field = target.dataset.delayField;
        const value = Number(target.value);
        if (!Number.isFinite(value) || value < 0) {
          throw new Error("延时必须是不小于 0 的数字");
        }
        const next = await api(`/api/links/${linkId}/delays`, {
          method: "PATCH",
          body: JSON.stringify({ [field]: value }),
        });
        setState(next);
      }
    } catch (error) {
      showError(error.message);
      refresh();
    }
  });

  backupSelect?.addEventListener("change", async () => {
    const linkId = backupSelect.value;
    try {
      const next = await api("/api/backup", {
        method: "POST",
        body: JSON.stringify({ link_id: linkId }),
      });
      setState(next);
    } catch (error) {
      showError(error.message);
      refresh();
    }
  });

  broadcastForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const source = new FormData(broadcastForm).get("source") || "s1";
    selectedSource = String(source);
    broadcastButton.disabled = true;
    try {
      const next = await api("/api/broadcast", {
        method: "POST",
        body: JSON.stringify({ source: selectedSource }),
      });
      setState(next);
    } catch (error) {
      showError(error.message);
    } finally {
      broadcastButton.disabled = false;
    }
  });

  resetButton.addEventListener("click", async () => {
    resetButton.disabled = true;
    try {
      const next = await api("/api/reset", { method: "POST" });
      selectedSource = "s1";
      const radio = broadcastForm.querySelector('input[name="source"][value="s1"]');
      if (radio) radio.checked = true;
      setState(next);
    } catch (error) {
      showError(error.message);
    } finally {
      resetButton.disabled = false;
    }
  });

  broadcastForm.querySelectorAll('input[name="source"]').forEach((radio) => {
    radio.addEventListener("change", () => selectSource(radio.value));
  });

  // Initial server-rendered state, then polling for command deadlines.
  const initialRadio = broadcastForm.querySelector(`input[name="source"][value="${selectedSource}"]`);
  if (initialRadio) initialRadio.checked = true;
  renderAll();
  setInterval(renderAll, 250);
  setInterval(refresh, 750);
})();
