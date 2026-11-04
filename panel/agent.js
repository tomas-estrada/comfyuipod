/*
 * Modo Agente (prototipo) — consume las APIs reales de ComfyUIPod Studio:
 *   sesiones   GET/POST /studio/api/projects
 *   chat       GET/POST /studio/api/projects/{id}/chat (SSE con stream:true)
 *   planes     POST /studio/api/productions, GET/PATCH /studio/api/productions/{id}[/plan]
 *   aprobación POST /studio/api/productions/{id}/approve   (puerta humana, solo API pública)
 *   cancel     POST /studio/api/productions/{id}/cancel
 *   progreso   SSE /studio/api/progress (eventos production_*, tool_call, job, ...)
 *               + polling GET /studio/api/productions/{id}/status (checkpoint por paso)
 *   resultados assets de galería vía /studio/api/view?project_id=&asset_id=
 * Sin build, sin CDNs, sin estilos inline (CSP script-src/style-src 'self').
 */
(() => {
    "use strict";

    const $ = (sel) => document.querySelector(sel);

    const TERMINAL_PHASES = new Set(["done", "failed", "cancelled", "halted"]);
    const ACTIVE_PHASES = new Set(["planning", "executing", "evaluating", "assembling"]);

    const state = {
        projectId: null,
        projects: [],
        productions: [],
        currentProductionId: null,
        planOriginal: null,
        sending: false,
        pollTimer: null,
        eventSource: null,
        streamAssistantEl: null,
    };

    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[ch]));
    }

    function toast(message, isError) {
        const el = $("#agent-toast");
        el.textContent = message;
        el.hidden = false;
        el.classList.toggle("agent-toast-error", Boolean(isError));
        window.clearTimeout(toast._t);
        toast._t = window.setTimeout(() => { el.hidden = true; }, 6000);
    }

    async function apiFetch(path, options) {
        const opts = options || {};
        const resp = await fetch(path, opts);
        if (!resp.ok) {
            let detail = resp.statusText;
            try {
                const body = await resp.json();
                detail = body.detail || detail;
            } catch (_) { /* cuerpo no JSON */ }
            throw new Error(detail);
        }
        if (resp.status === 204) return null;
        return resp.json();
    }

    function phaseChipClass(phase) {
        return "phase-chip ph-" + String(phase || "unknown").replace(/[^a-z_]/g, "_");
    }

    function shotChipClass(status) {
        return "shot-chip st-" + String(status || "unknown").replace(/[^a-z_]/g, "_");
    }

    // ---------------------------------------------------------------- estado
    async function loadSystemStatus() {
        try {
            const health = await apiFetch("/studio/api/health");
            $("#agent-health-server").textContent = "Servidor: " + (health.server || "ok");
            $("#agent-health-server").className = "status-pill " + (health.server === "ok" ? "status-ok" : "status-neutral");
            $("#agent-health-comfyui").textContent = "ComfyUI: " + (health.comfyui || "—");
            $("#agent-health-comfyui").className = "status-pill " + (health.comfyui === "ok" ? "status-ok" : "status-neutral");
            $("#agent-health-llm").textContent = "Agente LLM: " + (health.llm_model || "—") + (health.stub_llm ? " (stub)" : "");
        } catch (err) {
            $("#agent-health-server").textContent = "Servidor: Error";
            $("#agent-health-server").className = "status-pill status-danger";
        }
        try {
            const status = await apiFetch("/studio/api/status");
            const llm = status.llm || {};
            const model = llm.model || "—";
            const fallback = llm.fallback_model ? " · fallback: " + llm.fallback_model : "";
            const stub = llm.stub ? " · modo stub (sin LLM real)" : "";
            $("#agent-model-info").textContent = "modelo: " + model + fallback + stub;
        } catch (_) { /* el modelo es informativo */ }
    }

    // ---------------------------------------------------------------- sesiones
    async function loadProjects(selectId) {
        const data = await apiFetch("/studio/api/projects");
        state.projects = data.projects || [];
        const list = $("#agent-session-list");
        list.textContent = "";
        if (!state.projects.length) {
            const li = document.createElement("li");
            li.className = "agent-empty";
            li.textContent = "Sin sesiones. Crea la primera arriba.";
            list.appendChild(li);
            return;
        }
        for (const p of state.projects) {
            const li = document.createElement("li");
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "agent-session-item";
            btn.setAttribute("role", "option");
            btn.setAttribute("aria-selected", String(p.id === state.projectId));
            const name = document.createElement("span");
            name.className = "agent-session-name";
            name.textContent = p.name;
            const meta = document.createElement("span");
            meta.className = "agent-item-meta";
            meta.textContent = p.aspect_ratio || "16:9";
            btn.appendChild(name);
            btn.appendChild(meta);
            btn.addEventListener("click", () => selectSession(p.id));
            li.appendChild(btn);
            list.appendChild(li);
        }
        if (selectId) selectSession(selectId);
        else if (!state.projectId && state.projects.length) selectSession(state.projects[0].id);
    }

    async function selectSession(projectId) {
        state.projectId = projectId;
        const project = state.projects.find((p) => p.id === projectId);
        $("#agent-session-badge").textContent = "Sesión: " + (project ? project.name : projectId);
        const list = $("#agent-session-list");
        list.querySelectorAll(".agent-session-item").forEach((btn, i) => {
            btn.setAttribute("aria-selected", String(state.projects[i] && state.projects[i].id === projectId));
        });
        await Promise.all([loadChatHistory(), loadProductions()]);
    }

    // ---------------------------------------------------------------- chat
    function clearTranscriptPlaceholder() {
        const t = $("#agent-transcript");
        if (t.querySelector(".agent-empty")) t.textContent = "";
    }

    function addMessage(role, text) {
        clearTranscriptPlaceholder();
        const t = $("#agent-transcript");
        const div = document.createElement("div");
        div.className = "msg msg-" + role;
        const roleEl = document.createElement("span");
        roleEl.className = "msg-role";
        roleEl.textContent = role === "user" ? "Tú" : "Agente";
        const body = document.createElement("span");
        body.textContent = text || "";
        div.appendChild(roleEl);
        div.appendChild(body);
        t.appendChild(div);
        t.scrollTop = t.scrollHeight;
        return body;
    }

    function addToolRow(name, detailText, isError) {
        clearTranscriptPlaceholder();
        const t = $("#agent-transcript");
        const div = document.createElement("div");
        div.className = "msg-tool" + (isError ? " tool-error" : "");
        const summary = document.createElement("summary");
        summary.textContent = "🛠 " + name + (isError ? " (error)" : "");
        const pre = document.createElement("span");
        pre.textContent = detailText;
        div.appendChild(summary);
        div.appendChild(pre);
        t.appendChild(div);
        t.scrollTop = t.scrollHeight;
    }

    async function loadChatHistory() {
        if (!state.projectId) return;
        const t = $("#agent-transcript");
        t.textContent = "";
        try {
            const data = await apiFetch(
                "/studio/api/projects/" + encodeURIComponent(state.projectId) + "/chat?limit=200"
            );
            const messages = data.messages || [];
            if (!messages.length) {
                const p = document.createElement("p");
                p.className = "agent-empty";
                p.textContent = "Sin mensajes aún. Describe una tarea audiovisual abajo.";
                t.appendChild(p);
                return;
            }
            for (const m of messages) {
                if (m.role === "user" || m.role === "assistant") {
                    addMessage(m.role, m.content || "");
                } else if (m.role === "tool" && m.tool_calls && m.tool_calls.length) {
                    for (const tc of m.tool_calls) {
                        addToolRow(tc.name || "tool", m.content || "", Boolean(tc.is_error));
                    }
                }
            }
        } catch (err) {
            toast("No se pudo cargar el historial: " + err.message, true);
        }
    }

    function parseSseChunk(buffer, onEvent) {
        // devuelve el resto no procesado; un evento termina en línea en blanco
        let rest = buffer;
        let idx;
        while ((idx = rest.indexOf("\n\n")) !== -1) {
            const raw = rest.slice(0, idx);
            rest = rest.slice(idx + 2);
            let eventType = "message";
            const dataLines = [];
            for (const line of raw.split("\n")) {
                if (line.startsWith("event:")) eventType = line.slice(6).trim();
                else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
            }
            if (dataLines.length) {
                try { onEvent(eventType, JSON.parse(dataLines.join("\n"))); }
                catch (_) { /* frame malformado: se ignora */ }
            }
        }
        return rest;
    }

    async function sendMessage(message) {
        if (!state.projectId || state.sending) return;
        state.sending = true;
        $("#agent-send-btn").disabled = true;
        addMessage("user", message);
        const assistantBody = addMessage("assistant", "…");
        state.streamAssistantEl = assistantBody;
        let buffer = "";
        try {
            const resp = await fetch(
                "/studio/api/projects/" + encodeURIComponent(state.projectId) + "/chat",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
                    body: JSON.stringify({ message: message, stream: true }),
                }
            );
            if (!resp.ok) {
                const detail = await resp.text();
                throw new Error(detail || resp.statusText);
            }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                buffer = parseSseChunk(buffer, (type, data) => handleChatEvent(type, data));
            }
        } catch (err) {
            assistantBody.textContent = "Error al enviar: " + err.message;
            toast("Error en el turno de chat: " + err.message, true);
        } finally {
            state.sending = false;
            state.streamAssistantEl = null;
            $("#agent-send-btn").disabled = false;
            $("#agent-composer-input").focus();
            loadProductions();
        }
    }

    function handleChatEvent(type, data) {
        if (data.project_id && data.project_id !== state.projectId) return;
        if (type === "token" && state.streamAssistantEl) {
            state.streamAssistantEl.textContent += data.delta || "";
            const t = $("#agent-transcript");
            t.scrollTop = t.scrollHeight;
        } else if (type === "tool_call") {
            addToolRow(data.name || "tool", JSON.stringify(data.arguments || {}), false);
            addActivity("tool_call", (data.name || "") + " → " + JSON.stringify(data.arguments || {}).slice(0, 140));
        } else if (type === "tool_result") {
            addToolRow(data.name || "tool", JSON.stringify(data.result || {}).slice(0, 400), Boolean(data.is_error));
            addActivity("tool_result", (data.name || "") + (data.is_error ? " (error)" : " ok"));
        } else if (type === "done") {
            if (state.streamAssistantEl) state.streamAssistantEl.textContent = data.reply || "";
            addActivity("done", "turno terminado (" + (data.stop_reason || "") + ", modelo " + (data.model || "?") + ")");
        } else if (type === "error") {
            toast("Error del agente: " + (data.detail || "desconocido"), true);
        }
    }

    // ---------------------------------------------------------------- SSE global
    function connectSse() {
        if (state.eventSource) state.eventSource.close();
        const es = new EventSource("/studio/api/progress");
        state.eventSource = es;
        const badge = $("#agent-sse-badge");
        const mark = (ok) => {
            badge.textContent = ok ? "SSE: Conectado" : "SSE: Reconectando…";
            badge.className = "status-pill " + (ok ? "status-ok" : "status-neutral");
        };
        es.onopen = () => mark(true);
        es.onerror = () => mark(false);
        for (const type of [
            "heartbeat", "job", "progress", "token", "tool_call", "tool_result", "done",
            "production_planned", "production_plan_updated", "production_approved",
            "production_step", "production_cancelled", "production_cancel_requested",
            "production_finished", "error",
        ]) {
            es.addEventListener(type, (ev) => {
                let data = {};
                try { data = JSON.parse(ev.data); } catch (_) { /* sin datos */ }
                handleBrokerEvent(type, data);
            });
        }
    }

    function handleBrokerEvent(type, data) {
        if (type === "heartbeat") return;
        if (type === "token" || type === "tool_call" || type === "tool_result" || type === "done") {
            handleChatEvent(type, data);
            return;
        }
        if (type === "job" || type === "progress") {
            if (!data.project_id || data.project_id === state.projectId) {
                addActivity(type, (data.job_id || "") + " " + (data.status || data.node || ""));
            }
            return;
        }
        if (type.startsWith("production_")) {
            addActivity(type, (data.production_id || "") + " fase=" + (data.phase || ""));
            if (data.project_id && data.project_id !== state.projectId && type !== "production_step") {
                // producción de otra sesión: solo refrescamos la lista si no hay sesión
            }
            loadProductions();
            if (data.production_id && data.production_id === state.currentProductionId) {
                refreshProductionView();
            }
        } else if (type === "error") {
            toast("Evento de error: " + (data.detail || ""), true);
        }
    }

    // ---------------------------------------------------------------- actividad
    function addActivity(type, detail) {
        const feed = $("#agent-activity-feed");
        const li = document.createElement("li");
        const t = document.createElement("span");
        t.className = "activity-type";
        t.textContent = type;
        const d = document.createElement("span");
        d.className = "activity-detail";
        d.textContent = String(detail || "").slice(0, 160);
        d.title = String(detail || "");
        li.appendChild(t);
        li.appendChild(d);
        feed.prepend(li);
        while (feed.children.length > 50) feed.removeChild(feed.lastChild);
    }

    // ---------------------------------------------------------------- producciones
    async function loadProductions() {
        if (!state.projectId) {
            $("#agent-production-list").textContent = "";
            return;
        }
        try {
            const data = await apiFetch(
                "/studio/api/productions?project_id=" + encodeURIComponent(state.projectId) + "&limit=100"
            );
            state.productions = data.productions || [];
            renderProductionList();
        } catch (err) {
            toast("No se pudieron listar las producciones: " + err.message, true);
        }
    }

    function renderProductionList() {
        const list = $("#agent-production-list");
        list.textContent = "";
        if (!state.productions.length) {
            const li = document.createElement("li");
            li.className = "agent-empty";
            li.textContent = "Sin producciones en esta sesión. Describe una tarea abajo.";
            list.appendChild(li);
            return;
        }
        for (const p of state.productions) {
            const li = document.createElement("li");
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "agent-production-item";
            btn.setAttribute("aria-selected", String(p.production_id === state.currentProductionId));
            const idea = document.createElement("span");
            idea.className = "agent-production-idea";
            idea.textContent = p.idea || p.production_id;
            idea.title = p.idea || "";
            const meta = document.createElement("span");
            meta.className = "agent-item-meta";
            const chip = document.createElement("span");
            chip.className = phaseChipClass(p.phase);
            chip.textContent = p.phase;
            const shots = document.createElement("span");
            shots.textContent = (p.shots_accepted || 0) + "/" + (p.shots_total || 0) + " planos";
            meta.appendChild(chip);
            meta.appendChild(shots);
            btn.appendChild(idea);
            btn.appendChild(meta);
            btn.addEventListener("click", () => openProduction(p.production_id));
            li.appendChild(btn);
            list.appendChild(li);
        }
    }

    async function openProduction(productionId) {
        state.currentProductionId = productionId;
        renderProductionList();
        await refreshProductionView();
    }

    async function refreshProductionView() {
        if (!state.currentProductionId) return;
        try {
            const planData = await apiFetch("/studio/api/productions/" + encodeURIComponent(state.currentProductionId));
            const status = await apiFetch("/studio/api/productions/" + encodeURIComponent(state.currentProductionId) + "/status");
            renderProductionPanel(planData.plan, status);
            managePolling(status);
        } catch (err) {
            toast("No se pudo leer la producción: " + err.message, true);
        }
    }

    function managePolling(status) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        if (status && (ACTIVE_PHASES.has(status.phase) || status.cancel_requested)) {
            state.pollTimer = window.setInterval(() => {
                apiFetch("/studio/api/productions/" + encodeURIComponent(state.currentProductionId) + "/status")
                    .then((s) => {
                        renderProductionPanel(null, s);
                        if (TERMINAL_PHASES.has(s.phase) && !s.cancel_requested) managePolling(s);
                        else managePolling(s);
                    })
                    .catch(() => { /* reintento en el siguiente ciclo; SSE sigue activo */ });
            }, 1500);
        }
    }

    function renderProductionPanel(plan, status) {
        const container = $("#agent-plan-container");
        container.textContent = "";
        if (!state.currentProductionId) return;
        const card = document.createElement("div");
        card.className = "plan-card";

        const head = document.createElement("div");
        head.className = "plan-head";
        const title = document.createElement("strong");
        title.textContent = "Producción " + state.currentProductionId;
        const chip = document.createElement("span");
        chip.className = phaseChipClass(status ? status.phase : (plan ? plan.phase : ""));
        chip.textContent = status ? status.phase : (plan ? plan.phase : "");
        head.appendChild(title);
        head.appendChild(chip);
        card.appendChild(head);

        if (plan && plan.phase === "awaiting_approval") {
            state.planOriginal = plan;
            card.appendChild(renderPlanEditor(plan));
        } else if (plan) {
            const info = document.createElement("p");
            info.className = "agent-hint";
            info.textContent = "Idea: " + plan.idea;
            card.appendChild(info);
        }

        if (status) card.appendChild(renderExecutionStatus(status, plan));
        container.appendChild(card);
    }

    function renderPlanEditor(plan) {
        const wrap = document.createElement("div");

        const ideaLabel = document.createElement("label");
        ideaLabel.setAttribute("for", "plan-idea-edit");
        ideaLabel.textContent = "Idea (editable)";
        const ideaInput = document.createElement("input");
        ideaInput.type = "text";
        ideaInput.id = "plan-idea-edit";
        ideaInput.className = "plan-idea-input";
        ideaInput.maxLength = 4000;
        ideaInput.value = plan.idea || "";
        wrap.appendChild(ideaLabel);
        wrap.appendChild(ideaInput);

        plan.sequences.forEach((seq) => {
            const h = document.createElement("p");
            h.className = "plan-seq-title";
            h.textContent = seq.title || "Secuencia";
            wrap.appendChild(h);
            for (const shot of seq.shots) {
                const row = document.createElement("div");
                row.className = "plan-shot-row";

                const desc = document.createElement("input");
                desc.type = "text";
                desc.value = shot.description || "";
                desc.maxLength = 500;
                desc.setAttribute("data-shot-id", shot.id);
                desc.setAttribute("data-field", "description");
                desc.setAttribute("aria-label", "Descripción del plano " + shot.id);
                desc.dataset.original = shot.description || "";

                const dur = document.createElement("input");
                dur.type = "number";
                dur.min = "1";
                dur.max = "120";
                dur.step = "0.5";
                dur.value = String(shot.duration_s || 5);
                dur.setAttribute("data-shot-id", shot.id);
                dur.setAttribute("data-field", "duration_s");
                dur.setAttribute("aria-label", "Duración en segundos del plano " + shot.id);
                dur.dataset.original = String(shot.duration_s || 5);

                const removeLabel = document.createElement("label");
                removeLabel.className = "plan-shot-remove";
                const remove = document.createElement("input");
                remove.type = "checkbox";
                remove.setAttribute("data-shot-id", shot.id);
                remove.setAttribute("data-field", "remove");
                remove.setAttribute("aria-label", "Eliminar el plano " + shot.id);
                removeLabel.appendChild(remove);
                removeLabel.appendChild(document.createTextNode("eliminar"));

                row.appendChild(desc);
                row.appendChild(dur);
                row.appendChild(removeLabel);
                wrap.appendChild(row);
            }
        });

        const actions = document.createElement("div");
        actions.className = "plan-actions";

        const saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "btn btn-secondary";
        saveBtn.textContent = "Guardar cambios del plan";
        saveBtn.addEventListener("click", savePlanEdits);

        const approveBtn = document.createElement("button");
        approveBtn.type = "button";
        approveBtn.className = "btn btn-primary";
        approveBtn.textContent = "Aprobar y ejecutar";
        approveBtn.addEventListener("click", () => confirmApprove(plan));

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "Descartar producción";
        cancelBtn.addEventListener("click", () => confirmCancel(state.currentProductionId));

        actions.appendChild(saveBtn);
        actions.appendChild(approveBtn);
        actions.appendChild(cancelBtn);
        wrap.appendChild(actions);

        const note = document.createElement("p");
        note.className = "agent-hint";
        note.textContent = "La aprobación es la puerta humana: hasta que no la confirmes no corre ningún render (no se gasta GPU).";
        wrap.appendChild(note);

        return wrap;
    }

    async function savePlanEdits() {
        if (!state.currentProductionId || !state.planOriginal) return;
        const ideaInput = $("#plan-idea-edit");
        const edits = [];
        document.querySelectorAll("#agent-plan-container [data-shot-id]").forEach((el) => {
            const id = el.getAttribute("data-shot-id");
            const field = el.getAttribute("data-field");
            if (field === "remove") {
                if (el.checked) edits.push({ id: id, remove: true });
            } else if (el.dataset.original !== undefined && el.value !== el.dataset.original) {
                edits.push({ id: id, [field]: field === "duration_s" ? Number(el.value) : el.value });
            }
        });
        const body = {};
        if (ideaInput && ideaInput.value !== state.planOriginal.idea) body.idea = ideaInput.value;
        if (edits.length) body.shots = edits;
        if (!body.idea && !body.shots) {
            toast("No hay cambios que guardar");
            return;
        }
        try {
            const resp = await apiFetch(
                "/studio/api/productions/" + encodeURIComponent(state.currentProductionId) + "/plan",
                { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
            );
            state.planOriginal = resp.plan;
            renderProductionPanel(resp.plan, null);
            toast("Plan actualizado");
        } catch (err) {
            toast("No se pudo guardar el plan: " + err.message, true);
        }
    }

    function confirmApprove(plan) {
        const shots = plan ? plan.sequences.reduce((n, s) => n + s.shots.length, 0) : 0;
        const cost = plan ? plan.cost_cap_usd || 0 : 0;
        openDialog(
            "Aprobar y ejecutar",
            "Vas a aprobar la producción " + state.currentProductionId + " con " + shots + " plano(s)" +
            (cost > 0 ? " y límite de costo " + cost + " USD" : "") +
            ".\n\nEsto ejecuta el lazo de producción (motores/plantillas registradas). ¿Continuar?",
            async () => {
                try {
                    await apiFetch(
                        "/studio/api/productions/" + encodeURIComponent(state.currentProductionId) + "/approve",
                        { method: "POST" }
                    );
                    toast("Producción aprobada: ejecutando…");
                    await refreshProductionView();
                } catch (err) {
                    toast("No se pudo aprobar: " + err.message, true);
                }
            }
        );
    }

    function confirmCancel(productionId) {
        openDialog(
            "Cancelar producción",
            "¿Cancelar " + productionId + "?\nSi está esperando aprobación queda cancelada de inmediato; si está ejecutando, el lazo se detiene en el siguiente paso (checkpoint).",
            async () => {
                try {
                    const status = await apiFetch(
                        "/studio/api/productions/" + encodeURIComponent(productionId) + "/cancel",
                        { method: "POST" }
                    );
                    toast("Cancelación registrada (fase: " + status.phase + ")");
                    await refreshProductionView();
                } catch (err) {
                    toast("No se pudo cancelar: " + err.message, true);
                }
            }
        );
    }

    function openDialog(title, body, onConfirm) {
        const dlg = $("#agent-dialog");
        $("#agent-dialog-title").textContent = title;
        $("#agent-dialog-body").textContent = body;
        const confirmBtn = $("#agent-dialog-confirm");
        const cancelBtn = $("#agent-dialog-cancel");
        confirmBtn.onclick = () => { dlg.close(); onConfirm(); };
        cancelBtn.onclick = () => dlg.close();
        dlg.showModal();
        confirmBtn.focus();
    }

    function renderExecutionStatus(status, plan) {
        const wrap = document.createElement("div");

        const phaseRow = document.createElement("div");
        phaseRow.className = "exec-phase";
        const chip = document.createElement("span");
        chip.className = phaseChipClass(status.phase);
        chip.textContent = status.phase + (status.cancel_requested && !TERMINAL_PHASES.has(status.phase) ? " (cancelando…)" : "");
        const meta = document.createElement("span");
        meta.className = "agent-item-meta";
        meta.textContent = (status.shots_accepted || 0) + "/" + (status.shots_total || 0) + " planos aceptados";
        phaseRow.appendChild(chip);
        phaseRow.appendChild(meta);
        wrap.appendChild(phaseRow);

        if (typeof status.shots_total === "number" && status.shots_total > 0) {
            const progress = document.createElement("progress");
            progress.className = "exec-progress";
            progress.max = status.shots_total;
            progress.value = status.shots_accepted || 0;
            progress.setAttribute("aria-label", "Progreso de planos aceptados");
            wrap.appendChild(progress);
        }

        if (plan && plan.sequences) {
            const chips = document.createElement("div");
            chips.className = "exec-shots";
            for (const seq of plan.sequences) {
                for (const shot of seq.shots) {
                    const c = document.createElement("span");
                    c.className = shotChipClass(shot.status);
                    c.textContent = shot.status;
                    c.title = shot.description + (shot.error ? " — " + shot.error : "");
                    chips.appendChild(c);
                }
            }
            wrap.appendChild(chips);
        }

        if (status.cancel_requested && !TERMINAL_PHASES.has(status.phase)) {
            const note = document.createElement("p");
            note.className = "agent-hint";
            note.textContent = "Cancelación solicitada: el lazo se detiene en el siguiente paso.";
            wrap.appendChild(note);
        }

        if (status.phase === "failed" || (plan && plan.log)) {
            const errors = (plan && plan.log ? plan.log : []).filter((e) => e.kind === "error" || e.kind === "failed" || e.kind === "gallery_error");
            if (status.phase === "failed" || errors.length) {
                const errBox = document.createElement("div");
                errBox.className = "plan-error";
                errBox.setAttribute("role", "alert");
                errBox.textContent = "Error: " + JSON.stringify(errors.slice(-3), null, 2).slice(0, 800);
                wrap.appendChild(errBox);
            }
        }

        const assetIds = status.output_asset_ids || [];
        if (assetIds.length) {
            const results = document.createElement("div");
            results.className = "plan-results";
            const h = document.createElement("strong");
            h.textContent = "Resultados";
            results.appendChild(h);
            for (const aid of assetIds) {
                const a = document.createElement("a");
                a.href = "/studio/api/view?project_id=" + encodeURIComponent(status.project_id || state.projectId || "") +
                    "&asset_id=" + encodeURIComponent(aid);
                a.textContent = "Ver artefacto " + aid;
                results.appendChild(a);
            }
            if (status.project_id || state.projectId) {
                const g = document.createElement("a");
                g.href = "/studio/?view=gallery";
                g.textContent = "Abrir galería del proyecto en el Studio →";
                results.appendChild(g);
            }
            wrap.appendChild(results);
        }

        if (!TERMINAL_PHASES.has(status.phase) && ACTIVE_PHASES.has(status.phase)) {
            const cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "btn btn-secondary btn-sm";
            cancelBtn.textContent = "Cancelar ejecución";
            cancelBtn.addEventListener("click", () => confirmCancel(state.currentProductionId));
            const row = document.createElement("div");
            row.className = "plan-actions";
            row.appendChild(cancelBtn);
            wrap.appendChild(row);
        }

        return wrap;
    }

    // ---------------------------------------------------------------- boot
    function wireForms() {
        $("#agent-new-session-form").addEventListener("submit", async (ev) => {
            ev.preventDefault();
            const name = $("#agent-session-name").value.trim();
            if (!name) return;
            try {
                const created = await apiFetch("/studio/api/projects", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: name }),
                });
                $("#agent-session-name").value = "";
                toast("Sesión creada: " + created.name);
                await loadProjects(created.id);
            } catch (err) {
                toast("No se pudo crear la sesión: " + err.message, true);
            }
        });

        $("#agent-refresh-projects").addEventListener("click", () => loadProjects());
        $("#agent-refresh-productions").addEventListener("click", () => loadProductions());
        $("#agent-clear-activity").addEventListener("click", () => { $("#agent-activity-feed").textContent = ""; });

        const composer = $("#agent-composer-form");
        const input = $("#agent-composer-input");
        composer.addEventListener("submit", (ev) => {
            ev.preventDefault();
            const message = input.value.trim();
            if (!message) return;
            input.value = "";
            sendMessage(message);
        });
        input.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                composer.requestSubmit();
            }
        });

        $("#agent-plan-form").addEventListener("submit", async (ev) => {
            ev.preventDefault();
            if (!state.projectId) {
                toast("Selecciona o crea una sesión primero", true);
                return;
            }
            const idea = $("#agent-plan-idea").value.trim();
            const cost = Number($("#agent-plan-cost").value || 0);
            if (!idea) return;
            const btn = $("#agent-plan-btn");
            btn.disabled = true;
            try {
                const resp = await apiFetch("/studio/api/productions", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ idea: idea, cost_cap_usd: cost, project_id: state.projectId }),
                });
                $("#agent-plan-idea").value = "";
                state.currentProductionId = resp.production_id;
                toast("Plan creado: esperando tu aprobación");
                await loadProductions();
                await refreshProductionView();
            } catch (err) {
                toast("No se pudo planificar: " + err.message, true);
            } finally {
                btn.disabled = false;
            }
        });
    }

    async function boot() {
        wireForms();
        connectSse();
        await loadSystemStatus();
        try {
            await loadProjects();
        } catch (err) {
            toast("No se pudieron listar las sesiones: " + err.message, true);
        }
    }

    document.addEventListener("DOMContentLoaded", boot);
})();
