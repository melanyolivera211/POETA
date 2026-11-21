/**
 * POETA — Dashboard Minimalista de Creación Literaria Autónoma (frontend/js/dashboard.js)
 */

(() => {
  "use strict";

  const ROMAN_NUMERALS = ["I", "II", "III", "IV", "V", "VI", "VII"];

  const EVENT_LABELS = {
    AGENT_STARTED: "Agente Iniciado",
    CYCLE_CREATED: "Ciclo Creado",
    MEMORY_RECOVERED: "Memoria Recuperada",
    GEMINI_REQUEST_SENT: "Petición a Gemini AI",
    GEMINI_RESPONSE_RECEIVED: "Respuesta de Gemini AI",
    STANZA_GENERATED: "Estrofa Generada",
    VERSION_STORED: "Versión Almacenada",
    INTERVENTION_COMPLETED: "Intervención Completada",
    STATE_TRANSITION: "Cambio de Estado",
    RECOVERY_COMPLETED: "Recuperación",
    ERROR_DETECTED: "Error Detectado",
    POEM_COMPLETED: "Poema Finalizado",
  };

  const state = {
    agentStatus: null,
    currentPoem: null,
    inspectedPoemId: null,
    inspectedVersions: [],
    historyPage: 1,
    historyTotalPages: 1,
    logsPage: 1,
    logsTotalPages: 1,
    logsEventType: "",
    pollTimer: null,
    liveCountdownTimer: null,
  };

  const el = (id) => document.getElementById(id);

  function formatSecondsToMMSS(totalSeconds) {
    const sec = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const minutes = Math.floor(sec / 60);
    const seconds = sec % 60;
    return `${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function showAlert(message, type = "error") {
    const banner = el("globalAlertBanner");
    if (!banner) return;
    banner.textContent = message;
    banner.className = `alert-banner ${type}`;
    banner.classList.remove("hidden");
    setTimeout(() => {
      banner.classList.add("hidden");
    }, 7000);
  }

  function switchView(targetViewId) {
    document.querySelectorAll(".dashboard-view").forEach((section) => {
      section.classList.toggle("active", section.id === targetViewId);
    });
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      const isSelected = btn.getAttribute("data-view") === targetViewId;
      btn.classList.toggle("active", isSelected);
      btn.setAttribute("aria-selected", isSelected ? "true" : "false");
    });
  }

  function setupNavigation() {
    document.querySelectorAll(".nav-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const target = tab.getAttribute("data-view");
        if (target) switchView(target);
      });
    });

    document.querySelectorAll("[data-goto-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-goto-view");
        if (target) switchView(target);
      });
    });
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  }

  async function refreshHealth() {
    const { data } = await fetchJson("/api/agent/health");
    if (!data || !data.components) return;

    const backendChip = el("healthBackendChip");
    const dbChip = el("healthDbChip");
    const aiChip = el("healthAiChip");

    if (backendChip) {
      backendChip.textContent = "API";
      backendChip.className = "service-chip ok";
    }
    if (dbChip) {
      const dbOk = Boolean(data.components.database?.available);
      dbChip.textContent = "Supabase";
      dbChip.className = `service-chip ${dbOk ? "ok" : "warn"}`;
      dbChip.title = data.components.database?.message || "";
    }
    if (aiChip) {
      const aiOk = Boolean(data.components.ai_provider?.available);
      aiChip.textContent = "Gemini AI";
      aiChip.className = `service-chip ${aiOk ? "ok" : "warn"}`;
      aiChip.title = data.components.ai_provider?.message || "";
    }
  }

  async function refreshAgentStatus() {
    const { ok, data } = await fetchJson("/api/agent/status");
    if (!ok || !data?.data) return;

    state.agentStatus = data.data;
    renderHeaderAndMainPanel(data.data);
  }

  async function refreshCurrentPoem() {
    const { ok, data } = await fetchJson("/api/poem/current");
    if (!ok) return;

    state.currentPoem = data.poem || null;
    if (!state.inspectedPoemId && state.currentPoem) {
      state.inspectedPoemId = state.currentPoem.id;
    }

    if (state.currentPoem && state.inspectedPoemId === state.currentPoem.id) {
      renderPoemFolio(state.currentPoem, data.has_active_poem);
      state.inspectedVersions = state.currentPoem.versions || [];
      renderEvolutionView(state.currentPoem, state.inspectedVersions);
    } else if (!state.currentPoem) {
      renderPoemFolio(null, false);
      renderEvolutionView(null, []);
    }
  }

  async function loadPoemVersionsById(poemId) {
    const { ok, data } = await fetchJson(`/api/poem/${encodeURIComponent(poemId)}/versions`);
    if (!ok || !data?.poem) return;

    state.inspectedPoemId = data.poem.id;
    state.inspectedVersions = data.versions || [];
    const isActive = state.agentStatus?.active_poem?.id === data.poem.id;
    renderPoemFolio({ ...data.poem, versions: state.inspectedVersions }, isActive);
    renderEvolutionView(data.poem, state.inspectedVersions);
  }

  async function refreshHistory(page = 1) {
    const { ok, data } = await fetchJson(`/api/poem/history?page=${page}&per_page=8`);
    if (!ok || !data) return;

    state.historyPage = data.pagination?.page || 1;
    state.historyTotalPages = data.pagination?.total_pages || 1;
    renderHistorySidebar(data.items || [], data.pagination);
  }

  async function refreshLogs(page = 1) {
    let url = `/api/agent/logs?page=${page}&per_page=20`;
    if (state.logsEventType) {
      url += `&event_type=${encodeURIComponent(state.logsEventType)}`;
    }
    const { ok, data } = await fetchJson(url);
    if (!ok || !data) return;

    state.logsPage = data.pagination?.page || 1;
    state.logsTotalPages = data.pagination?.total_pages || 1;
    renderActivityLogs(data.items || [], data.pagination);
  }

  async function refreshAllDashboard() {
    try {
      await Promise.all([
        refreshHealth(),
        refreshAgentStatus(),
        refreshCurrentPoem(),
        refreshHistory(state.historyPage),
        refreshLogs(state.logsPage),
      ]);
    } catch (err) {
      console.error("Error sincronizando dashboard POETA:", err);
    }
  }

  function renderHeaderAndMainPanel(statusData) {
    const status = statusData.agent_status || "IDLE";
    const isActive = Boolean(statusData.is_cycle_active);
    const activePoem = statusData.active_poem;
    const latestPoem = statusData.latest_poem;
    const displayPoem = activePoem || latestPoem;

    if (el("headerAgentStatus")) el("headerAgentStatus").textContent = status;

    const statusDot = el("headerStatusDot");
    if (statusDot) {
      statusDot.className = "status-dot";
      if (status === "RUNNING" || status === "INITIALIZING") statusDot.classList.add("running");
      else if (status === "GENERATING" || status === "RECOVERING") statusDot.classList.add("generating");
      else if (status === "ERROR") statusDot.classList.add("error");
      else if (status === "COMPLETED") statusDot.classList.add("completed");
    }

    if (el("headerTimezone")) {
      el("headerTimezone").textContent = statusData.display_timezone || "America/Bogota";
    }
    if (el("headerServerClock") && statusData.server_time_display) {
      const timeOnly = statusData.server_time_display.split(" ")[1] || statusData.server_time_display;
      el("headerServerClock").textContent = timeOnly;
    }

    const adminGroup = el("adminTokenGroup");
    if (adminGroup) {
      adminGroup.classList.toggle("hidden", !statusData.admin_auth_required);
    }

    const opText = el("workerOperationText");
    if (opText) {
      opText.textContent = isActive
        ? `Ciclo en curso · ${status}`
        : status === "COMPLETED"
        ? "Obra completada"
        : "Agente en reposo";
    }

    if (el("panelActivePoemTitle")) {
      el("panelActivePoemTitle").textContent = activePoem
        ? activePoem.title
        : latestPoem
        ? latestPoem.title
        : "Listo para iniciar una nueva obra";
    }

    if (el("panelActivePoemTheme")) {
      el("panelActivePoemTheme").textContent = activePoem
        ? activePoem.theme
        : latestPoem
        ? latestPoem.theme
        : "Ciclo autónomo de 60 minutos: una estrofa cada 10 minutos con memoria persistente en Supabase.";
    }

    const completedInterventions = Number(statusData.intervention_number || 0);
    const totalInterventions = Number(statusData.total_interventions || 6);
    const progressPercent = Number(statusData.progress_percent || 0);

    if (el("panelProgressPercent")) {
      el("panelProgressPercent").textContent = `${progressPercent}%`;
    }
    if (el("panelInterventionCounter")) {
      el("panelInterventionCounter").textContent = `${completedInterventions} / ${totalInterventions} estrofas`;
    }
    if (el("cycleProgressBarFill")) {
      el("cycleProgressBarFill").style.width = `${Math.min(100, progressPercent)}%`;
    }

    document.querySelectorAll("#cycleMilestonesGrid .step-item").forEach((stepEl) => {
      const stepAttr = stepEl.getAttribute("data-step");
      stepEl.classList.remove("done", "current");
      if (stepAttr === "close") {
        if (status === "COMPLETED" && !isActive && latestPoem) {
          stepEl.classList.add("done");
        } else if (isActive && completedInterventions >= totalInterventions) {
          stepEl.classList.add("current");
        }
      } else {
        const stepNum = Number(stepAttr);
        if (completedInterventions >= stepNum) {
          stepEl.classList.add("done");
        } else if (isActive && completedInterventions + 1 === stepNum) {
          stepEl.classList.add("current");
        }
      }
    });

    const btnStart = el("btnStartCycle");
    const btnStartText = el("btnStartCycleText");
    const seedInput = el("seedThemeInput");
    if (btnStart && btnStartText) {
      btnStart.disabled = isActive;
      if (seedInput) seedInput.disabled = isActive;
      btnStartText.textContent = isActive ? "Ciclo en Curso" : "Iniciar Ciclo";
    }

    if (el("metricElapsed")) {
      el("metricElapsed").textContent = formatSecondsToMMSS(statusData.elapsed_seconds);
    }
    if (el("metricRemaining")) {
      el("metricRemaining").textContent = formatSecondsToMMSS(statusData.remaining_seconds);
    }
    if (el("metricNextExecution")) {
      el("metricNextExecution").textContent = statusData.next_execution_display || "—";
    }
    if (el("metricLastExecution")) {
      el("metricLastExecution").textContent = statusData.last_execution_display || "—";
    }

    const previewEl = el("panelLatestStanzaPreview");
    if (previewEl) {
      const stanzas = displayPoem?.stanzas || [];
      if (stanzas.length > 0) {
        const lastIdx = stanzas.length - 1;
        previewEl.textContent = stanzas[lastIdx];
      } else {
        previewEl.textContent = "Aún no se han generado versos en este ciclo.";
      }
    }
  }

  function renderPoemFolio(poem, isActive) {
    const badge = el("folioCycleBadge");
    const titleEl = el("folioTitle");
    const themeEl = el("folioTheme");
    const statusEl = el("folioStatus");
    const intEl = el("folioInterventions");
    const startEl = el("folioStartedAt");
    const updEl = el("folioUpdatedAt");
    const container = el("folioStanzasContainer");

    if (!poem) {
      if (badge) badge.textContent = "EN ESPERA";
      if (titleEl) titleEl.textContent = "Sin composición activa";
      if (themeEl) themeEl.textContent = "Inicia un ciclo desde el Panel para comenzar la obra.";
      if (statusEl) statusEl.textContent = "IDLE";
      if (intEl) intEl.textContent = "0 / 6 estrofas";
      if (startEl) startEl.textContent = "—";
      if (updEl) updEl.textContent = "—";
      if (container) {
        container.innerHTML = '<p class="empty-state">No hay estrofas almacenadas todavía.</p>';
      }
      return;
    }

    if (badge) {
      badge.textContent = isActive ? "OBRA EN CURSO" : "OBRA ARCHIVADA";
    }
    if (titleEl) titleEl.textContent = poem.title;
    if (themeEl) themeEl.textContent = poem.theme;
    if (statusEl) statusEl.textContent = poem.status;
    if (intEl) {
      intEl.textContent = `${poem.completed_interventions} / ${poem.total_interventions} estrofas`;
    }
    if (startEl) startEl.textContent = poem.started_at_display || "—";
    if (updEl) updEl.textContent = poem.updated_at_display || "—";

    if (!container) return;

    const versions = poem.versions || [];
    const stanzas = poem.stanzas || [];

    if (versions.length === 0 && stanzas.length === 0) {
      container.innerHTML = '<p class="empty-state">Generando primera estrofa...</p>';
      return;
    }

    const list =
      versions.length > 0
        ? versions
        : stanzas.map((s, idx) => ({
            intervention_number: idx + 1,
            added_stanza: s,
            generated_at_display: poem.updated_at_display || "",
          }));

    container.innerHTML = list
      .map((v) => {
        const idx = (v.intervention_number || 1) - 1;
        const roman = ROMAN_NUMERALS[idx] || String(v.intervention_number);
        return `
          <article class="stanza-block">
            <div class="stanza-block-header">
              <span class="stanza-roman">${escapeHtml(roman)}</span>
              <span class="stanza-time">${escapeHtml(v.generated_at_display || "")}</span>
            </div>
            <div class="stanza-verses">${escapeHtml(v.added_stanza)}</div>
          </article>
        `;
      })
      .join("");
  }

  function renderHistorySidebar(items, pagination) {
    const listEl = el("poemHistoryList");
    if (!listEl) return;

    if (!items || items.length === 0) {
      listEl.innerHTML = '<p class="empty-small">Sin obras archivadas aún.</p>';
    } else {
      listEl.innerHTML = items
        .map(
          (p) => `
          <div class="history-item" data-poem-id="${escapeHtml(p.id)}" role="button" tabindex="0">
            <div class="history-item-title">${escapeHtml(p.title)}</div>
            <div class="history-item-meta">
              ${escapeHtml(p.completed_interventions)}/${escapeHtml(p.total_interventions)} estrofas &middot;
              ${escapeHtml(p.completed_at_display || p.updated_at_display || "")}
            </div>
          </div>
        `
        )
        .join("");

      listEl.querySelectorAll(".history-item").forEach((itemEl) => {
        const handler = () => {
          const pid = itemEl.getAttribute("data-poem-id");
          if (pid) loadPoemVersionsById(pid);
        };
        itemEl.addEventListener("click", handler);
        itemEl.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            handler();
          }
        });
      });
    }

    const prevBtn = el("btnHistoryPrev");
    const nextBtn = el("btnHistoryNext");
    const infoEl = el("historyPageInfo");
    if (prevBtn) prevBtn.disabled = (pagination?.page || 1) <= 1;
    if (nextBtn) nextBtn.disabled = (pagination?.page || 1) >= (pagination?.total_pages || 1);
    if (infoEl) {
      infoEl.textContent = `${pagination?.page || 1} / ${Math.max(1, pagination?.total_pages || 1)}`;
    }
  }

  function renderEvolutionView(poem, versions) {
    const titleEl = el("evolutionPoemTitle");
    const pillsBar = el("versionPillsBar");
    const selectA = el("selectVersionA");
    const selectB = el("selectVersionB");

    if (titleEl) {
      titleEl.textContent = poem
        ? `${poem.title} · ${versions.length} de 6 versiones`
        : "Ninguna obra seleccionada";
    }

    if (!versions || versions.length === 0) {
      if (pillsBar) pillsBar.innerHTML = "";
      if (selectA) selectA.innerHTML = "<option>Sin versiones</option>";
      if (selectB) selectB.innerHTML = "<option>Sin versiones</option>";
      renderVersionComparisonPanes(null, null);
      return;
    }

    if (pillsBar) {
      pillsBar.innerHTML = versions
        .map(
          (v, idx) => `
          <button type="button" class="version-pill ${idx === versions.length - 1 ? "active" : ""}" data-v-num="${v.intervention_number}">
            V${v.intervention_number} (${String((v.intervention_number - 1) * 10).padStart(2, "0")}m)
          </button>
        `
        )
        .join("");

      pillsBar.querySelectorAll(".version-pill").forEach((btn) => {
        btn.addEventListener("click", () => {
          pillsBar.querySelectorAll(".version-pill").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          const num = Number(btn.getAttribute("data-v-num"));
          const prevNum = Math.max(1, num - 1);
          if (selectA) selectA.value = String(prevNum);
          if (selectB) selectB.value = String(num);
          updateComparisonFromSelects(versions);
        });
      });
    }

    const optionsHtml = versions
      .map(
        (v) =>
          `<option value="${v.intervention_number}">Versión ${v.intervention_number} — Estrofa ${ROMAN_NUMERALS[v.intervention_number - 1] || v.intervention_number}</option>`
      )
      .join("");

    const prevValA = selectA?.value;
    const prevValB = selectB?.value;

    if (selectA) selectA.innerHTML = optionsHtml;
    if (selectB) selectB.innerHTML = optionsHtml;

    const defaultB = versions[versions.length - 1].intervention_number;
    const defaultA = versions.length > 1 ? versions[versions.length - 2].intervention_number : defaultB;

    if (selectA) {
      selectA.value = versions.some((v) => String(v.intervention_number) === prevValA)
        ? prevValA
        : String(defaultA);
    }
    if (selectB) {
      selectB.value = versions.some((v) => String(v.intervention_number) === prevValB)
        ? prevValB
        : String(defaultB);
    }

    updateComparisonFromSelects(versions);
  }

  function updateComparisonFromSelects(versions) {
    const selectA = el("selectVersionA");
    const selectB = el("selectVersionB");
    if (!selectA || !selectB) return;

    const numA = Number(selectA.value);
    const numB = Number(selectB.value);
    const verA = versions.find((v) => v.intervention_number === numA) || versions[0];
    const verB = versions.find((v) => v.intervention_number === numB) || versions[versions.length - 1];
    renderVersionComparisonPanes(verA, verB);
  }

  function renderVersionComparisonPanes(verA, verB) {
    if (!verA || !verB) {
      if (el("paneABadge")) el("paneABadge").textContent = "Versión —";
      if (el("paneAMeta")) el("paneAMeta").textContent = "—";
      if (el("paneAAddedStanza")) el("paneAAddedStanza").textContent = "—";
      if (el("paneAFullText")) el("paneAFullText").textContent = "—";
      if (el("paneBBadge")) el("paneBBadge").textContent = "Versión —";
      if (el("paneBMeta")) el("paneBMeta").textContent = "—";
      if (el("paneBAddedStanza")) el("paneBAddedStanza").textContent = "—";
      if (el("paneBFullText")) el("paneBFullText").textContent = "—";
      if (el("evolutionPromptSnapshot")) {
        el("evolutionPromptSnapshot").textContent = "Sin versión seleccionada.";
      }
      return;
    }

    if (el("paneABadge")) el("paneABadge").textContent = `Versión ${verA.intervention_number}`;
    if (el("paneAMeta")) {
      el("paneAMeta").textContent = `${verA.generated_at_display || ""} · ${verA.generation_duration_ms} ms`;
    }
    if (el("paneAAddedStanza")) el("paneAAddedStanza").textContent = verA.added_stanza;
    if (el("paneAFullText")) el("paneAFullText").textContent = verA.full_text;

    if (el("paneBBadge")) el("paneBBadge").textContent = `Versión ${verB.intervention_number}`;
    if (el("paneBMeta")) {
      el("paneBMeta").textContent = `${verB.generated_at_display || ""} · ${verB.generation_duration_ms} ms`;
    }
    if (el("paneBAddedStanza")) el("paneBAddedStanza").textContent = verB.added_stanza;
    if (el("paneBFullText")) el("paneBFullText").textContent = verB.full_text;

    if (el("evolutionPromptSnapshot")) {
      el("evolutionPromptSnapshot").textContent = verB.prompt_snapshot || "";
    }
  }

  function renderActivityLogs(items, pagination) {
    const container = el("activityTimelineContainer");
    if (!container) return;

    if (!items || items.length === 0) {
      container.innerHTML = '<p class="empty-state">No hay eventos registrados aún.</p>';
    } else {
      container.innerHTML = items
        .map((log) => {
          const label = EVENT_LABELS[log.event_type] || log.event_type;
          return `
            <div class="timeline-event">
              <div class="event-main">
                <div class="event-badges">
                  <span class="event-type-tag">${escapeHtml(label)}</span>
                  <span class="event-status-tag ${escapeHtml(log.status)}">${escapeHtml(log.status)}</span>
                </div>
                <p class="event-description">${escapeHtml(log.description)}</p>
              </div>
              <span class="event-timestamp">${escapeHtml(log.created_at_display || "")}</span>
            </div>
          `;
        })
        .join("");
    }

    const prevBtn = el("btnLogsPrev");
    const nextBtn = el("btnLogsNext");
    const infoEl = el("logsPageInfo");
    if (prevBtn) prevBtn.disabled = (pagination?.page || 1) <= 1;
    if (nextBtn) nextBtn.disabled = (pagination?.page || 1) >= (pagination?.total_pages || 1);
    if (infoEl) {
      infoEl.textContent = `Página ${pagination?.page || 1} de ${Math.max(1, pagination?.total_pages || 1)}`;
    }
  }

  function setupEventListeners() {
    const startForm = el("startCycleForm");
    if (startForm) {
      startForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const btn = el("btnStartCycle");
        const btnText = el("btnStartCycleText");
        const seedTheme = el("seedThemeInput")?.value?.trim() || "";
        const adminToken = el("adminTokenInput")?.value?.trim() || "";

        if (btn) btn.disabled = true;
        if (btnText) btnText.textContent = "Generando...";

        const headers = { "Content-Type": "application/json" };
        if (adminToken) {
          headers["X-Admin-Token"] = adminToken;
        }

        try {
          const { ok, data } = await fetchJson("/api/agent/start", {
            method: "POST",
            headers,
            body: JSON.stringify(seedTheme ? { seed_theme: seedTheme } : {}),
          });

          if (!ok) {
            showAlert(
              data?.message || "No fue posible iniciar el ciclo de creación.",
              "error"
            );
          } else {
            showAlert(
              `Ciclo iniciado: "${data.poem?.title || "Nueva obra"}".`,
              "success"
            );
            state.inspectedPoemId = data.poem?.id || null;
            if (el("seedThemeInput")) el("seedThemeInput").value = "";
          }
        } catch (err) {
          showAlert(`Error de red al iniciar el ciclo: ${err}`, "error");
        } finally {
          await refreshAllDashboard();
        }
      });
    }

    el("btnManualRefresh")?.addEventListener("click", () => refreshAllDashboard());

    el("btnRestoreCurrentPoem")?.addEventListener("click", () => {
      state.inspectedPoemId = state.currentPoem?.id || null;
      refreshCurrentPoem();
    });

    el("selectVersionA")?.addEventListener("change", () =>
      updateComparisonFromSelects(state.inspectedVersions)
    );
    el("selectVersionB")?.addEventListener("change", () =>
      updateComparisonFromSelects(state.inspectedVersions)
    );

    const logFilter = el("logEventTypeFilter");
    if (logFilter) {
      logFilter.addEventListener("change", () => {
        state.logsEventType = logFilter.value;
        state.logsPage = 1;
        refreshLogs(1);
      });
    }

    el("btnHistoryPrev")?.addEventListener("click", () => {
      if (state.historyPage > 1) refreshHistory(state.historyPage - 1);
    });
    el("btnHistoryNext")?.addEventListener("click", () => {
      if (state.historyPage < state.historyTotalPages) refreshHistory(state.historyPage + 1);
    });

    el("btnLogsPrev")?.addEventListener("click", () => {
      if (state.logsPage > 1) refreshLogs(state.logsPage - 1);
    });
    el("btnLogsNext")?.addEventListener("click", () => {
      if (state.logsPage < state.logsTotalPages) refreshLogs(state.logsPage + 1);
    });
  }

  function startControlledPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (!document.hidden) {
        refreshAllDashboard();
      }
    }, 10000);

    if (state.liveCountdownTimer) clearInterval(state.liveCountdownTimer);
    state.liveCountdownTimer = setInterval(() => {
      if (!state.agentStatus || !state.agentStatus.is_cycle_active) return;
      if (state.agentStatus.remaining_seconds > 0) {
        state.agentStatus.elapsed_seconds = Math.min(
          state.agentStatus.cycle_duration_seconds,
          state.agentStatus.elapsed_seconds + 1
        );
        state.agentStatus.remaining_seconds = Math.max(
          0,
          state.agentStatus.remaining_seconds - 1
        );
        if (el("metricElapsed")) {
          el("metricElapsed").textContent = formatSecondsToMMSS(state.agentStatus.elapsed_seconds);
        }
        if (el("metricRemaining")) {
          el("metricRemaining").textContent = formatSecondsToMMSS(
            state.agentStatus.remaining_seconds
          );
        }
      }
    }, 1000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupNavigation();
    setupEventListeners();
    refreshAllDashboard();
    startControlledPolling();
  });
})();
