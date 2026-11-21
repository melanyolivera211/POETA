/**
 * POETA — Dashboard Editorial de Monitoreo y Evolución Literaria (frontend/js/dashboard.js)
 *
 * Responsabilidades:
 * - Consultar de forma periódica y controlada la API Flask (`/api/agent/status`,
 *   `/api/poem/current`, `/api/poem/history`, `/api/poem/{id}/versions`,
 *   `/api/agent/logs`, `/api/agent/health`).
 * - Actualizar las 4 vistas obligatorias con datos reales procedentes de Supabase.
 * - Permitir iniciar un ciclo poético (`POST /api/agent/start`) únicamente cuando
 *   no exista otro activo.
 * - El navegador NUNCA ejecuta ni programa las intervenciones del agente.
 */

(() => {
  "use strict";

  const ROMAN_NUMERALS = ["I", "II", "III", "IV", "V", "VI", "VII"];

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

  // Referencias DOM
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
    }, 8000);
  }

  // ==========================================================================
  // NAVEGACIÓN DE VISTAS
  // ==========================================================================
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

  // ==========================================================================
  // LLAMADAS A LA API REST DE POETA
  // ==========================================================================
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
      backendChip.textContent = "API: Operativa";
      backendChip.className = "health-chip ok";
    }
    if (dbChip) {
      const dbOk = Boolean(data.components.database?.available);
      dbChip.textContent = dbOk ? "Supabase: Conectado" : "Supabase: Pendiente";
      dbChip.className = `health-chip ${dbOk ? "ok" : "warn"}`;
      dbChip.title = data.components.database?.message || "";
    }
    if (aiChip) {
      const aiOk = Boolean(data.components.ai_provider?.available);
      const aiCfg = Boolean(data.components.ai_provider?.configured);
      aiChip.textContent = aiOk
        ? "Ollama Cloud: Listo"
        : aiCfg
        ? "Ollama Cloud: Error"
        : "Ollama Cloud: Sin API Key";
      aiChip.className = `health-chip ${aiOk ? "ok" : "warn"}`;
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
      const syncEl = el("lastSyncText");
      if (syncEl) {
        const nowStr = new Date().toLocaleTimeString("es-CO", {
          timeZone: "America/Bogota",
          hour12: false,
        });
        syncEl.textContent = `Actualizado: ${nowStr}`;
      }
    } catch (err) {
      console.error("Error sincronizando dashboard POETA:", err);
    }
  }

  // ==========================================================================
  // RENDERIZADO VISTA 1: PANEL PRINCIPAL
  // ==========================================================================
  function renderHeaderAndMainPanel(statusData) {
    const status = statusData.agent_status || "IDLE";
    const isActive = Boolean(statusData.is_cycle_active);
    const activePoem = statusData.active_poem;
    const latestPoem = statusData.latest_poem;
    const displayPoem = activePoem || latestPoem;

    // Cabecera
    const headerStatus = el("headerAgentStatus");
    if (headerStatus) headerStatus.textContent = status;

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
      el("headerServerClock").textContent = statusData.server_time_display;
    }

    // Mostrar campo de token administrativo si el backend lo requiere
    const adminGroup = el("adminTokenGroup");
    if (adminGroup) {
      adminGroup.classList.toggle("hidden", !statusData.admin_auth_required);
    }

    // Estado operativo en tarjeta principal
    const opText = el("workerOperationText");
    if (opText) {
      opText.textContent = isActive
        ? `Ciclo activo en ejecución (${status})`
        : status === "COMPLETED"
        ? "Último ciclo finalizado y archivado"
        : "Agente en reposo (IDLE)";
    }

    if (el("panelActivePoemTitle")) {
      el("panelActivePoemTitle").textContent = activePoem
        ? activePoem.title
        : latestPoem
        ? `Última obra archivada: ${latestPoem.title}`
        : "Sin ciclo activo en ejecución";
    }

    if (el("panelActivePoemTheme")) {
      el("panelActivePoemTheme").textContent = activePoem
        ? `Temática: ${activePoem.theme}`
        : latestPoem
        ? `Temática: ${latestPoem.theme}`
        : "Inicie un ciclo de creación literaria para que POETA componga progresivamente una obra de 6 estrofas durante 60 minutos.";
    }

    const completedInterventions = Number(statusData.intervention_number || 0);
    const totalInterventions = Number(statusData.total_interventions || 6);
    const progressPercent = Number(statusData.progress_percent || 0);

    if (el("panelProgressPercent")) {
      el("panelProgressPercent").textContent = `${progressPercent}%`;
    }
    if (el("panelInterventionCounter")) {
      el("panelInterventionCounter").textContent = `${completedInterventions} / ${totalInterventions}`;
    }
    if (el("cycleProgressBarFill")) {
      el("cycleProgressBarFill").style.width = `${Math.min(100, progressPercent)}%`;
    }
    if (el("cycleProgressBarTrack")) {
      el("cycleProgressBarTrack").setAttribute("aria-valuenow", String(Math.round(progressPercent)));
    }

    // Hitos de los 60 minutos
    document.querySelectorAll("#cycleMilestonesGrid .milestone-step").forEach((stepEl) => {
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

    // Botón de inicio de ciclo (deshabilitado si ya hay un poema activo)
    const btnStart = el("btnStartCycle");
    const btnStartText = el("btnStartCycleText");
    const seedInput = el("seedThemeInput");
    if (btnStart && btnStartText) {
      btnStart.disabled = isActive;
      if (seedInput) seedInput.disabled = isActive;
      btnStartText.textContent = isActive
        ? "Ciclo Poético en Ejecución (Creación Bloqueada)"
        : "Iniciar Ciclo Poético de 60 Minutos";
    }

    // Métricas temporales
    if (el("metricElapsed")) {
      el("metricElapsed").textContent = formatSecondsToMMSS(statusData.elapsed_seconds);
    }
    if (el("metricRemaining")) {
      el("metricRemaining").textContent = formatSecondsToMMSS(statusData.remaining_seconds);
    }
    if (el("metricNextExecution")) {
      el("metricNextExecution").textContent =
        statusData.next_execution_display || "Sin ejecución pendiente";
    }
    if (el("metricLastExecution")) {
      el("metricLastExecution").textContent =
        statusData.last_execution_display || "Sin ejecuciones registradas";
    }
    if (el("metricLastState")) {
      el("metricLastState").textContent = `Estado de máquina: ${status}`;
    }

    // Vista previa de la última estrofa
    const previewEl = el("panelLatestStanzaPreview");
    if (previewEl) {
      const stanzas = displayPoem?.stanzas || [];
      if (stanzas.length > 0) {
        const lastIdx = stanzas.length - 1;
        previewEl.textContent = `[Estrofa ${ROMAN_NUMERALS[lastIdx] || lastIdx + 1}]\n${stanzas[lastIdx]}`;
      } else {
        previewEl.textContent = "Aún no hay versos generados en el ciclo actual.";
      }
    }
  }

  // ==========================================================================
  // RENDERIZADO VISTA 2: POEMA ACTUAL Y ARCHIVO
  // ==========================================================================
  function renderPoemFolio(poem, isActive) {
    const badge = el("folioCycleBadge");
    const idEl = el("folioPoemId");
    const titleEl = el("folioTitle");
    const themeEl = el("folioTheme");
    const statusEl = el("folioStatus");
    const intEl = el("folioInterventions");
    const startEl = el("folioStartedAt");
    const updEl = el("folioUpdatedAt");
    const container = el("folioStanzasContainer");

    if (!poem) {
      if (badge) badge.textContent = "SIN COMPOSICIÓN REGISTRADA";
      if (idEl) idEl.textContent = "ID: --";
      if (titleEl) titleEl.textContent = "Esperando el inicio del primer ciclo poético";
      if (themeEl) {
        themeEl.textContent =
          "Inicie POETA desde el Panel Principal para generar el título, la temática y la primera estrofa.";
      }
      if (statusEl) statusEl.textContent = "IDLE";
      if (intEl) intEl.textContent = "0 de 6";
      if (startEl) startEl.textContent = "--";
      if (updEl) updEl.textContent = "--";
      if (container) {
        container.innerHTML =
          '<p class="empty-literary-state">No hay estrofas almacenadas en Supabase todavía.</p>';
      }
      return;
    }

    if (badge) {
      badge.textContent = isActive
        ? "COMPOSICIÓN ACTIVA EN EVOLUCIÓN"
        : "OBRA FINALIZADA Y ARCHIVADA EN SUPABASE";
    }
    if (idEl) idEl.textContent = `ID: ${poem.id}`;
    if (titleEl) titleEl.textContent = poem.title;
    if (themeEl) themeEl.textContent = poem.theme;
    if (statusEl) statusEl.textContent = poem.status;
    if (intEl) {
      intEl.textContent = `${poem.completed_interventions} de ${poem.total_interventions}`;
    }
    if (startEl) startEl.textContent = poem.started_at_display || "--";
    if (updEl) updEl.textContent = poem.updated_at_display || "--";

    if (!container) return;

    const versions = poem.versions || [];
    const stanzas = poem.stanzas || [];

    if (versions.length === 0 && stanzas.length === 0) {
      container.innerHTML =
        '<p class="empty-literary-state">Preparando la primera estrofa con Ollama Cloud...</p>';
      return;
    }

    const blocksHtml = (versions.length > 0 ? versions : stanzas.map((s, idx) => ({
      intervention_number: idx + 1,
      added_stanza: s,
      generated_at_display: poem.updated_at_display || "--",
      model_name: "--",
    }))).map((v) => {
      const idx = (v.intervention_number || 1) - 1;
      const roman = ROMAN_NUMERALS[idx] || String(v.intervention_number);
      const minuteMark = idx * 10;
      return `
        <article class="stanza-block">
          <div class="stanza-block-header">
            <span class="stanza-roman">ESTROFA ${escapeHtml(roman)} &middot; INTERVENCIÓN ${v.intervention_number}/6 (MINUTO ${String(minuteMark).padStart(2, "0")})</span>
            <span>${escapeHtml(v.generated_at_display || "")}</span>
          </div>
          <div class="stanza-verses">${escapeHtml(v.added_stanza)}</div>
        </article>
      `;
    });

    container.innerHTML = blocksHtml.join("");
  }

  function renderHistorySidebar(items, pagination) {
    const listEl = el("poemHistoryList");
    if (!listEl) return;

    if (!items || items.length === 0) {
      listEl.innerHTML = '<p class="empty-small">No hay poemas finalizados en el archivo.</p>';
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
    if (nextBtn) {
      nextBtn.disabled = (pagination?.page || 1) >= (pagination?.total_pages || 1);
    }
    if (infoEl) {
      infoEl.textContent = `Página ${pagination?.page || 1} de ${Math.max(1, pagination?.total_pages || 1)}`;
    }
  }

  // ==========================================================================
  // RENDERIZADO VISTA 3: EVOLUCIÓN LITERARIA (COMPARADOR DE VERSIONES)
  // ==========================================================================
  function renderEvolutionView(poem, versions) {
    const titleEl = el("evolutionPoemTitle");
    const pillsBar = el("versionPillsBar");
    const selectA = el("selectVersionA");
    const selectB = el("selectVersionB");

    if (titleEl) {
      titleEl.textContent = poem ? `${poem.title} (${versions.length}/6 versiones)` : "Ninguna obra seleccionada";
    }

    if (!versions || versions.length === 0) {
      if (pillsBar) {
        pillsBar.innerHTML = '<span class="empty-small">Sin versiones almacenadas aún.</span>';
      }
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
            Versión ${v.intervention_number} (Min ${String((v.intervention_number - 1) * 10).padStart(2, "0")})
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
          `<option value="${v.intervention_number}">Versión ${v.intervention_number} — Estrofa ${ROMAN_NUMERALS[v.intervention_number - 1] || v.intervention_number} (${escapeHtml(v.generated_at_display || "")})</option>`
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
      if (el("paneABadge")) el("paneABadge").textContent = "Versión --";
      if (el("paneAMeta")) el("paneAMeta").textContent = "--";
      if (el("paneAAddedStanza")) el("paneAAddedStanza").textContent = "Sin datos";
      if (el("paneAFullText")) el("paneAFullText").textContent = "Sin datos";
      if (el("paneBBadge")) el("paneBBadge").textContent = "Versión --";
      if (el("paneBMeta")) el("paneBMeta").textContent = "--";
      if (el("paneBAddedStanza")) el("paneBAddedStanza").textContent = "Sin datos";
      if (el("paneBFullText")) el("paneBFullText").textContent = "Sin datos";
      if (el("evolutionPromptSnapshot")) {
        el("evolutionPromptSnapshot").textContent = "Seleccione una versión para inspeccionar su prompt.";
      }
      return;
    }

    if (el("paneABadge")) {
      el("paneABadge").textContent = `Versión ${verA.intervention_number} (Intervención #${verA.intervention_number})`;
    }
    if (el("paneAMeta")) {
      el("paneAMeta").textContent = `${verA.generated_at_display || ""} · ${verA.model_name} · ${verA.generation_duration_ms} ms`;
    }
    if (el("paneAAddedStanza")) el("paneAAddedStanza").textContent = verA.added_stanza;
    if (el("paneAFullText")) el("paneAFullText").textContent = verA.full_text;

    if (el("paneBBadge")) {
      el("paneBBadge").textContent = `Versión ${verB.intervention_number} (Intervención #${verB.intervention_number})`;
    }
    if (el("paneBMeta")) {
      el("paneBMeta").textContent = `${verB.generated_at_display || ""} · ${verB.model_name} · ${verB.generation_duration_ms} ms`;
    }
    if (el("paneBAddedStanza")) el("paneBAddedStanza").textContent = verB.added_stanza;
    if (el("paneBFullText")) el("paneBFullText").textContent = verB.full_text;

    if (el("evolutionPromptSnapshot")) {
      el("evolutionPromptSnapshot").textContent =
        verB.prompt_snapshot || "Snapshot de prompt no disponible.";
    }
  }

  // ==========================================================================
  // RENDERIZADO VISTA 4: REGISTRO DE ACTIVIDAD (TIMELINE)
  // ==========================================================================
  function renderActivityLogs(items, pagination) {
    const container = el("activityTimelineContainer");
    if (!container) return;

    if (!items || items.length === 0) {
      container.innerHTML =
        '<p class="empty-literary-state">No existen eventos registrados para el filtro seleccionado.</p>';
    } else {
      container.innerHTML = items
        .map((log) => {
          const traceParts = [];
          if (log.scheduled_at_bogota) {
            traceParts.push(`<span>Programada: ${escapeHtml(log.scheduled_at_bogota)}</span>`);
          }
          if (log.actual_execution_at_bogota) {
            traceParts.push(`<span>Ejecución real: ${escapeHtml(log.actual_execution_at_bogota)}</span>`);
          }
          if (log.details?.duration_ms != null) {
            traceParts.push(`<span>Duración IA: ${escapeHtml(log.details.duration_ms)} ms</span>`);
          }

          return `
            <div class="timeline-event">
              <div class="timeline-event-header">
                <div>
                  <span class="event-type-tag">${escapeHtml(log.event_type)}</span>
                  <span class="event-status-tag ${escapeHtml(log.status)}">${escapeHtml(log.status)}</span>
                </div>
                <span class="event-timestamp">${escapeHtml(log.created_at_display || log.created_at_bogota || "")}</span>
              </div>
              <p class="event-description">${escapeHtml(log.description)}</p>
              ${
                traceParts.length > 0
                  ? `<div class="event-trace-times">${traceParts.join("")}</div>`
                  : ""
              }
            </div>
          `;
        })
        .join("");
    }

    const prevBtn = el("btnLogsPrev");
    const nextBtn = el("btnLogsNext");
    const infoEl = el("logsPageInfo");
    if (prevBtn) prevBtn.disabled = (pagination?.page || 1) <= 1;
    if (nextBtn) {
      nextBtn.disabled = (pagination?.page || 1) >= (pagination?.total_pages || 1);
    }
    if (infoEl) {
      infoEl.textContent = `Página ${pagination?.page || 1} de ${Math.max(1, pagination?.total_pages || 1)} (${pagination?.total || 0} eventos)`;
    }
  }

  // ==========================================================================
  // EVENTOS E INTERACCIÓN DEL USUARIO
  // ==========================================================================
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
        if (btnText) btnText.textContent = "Solicitando generación inicial a Ollama Cloud...";

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
              data?.message || "No fue posible iniciar el ciclo de creación literaria.",
              "error"
            );
          } else {
            showAlert(
              `Ciclo iniciado con éxito: "${data.poem?.title || "Nueva composición"}".`,
              "success"
            );
            state.inspectedPoemId = data.poem?.id || null;
            if (el("seedThemeInput")) el("seedThemeInput").value = "";
          }
        } catch (err) {
          showAlert(`Error de red al intentar iniciar el ciclo: ${err}`, "error");
        } finally {
          await refreshAllDashboard();
        }
      });
    }

    const btnRefresh = el("btnManualRefresh");
    if (btnRefresh) {
      btnRefresh.addEventListener("click", () => {
        refreshAllDashboard();
      });
    }

    const btnRestoreCurrent = el("btnRestoreCurrentPoem");
    if (btnRestoreCurrent) {
      btnRestoreCurrent.addEventListener("click", () => {
        state.inspectedPoemId = state.currentPoem?.id || null;
        refreshCurrentPoem();
      });
    }

    const selectA = el("selectVersionA");
    const selectB = el("selectVersionB");
    if (selectA) {
      selectA.addEventListener("change", () =>
        updateComparisonFromSelects(state.inspectedVersions)
      );
    }
    if (selectB) {
      selectB.addEventListener("change", () =>
        updateComparisonFromSelects(state.inspectedVersions)
      );
    }

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

    // Reloj suave local para avanzar tiempo transcurrido/restante entre polls de 10s
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
