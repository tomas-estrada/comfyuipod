/**
 * ComfyUIPod Studio — Panel Web Agéntico (Vanilla JS, sin build)
 * Consume /studio/api/* y SSE en /studio/api/progress + /studio/api/projects/{id}/chat
 */
(function () {
    "use strict";

    const AUTH_STORAGE_KEY = "comfy_studio_edge_auth_v1";

    const state = {
        activeView: "projects",
        projects: [],
        activeProjectId: null,
        activeProjectDetail: null,
        assets: [],
        galleryItems: [],
        selectedGalleryAssetId: null,
        templates: [],
        jobs: [],
        edlData: null,
        toolEvents: [],
        selectedKeyframeIds: new Set(),
        chatDomProjectId: null,
        chatDomEpoch: 0,
        selectProjectSeq: 0,
        pendingSelectProjectPromise: null,
        pendingChatByProject: {},
        sseSource: null,
        sseConnected: false,
        auth: loadSavedAuth(),
    };

    function loadSavedAuth() {
        try {
            const raw = sessionStorage.getItem(AUTH_STORAGE_KEY);
            if (!raw) {
                return { username: "admin", password: "", simulateEdge: false };
            }
            const parsed = JSON.parse(raw);
            return {
                username: parsed.username || "admin",
                password: parsed.password || "",
                simulateEdge: Boolean(parsed.simulateEdge),
            };
        } catch (_err) {
            return { username: "admin", password: "", simulateEdge: false };
        }
    }

    function saveAuthToStorage() {
        try {
            sessionStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(state.auth));
        } catch (_err) {
            // Ignorar si sessionStorage está restringido
        }
    }

    function buildAuthHeaders(extraHeaders) {
        const headers = Object.assign({}, extraHeaders || {});
        if (state.auth && state.auth.password) {
            const token = btoa(
                unescape(encodeURIComponent(`${state.auth.username}:${state.auth.password}`))
            );
            headers["Authorization"] = `Basic ${token}`;
        }
        if (state.auth && state.auth.simulateEdge) {
            headers["X-Studio-Simulate-Edge"] = "1";
        }
        return headers;
    }

    async function apiFetch(path, options) {
        const opts = Object.assign({}, options || {});
        opts.headers = buildAuthHeaders(opts.headers);
        const response = await fetch(path, opts);
        if (response.status === 401) {
            setToast(
                "Autenticación requerida (401). Configura Usuario/Contraseña en «Acceso / Login».",
                "error"
            );
            updateAuthBadge("Requiere Basic-Auth (401)", "status-err");
        }
        return response;
    }

    function setToast(message, level) {
        const el = document.getElementById("studio-toast");
        if (!el) return;
        el.textContent = message;
        el.className = "studio-toast";
        if (level === "error") {
            el.classList.add("toast-error");
        } else if (level === "success") {
            el.classList.add("toast-success");
        }
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function formatBytes(bytes) {
        const num = Number(bytes || 0);
        if (num < 1024) return `${num} B`;
        if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
        return `${(num / (1024 * 1024)).toFixed(2)} MB`;
    }

    function switchView(viewName) {
        const validViews = ["projects", "chat", "jobs", "gallery", "edl", "login"];
        const target = validViews.includes(viewName) ? viewName : "projects";
        state.activeView = target;

        document.querySelectorAll(".nav-tab[data-view]").forEach((btn) => {
            const isMatch = btn.getAttribute("data-view") === target;
            btn.classList.toggle("active", isMatch);
            btn.setAttribute("aria-selected", isMatch ? "true" : "false");
        });

        document.querySelectorAll(".studio-view[data-view-panel]").forEach((sec) => {
            const isMatch = sec.getAttribute("data-view-panel") === target;
            sec.classList.toggle("active", isMatch);
            if (isMatch) {
                sec.removeAttribute("hidden");
            } else {
                sec.setAttribute("hidden", "");
            }
        });

        if (target === "jobs") {
            refreshJobs();
        } else if (target === "gallery") {
            refreshGallery();
        } else if (target === "edl") {
            refreshEdl();
        } else if (target === "login") {
            refreshHealthAndStatus();
        }
    }

    function updateAuthBadge(text, statusClass) {
        const badge = document.getElementById("auth-mode-badge");
        if (!badge) return;
        badge.textContent = text;
        badge.className = `code-badge ${statusClass || ""}`.trim();
    }

    /* --- HEALTH & STATUS --- */
    async function refreshHealthAndStatus() {
        const serverBadge = document.getElementById("health-server-badge");
        const comfyBadge = document.getElementById("health-comfyui-badge");
        const llmBadge = document.getElementById("health-llm-badge");
        const authCard = document.getElementById("auth-status-card");

        try {
            const resp = await apiFetch("/studio/api/health");
            if (!resp.ok) {
                if (serverBadge) {
                    serverBadge.textContent = `Servidor: HTTP ${resp.status}`;
                    serverBadge.className = "status-pill status-err";
                }
                return;
            }
            const health = await resp.json();
            if (serverBadge) {
                serverBadge.textContent = `Servidor: ${health.server === "ok" ? "OK" : health.server}`;
                serverBadge.className = "status-pill status-ok";
            }
            if (comfyBadge) {
                const comfyOk = health.comfyui === "ok";
                comfyBadge.textContent = `ComfyUI: ${comfyOk ? "OK" : "Degradado"}`;
                comfyBadge.className = `status-pill ${comfyOk ? "status-ok" : "status-warn"}`;
            }
            if (llmBadge) {
                const mode = health.stub_llm ? "stub" : "live";
                llmBadge.textContent = `Agente: ${health.llm_model || "qwen3-vl-plus"} (${mode})`;
                llmBadge.className = "status-pill status-accent";
            }
            updateAuthBadge(
                state.auth.password ? `Basic-Auth (${state.auth.username})` : "Sesión Loopback / Edge OK",
                "status-ok"
            );

            if (authCard) {
                authCard.innerHTML = `
                    <p><strong>Estado del Servidor Studio:</strong> <code>${escapeHtml(health.status)}</code></p>
                    <p><strong>Motor ComfyUI:</strong> <code>${escapeHtml(health.comfyui)}</code> (${escapeHtml(health.comfyui_url)})</p>
                    <p><strong>Modelo LLM Principal:</strong> <code>${escapeHtml(health.llm_model)}</code> · Fallback: <code>${escapeHtml(health.llm_fallback_model)}</code></p>
                    <p><strong>Modo Stub LLM:</strong> <code>${health.stub_llm ? "Activo (sin llamadas externas)" : "Desactivado"}</code> · Clave: <code>${escapeHtml(health.llm_key_status)}</code></p>
                `;
            }
        } catch (err) {
            if (serverBadge) {
                serverBadge.textContent = "Servidor: Sin conexión";
                serverBadge.className = "status-pill status-err";
            }
            if (authCard) {
                authCard.innerHTML = `<p>Error al conectar con <code>/studio/api/health</code>: ${escapeHtml(err.message)}</p>`;
            }
        }
    }

    /* --- PROJECTS & ASSETS --- */
    async function refreshProjects() {
        try {
            const resp = await apiFetch("/studio/api/projects");
            if (!resp.ok) return;
            const data = await resp.json();
            state.projects = Array.isArray(data.projects) ? data.projects : [];

            if (!state.activeProjectId && state.projects.length > 0) {
                state.activeProjectId = state.projects[0].id;
            } else if (
                state.activeProjectId &&
                !state.projects.some((p) => p.id === state.activeProjectId)
            ) {
                state.selectedKeyframeIds.clear();
                state.activeProjectId = state.projects.length > 0 ? state.projects[0].id : null;
            }

            renderProjectsList();
            if (state.activeProjectId) {
                await selectProject(state.activeProjectId, false);
            } else {
                state.activeProjectDetail = null;
                state.assets = [];
                state.jobs = [];
                state.selectedKeyframeIds.clear();
                renderActiveProjectDetail();
                renderProjectAssets();
                renderKeyframesSelector();
                renderTemplateAssetOptions();
            }
        } catch (_err) {
            setToast("No se pudo cargar la lista de proyectos.", "error");
        }
    }

    function getPendingChatCount(projectId) {
        if (!projectId) return 0;
        return Number(state.pendingChatByProject[projectId] || 0);
    }

    function syncChatControlsState() {
        const sendBtn = document.getElementById("chat-send-btn");
        if (sendBtn) {
            sendBtn.disabled = getPendingChatCount(state.activeProjectId) > 0;
        }
    }

    function renderProjectsList() {
        const container = document.getElementById("projects-list");
        const activeBadge = document.getElementById("active-project-badge");
        if (!container) return;

        const activeProj = state.projects.find((p) => p.id === state.activeProjectId);
        if (activeBadge) {
            activeBadge.textContent = activeProj
                ? `Proyecto: ${activeProj.name}`
                : "Proyecto: Ninguno";
            activeBadge.setAttribute("data-active-project-id", activeProj ? activeProj.id : "");
        }

        if (state.projects.length === 0) {
            container.innerHTML =
                '<p class="empty-hint">No hay proyectos todavía. Crea el primero usando el formulario superior.</p>';
            return;
        }

        container.innerHTML = state.projects
            .map((proj) => {
                const isActive = proj.id === state.activeProjectId;
                return `
                <div class="item-card ${isActive ? "active-item" : ""}" data-project-card="${escapeHtml(proj.id)}">
                    <div class="item-main">
                        <span class="item-title">${escapeHtml(proj.name)}</span>
                        <span class="item-meta">
                            ID: <code>${escapeHtml(proj.id)}</code> · Formato: <strong>${escapeHtml(proj.aspect_ratio || "16:9")}</strong>
                            ${proj.description ? ` · ${escapeHtml(proj.description)}` : ""}
                        </span>
                    </div>
                    <div class="item-actions">
                        <button
                            type="button"
                            class="btn ${isActive ? "btn-accent" : "btn-primary"} btn-sm"
                            data-action="select-project"
                            data-project-id="${escapeHtml(proj.id)}"
                        >
                            ${isActive ? "Proyecto abierto" : "Abrir proyecto"}
                        </button>
                        <button
                            type="button"
                            class="btn btn-danger btn-sm"
                            data-action="delete-project"
                            data-project-id="${escapeHtml(proj.id)}"
                        >
                            Eliminar
                        </button>
                    </div>
                </div>
            `;
            })
            .join("");
    }

    async function ensureActiveProject() {
        if (state.activeProjectId) {
            return state.activeProjectId;
        }
        if (state.projects.length > 0) {
            await selectProject(state.projects[0].id, false);
            return state.activeProjectId;
        }
        const resp = await apiFetch("/studio/api/projects", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: "Proyecto Estudio Inicial",
                aspect_ratio: "16:9",
                description: "Proyecto creado automáticamente por el panel",
            }),
        });
        if (resp.ok) {
            const created = await resp.json();
            state.projects.unshift(created);
            await selectProject(created.id, false);
            renderProjectsList();
            return created.id;
        }
        return null;
    }

    async function selectProject(projectId, announce, options) {
        const opts = options || {};
        const listEl = document.getElementById("chat-messages-list");
        const statusEl = document.getElementById("chat-stream-status");
        const previousLoadedId = state.activeProjectDetail
            ? state.activeProjectDetail.id
            : state.activeProjectId;
        const isSwitchingProject =
            previousLoadedId !== projectId || state.activeProjectId !== projectId;

        const shouldPreserveChatDom = Boolean(
            opts.preserveChatDom &&
                !isSwitchingProject &&
                state.chatDomProjectId === projectId &&
                (opts.expectedDomEpoch == null || state.chatDomEpoch === opts.expectedDomEpoch) &&
                listEl &&
                listEl.getAttribute("data-chat-project-id") === projectId
        );

        if (isSwitchingProject) {
            state.selectedKeyframeIds.clear();
            state.toolEvents = [];
            renderToolEvents();
        }
        if (!shouldPreserveChatDom) {
            state.chatDomEpoch += 1;
            if (isSwitchingProject || state.chatDomProjectId !== projectId) {
                renderChatHistory([], projectId);
            }
            if (statusEl && getPendingChatCount(projectId) === 0) {
                statusEl.textContent = "Estado del turno: En espera de instrucción.";
            }
        }

        state.activeProjectId = projectId;
        renderProjectsList();
        syncChatControlsState();

        const reqSeq = ++state.selectProjectSeq;
        const runSelect = (async () => {
            try {
                const resp = await apiFetch(
                    `/studio/api/projects/${encodeURIComponent(projectId)}`
                );
                if (
                    !resp.ok ||
                    state.activeProjectId !== projectId ||
                    state.selectProjectSeq !== reqSeq
                ) {
                    return;
                }
                const detail = await resp.json();
                if (
                    state.activeProjectId !== projectId ||
                    state.selectProjectSeq !== reqSeq
                ) {
                    return;
                }
                state.activeProjectDetail = detail;
                state.assets = Array.isArray(detail.assets) ? detail.assets : [];
                state.jobs = Array.isArray(detail.jobs) ? detail.jobs : [];
                if (Array.isArray(detail.edl_versions)) {
                    state.edlData = {
                        project_id: projectId,
                        versions: detail.edl_versions,
                        latest: detail.edl_versions[0] || null,
                    };
                }

                const validImageAssetIds = new Set(
                    state.assets.filter((a) => a.kind === "image").map((a) => a.id)
                );
                for (const kfId of Array.from(state.selectedKeyframeIds)) {
                    if (!validImageAssetIds.has(kfId)) {
                        state.selectedKeyframeIds.delete(kfId);
                    }
                }

                renderActiveProjectDetail();
                renderProjectAssets();
                renderKeyframesSelector();
                renderTemplateAssetOptions();
                const canStillPreserveChatDom = Boolean(
                    shouldPreserveChatDom &&
                        state.chatDomProjectId === projectId &&
                        (opts.expectedDomEpoch == null ||
                            state.chatDomEpoch === opts.expectedDomEpoch) &&
                        listEl &&
                        listEl.getAttribute("data-chat-project-id") === projectId
                );
                if (!canStillPreserveChatDom) {
                    await refreshChatHistory(projectId);
                }
                if (
                    state.activeProjectId !== projectId ||
                    state.selectProjectSeq !== reqSeq
                ) {
                    return;
                }
                await refreshGallery();

                if (
                    announce &&
                    state.activeProjectId === projectId &&
                    state.selectProjectSeq === reqSeq
                ) {
                    setToast(`Proyecto activo: «${detail.name}» (${detail.id}).`, "success");
                }
            } catch (_err) {
                if (state.activeProjectId === projectId) {
                    setToast("Error al cargar el detalle del proyecto.", "error");
                }
            }
        })();

        state.pendingSelectProjectPromise = runSelect;
        await runSelect;
        if (state.pendingSelectProjectPromise === runSelect) {
            state.pendingSelectProjectPromise = null;
        }
    }

    function renderActiveProjectDetail() {
        const titleEl = document.getElementById("active-project-title");
        const codeEl = document.getElementById("active-project-id-code");
        const summaryEl = document.getElementById("active-project-summary");

        const detail = state.activeProjectDetail;
        if (!detail || !state.activeProjectId) {
            if (titleEl) titleEl.textContent = "Detalle del Proyecto Activo";
            if (codeEl) codeEl.textContent = "Sin proyecto";
            if (summaryEl) {
                summaryEl.innerHTML =
                    "<p>Crea o selecciona un proyecto en la columna izquierda para gestionar sus medios, abrir el chat del agente o lanzar renders.</p>";
            }
            return;
        }

        if (titleEl) titleEl.textContent = `Proyecto: ${detail.name}`;
        if (codeEl) codeEl.textContent = detail.id;

        const uploadsCount = state.assets.filter((a) => a.category !== "outputs").length;
        const outputsCount = state.assets.filter((a) => a.category === "outputs").length;
        const jobsCount = (detail.jobs || []).length;
        const edlCount = (detail.edl_versions || []).length;

        if (summaryEl) {
            summaryEl.innerHTML = `
                <p><strong>Nombre:</strong> ${escapeHtml(detail.name)} · <strong>Relación de aspecto:</strong> <code>${escapeHtml(detail.aspect_ratio || "16:9")}</code></p>
                <p><strong>Descripción:</strong> ${escapeHtml(detail.description || "Sin descripción")}</p>
                <p>
                    <strong>Resumen:</strong>
                    <span>${uploadsCount} medios subidos</span> ·
                    <span>${outputsCount} salidas en galería</span> ·
                    <span>${jobsCount} jobs</span> ·
                    <span>${edlCount} versiones EDL</span>
                </p>
            `;
        }
    }

    function renderProjectAssets() {
        const countEl = document.getElementById("project-assets-count");
        const listEl = document.getElementById("project-assets-list");
        if (countEl) countEl.textContent = String(state.assets.length);
        if (!listEl) return;

        if (state.assets.length === 0) {
            listEl.innerHTML =
                '<p class="empty-hint">Aún no hay medios en este proyecto. Sube un archivo arriba o genera un render.</p>';
            return;
        }

        listEl.innerHTML = state.assets
            .map((asset) => {
                const displayName = asset.original_filename || asset.filename;
                return `
                <div class="item-card" data-asset-card="${escapeHtml(asset.id)}">
                    <div class="item-main">
                        <span class="item-title">${escapeHtml(displayName)}</span>
                        <span class="item-meta">
                            Tipo: <code>${escapeHtml(asset.kind)}</code> ·
                            Categoría: <code>${escapeHtml(asset.category)}</code> ·
                            Tamaño: ${formatBytes(asset.size_bytes)} ·
                            v${escapeHtml(asset.version || 1)}
                        </span>
                    </div>
                    <div class="item-actions">
                        <button
                            type="button"
                            class="btn btn-accent btn-sm"
                            data-action="preview-in-gallery"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Previsualizar en Galería
                        </button>
                        <a
                            href="${escapeHtml(asset.download_url)}"
                            download="${escapeHtml(displayName)}"
                            class="btn btn-secondary btn-sm"
                            data-action="download-asset"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Descargar
                        </a>
                    </div>
                </div>
            `;
            })
            .join("");
    }

    function renderKeyframesSelector() {
        const container = document.getElementById("chat-keyframes-list");
        if (!container) return;

        const imageAssets = state.assets.filter((a) => a.kind === "image");
        if (imageAssets.length === 0) {
            container.innerHTML =
                '<span class="muted-text">No hay imágenes en el proyecto. Sube un PNG/JPG/WEBP para adjuntarlo como keyframe base64.</span>';
            return;
        }

        container.innerHTML = imageAssets
            .map((img) => {
                const checked = state.selectedKeyframeIds.has(img.id) ? "checked" : "";
                const label = img.original_filename || img.filename;
                return `
                <label class="checkbox-label">
                    <input
                        type="checkbox"
                        data-keyframe-checkbox="${escapeHtml(img.id)}"
                        ${checked}
                    >
                    <span>${escapeHtml(label)} (<code>${escapeHtml(img.id)}</code>)</span>
                </label>
            `;
            })
            .join("");
    }

    function renderTemplateAssetOptions() {
        const selectEl = document.getElementById("template-asset-select");
        const lastFrameSelectEl = document.getElementById("template-last-frame-select");

        const assetOptionsHtml = state.assets
            .map((asset) => {
                const label = `${asset.original_filename || asset.filename} (${asset.kind} · ${asset.id})`;
                return `<option value="${escapeHtml(asset.id)}">${escapeHtml(label)}</option>`;
            })
            .join("");

        if (selectEl) {
            const currentVal = selectEl.value;
            selectEl.innerHTML =
                '<option value="">— Sin asset de entrada (por defecto) —</option>' +
                assetOptionsHtml;
            if (currentVal && state.assets.some((a) => a.id === currentVal)) {
                selectEl.value = currentVal;
            }
        }

        if (lastFrameSelectEl) {
            const currentLastVal = lastFrameSelectEl.value;
            lastFrameSelectEl.innerHTML =
                '<option value="">— Sin último fotograma (por defecto) —</option>' +
                assetOptionsHtml;
            if (currentLastVal && state.assets.some((a) => a.id === currentLastVal)) {
                lastFrameSelectEl.value = currentLastVal;
            }
        }
    }

    /* --- CHAT & STREAMING SSE --- */
    async function refreshChatHistory(forProjectId) {
        const listEl = document.getElementById("chat-messages-list");
        const targetProjectId = forProjectId || state.activeProjectId;
        if (!listEl || !targetProjectId) return;
        const epochAtRequest = state.chatDomEpoch;

        try {
            const resp = await apiFetch(
                `/studio/api/projects/${encodeURIComponent(targetProjectId)}/chat`
            );
            if (!resp.ok) return;
            const data = await resp.json();
            if (
                state.activeProjectId !== targetProjectId ||
                state.chatDomEpoch !== epochAtRequest
            ) {
                return;
            }
            const messages = Array.isArray(data.messages) ? data.messages : [];
            renderChatHistory(messages, targetProjectId);
        } catch (_err) {
            // Mantener estado actual si falla lectura inicial
        }
    }

    function renderChatHistory(messages, forProjectId) {
        const listEl = document.getElementById("chat-messages-list");
        if (!listEl) return;
        const ownerProjectId = forProjectId || state.activeProjectId || "";
        if (ownerProjectId) {
            state.chatDomProjectId = ownerProjectId;
            listEl.setAttribute("data-chat-project-id", ownerProjectId);
        }

        if (!messages || messages.length === 0) {
            listEl.innerHTML = `
                <div class="chat-msg chat-msg-system" data-project-id="${escapeHtml(ownerProjectId)}">
                    <strong>Agente listo para el proyecto.</strong> Escribe una instrucción o usa uno de los botones rápidos para transmitir tokens SSE y eventos de herramientas en vivo.
                </div>
            `;
            return;
        }

        const visible = messages.filter((m) => m.role === "user" || m.role === "assistant");
        if (visible.length === 0) {
            listEl.innerHTML = `
                <div class="chat-msg chat-msg-system" data-project-id="${escapeHtml(ownerProjectId)}">
                    <strong>Agente listo.</strong> Envía un mensaje para iniciar la conversación.
                </div>
            `;
            return;
        }

        listEl.innerHTML = visible
            .map((msg) => {
                const isUser = msg.role === "user";
                const roleLabel = isUser ? "Tú (Director)" : "Agente Studio";
                const text =
                    typeof msg.content === "string"
                        ? msg.content
                        : JSON.stringify(msg.content);
                return `
                <div class="chat-msg ${isUser ? "chat-msg-user" : "chat-msg-assistant"}" data-project-id="${escapeHtml(ownerProjectId)}">
                    <span class="chat-msg-role">${escapeHtml(roleLabel)}</span>
                    <div class="chat-msg-text">${escapeHtml(text)}</div>
                </div>
            `;
            })
            .join("");
        listEl.scrollTop = listEl.scrollHeight;
    }

    function appendToolEvent(type, payload, forProjectId) {
        if (forProjectId && state.activeProjectId !== forProjectId) {
            return;
        }
        state.toolEvents.unshift({
            type,
            payload,
            timestamp: new Date().toLocaleTimeString("es-ES"),
        });
        renderToolEvents();
    }

    function renderToolEvents() {
        const countEl = document.getElementById("tool-events-count");
        const listEl = document.getElementById("chat-tool-events");
        if (countEl) {
            countEl.textContent = `${state.toolEvents.length} eventos`;
        }
        if (!listEl) return;

        if (state.toolEvents.length === 0) {
            listEl.innerHTML =
                '<p class="empty-hint">Aún no se han ejecutado herramientas en esta sesión.</p>';
            return;
        }

        listEl.innerHTML = state.toolEvents
            .map((evt) => {
                const isResult = evt.type === "tool_result";
                const p = evt.payload || {};
                const toolName = p.name || "tool";
                let detailsHtml = "";

                if (isResult) {
                    const resObj = p.result || {};
                    const jobId = resObj.job_id || "";
                    const status = resObj.status || "ok";
                    detailsHtml = `
                        <div><strong>Resultado:</strong> <code>${escapeHtml(status)}</code> ${
                        jobId ? `· Job: <code>${escapeHtml(jobId)}</code>` : ""
                    }</div>
                        <div class="item-meta"><code>${escapeHtml(
                            JSON.stringify(resObj).slice(0, 180)
                        )}</code></div>
                        <div class="item-actions" style="margin-top:0.35rem;">
                            <button type="button" class="btn btn-secondary btn-sm" data-goto-view="jobs">Ver Jobs</button>
                            <button type="button" class="btn btn-accent btn-sm" data-goto-view="gallery">Abrir en Galería</button>
                        </div>
                    `;
                } else {
                    const argsStr = JSON.stringify(p.arguments || {});
                    detailsHtml = `
                        <div><strong>Argumentos:</strong> <code>${escapeHtml(
                            argsStr.slice(0, 180)
                        )}</code></div>
                    `;
                }

                return `
                <div class="tool-event-card ${isResult ? "tool-result" : "tool-call"}" data-tool-event="${escapeHtml(evt.type)}">
                    <div>
                        <strong>${isResult ? "TOOL RESULT" : "TOOL CALL"}:</strong>
                        <code>${escapeHtml(toolName)}</code>
                        <span class="muted-text">(${escapeHtml(evt.timestamp)})</span>
                    </div>
                    ${detailsHtml}
                </div>
            `;
            })
            .join("");
    }

    async function sendChatMessage(messageText) {
        const cleanMsg = String(messageText || "").trim();
        if (!cleanMsg) return;

        let projectId = state.chatDomProjectId || state.activeProjectId;
        const capturedKeyframeIds = Array.from(state.selectedKeyframeIds);
        const pendingSelectAtSubmit = state.pendingSelectProjectPromise;

        if (projectId && !state.projects.some((p) => p.id === projectId)) {
            // Origen huérfano (proyecto eliminado): invalidar la propiedad de la
            // conversación para no publicar en un proyecto inexistente (404 silencioso)
            // y restaurar la recuperación de sin-proyecto.
            if (state.chatDomProjectId === projectId) {
                state.chatDomProjectId = null;
                state.chatDomEpoch += 1;
                const orphanListEl = document.getElementById("chat-messages-list");
                if (
                    orphanListEl &&
                    orphanListEl.getAttribute("data-chat-project-id") === projectId
                ) {
                    orphanListEl.removeAttribute("data-chat-project-id");
                    orphanListEl.innerHTML =
                        '<p class="empty-hint">Sin proyecto activo. Crea o selecciona un proyecto para chatear con el agente.</p>';
                }
            }
            if (state.activeProjectId === projectId) {
                state.activeProjectId = null;
                state.activeProjectDetail = null;
            }
            projectId = null;
        }

        if (!projectId) {
            if (pendingSelectAtSubmit) {
                await pendingSelectAtSubmit;
            }
            projectId = await ensureActiveProject();
            if (!projectId) {
                setToast("Crea un proyecto antes de enviar un mensaje al agente.", "error");
                return;
            }
        }

        const listEl = document.getElementById("chat-messages-list");
        const statusEl = document.getElementById("chat-stream-status");
        const streamCheckbox = document.getElementById("chat-stream-checkbox");
        const useStream = streamCheckbox ? streamCheckbox.checked : true;

        const userBubble = document.createElement("div");
        userBubble.className = "chat-msg chat-msg-user";
        userBubble.setAttribute("data-project-id", projectId);
        userBubble.innerHTML = `
            <span class="chat-msg-role">Tú (Director)</span>
            <div class="chat-msg-text">${escapeHtml(cleanMsg)}</div>
        `;

        const assistantBubble = document.createElement("div");
        assistantBubble.className = "chat-msg chat-msg-assistant";
        assistantBubble.setAttribute("data-live-stream", "true");
        assistantBubble.setAttribute("data-project-id", projectId);
        assistantBubble.innerHTML = `
            <span class="chat-msg-role">Agente Studio (Streaming SSE)</span>
            <div class="chat-msg-text" data-stream-tokens="">Pensando…</div>
        `;
        const tokenContainer = assistantBubble.querySelector("[data-stream-tokens]");

        let turnDomEpoch = state.chatDomEpoch;

        function mountTurnBubblesInDom(advanceEpoch) {
            if (!listEl || state.activeProjectId !== projectId) {
                return;
            }
            if (advanceEpoch) {
                state.chatDomEpoch += 1;
                turnDomEpoch = state.chatDomEpoch;
            }
            state.chatDomProjectId = projectId;
            listEl.setAttribute("data-chat-project-id", projectId);
            const sysPlaceholder = listEl.querySelector(".chat-msg-system");
            if (sysPlaceholder) {
                sysPlaceholder.remove();
            }
            if (!listEl.contains(userBubble)) {
                listEl.appendChild(userBubble);
            }
            if (!listEl.contains(assistantBubble)) {
                listEl.appendChild(assistantBubble);
            }
            listEl.scrollTop = listEl.scrollHeight;
        }

        function isOriginConversationDisplayed() {
            return Boolean(
                state.activeProjectId === projectId &&
                    state.chatDomProjectId === projectId &&
                    state.chatDomEpoch === turnDomEpoch &&
                    listEl &&
                    listEl.getAttribute("data-chat-project-id") === projectId &&
                    listEl.contains(assistantBubble)
            );
        }

        state.pendingChatByProject[projectId] = getPendingChatCount(projectId) + 1;
        syncChatControlsState();

        if (state.activeProjectId === projectId && state.chatDomProjectId === projectId) {
            mountTurnBubblesInDom(!pendingSelectAtSubmit);
            if (statusEl) {
                statusEl.textContent = "Transmitiendo turno SSE con el agente…";
            }
        }

        try {
            if (pendingSelectAtSubmit) {
                await pendingSelectAtSubmit;
            }
            while (state.activeProjectId === projectId && state.pendingSelectProjectPromise) {
                await state.pendingSelectProjectPromise;
            }

            if (state.activeProjectId === projectId) {
                if (state.chatDomProjectId !== projectId) {
                    await refreshChatHistory(projectId);
                }
                if (state.activeProjectId === projectId) {
                    if (!listEl || !listEl.contains(assistantBubble) || pendingSelectAtSubmit) {
                        mountTurnBubblesInDom(true);
                    }
                    if (statusEl && isOriginConversationDisplayed()) {
                        statusEl.textContent = "Transmitiendo turno SSE con el agente…";
                    }
                }
            }

            const projectAssets =
                state.activeProjectDetail && state.activeProjectDetail.id === projectId
                    ? state.assets
                    : [];
            const validProjectImageIds = new Set(
                projectAssets.filter((a) => a.kind === "image").map((a) => a.id)
            );
            const assetIds = capturedKeyframeIds.filter((id) =>
                validProjectImageIds.has(id)
            );

            const resp = await apiFetch(
                `/studio/api/projects/${encodeURIComponent(projectId)}/chat`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        message: cleanMsg,
                        asset_ids: assetIds,
                        stream: useStream,
                    }),
                }
            );

            if (!resp.ok) {
                const errText = await resp.text();
                if (isOriginConversationDisplayed()) {
                    if (tokenContainer) {
                        tokenContainer.textContent = `Error HTTP ${resp.status}: ${errText}`;
                    }
                    if (statusEl) {
                        statusEl.textContent = `Error en chat (HTTP ${resp.status}).`;
                    }
                }
                return;
            }

            if (!useStream) {
                const body = await resp.json();
                if (isOriginConversationDisplayed()) {
                    if (tokenContainer) {
                        tokenContainer.textContent = body.reply || "";
                    }
                    (body.tool_calls || []).forEach((tc) =>
                        appendToolEvent("tool_call", tc, projectId)
                    );
                    (body.tool_results || []).forEach((tr) =>
                        appendToolEvent("tool_result", tr, projectId)
                    );
                    if (statusEl) {
                        statusEl.textContent = `Turno completado: ${(body.tool_calls || []).length} herramientas invocadas.`;
                    }
                }
            } else {
                let accumulatedTokens = "";
                let tokenCount = 0;
                let toolCallCount = 0;
                let currentStreamIteration = null;

                const reader = resp.body.getReader();
                const decoder = new TextDecoder("utf-8");
                let buffer = "";

                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });

                    const blocks = buffer.split("\n\n");
                    buffer = blocks.pop() || "";

                    for (const block of blocks) {
                        const parsed = parseSseFrame(block);
                        if (!parsed) continue;
                        const evtName = parsed.event;
                        const payload = parsed.data;

                        if (evtName === "token") {
                            if (
                                payload.iteration &&
                                currentStreamIteration !== null &&
                                payload.iteration !== currentStreamIteration
                            ) {
                                currentStreamIteration = payload.iteration;
                                accumulatedTokens = "";
                            } else if (payload.iteration && currentStreamIteration === null) {
                                currentStreamIteration = payload.iteration;
                            }
                            const piece =
                                typeof payload.delta === "string"
                                    ? payload.delta
                                    : payload.text || "";
                            accumulatedTokens += piece;
                            tokenCount += 1;
                            if (isOriginConversationDisplayed()) {
                                if (tokenContainer) {
                                    tokenContainer.textContent = accumulatedTokens;
                                    tokenContainer.setAttribute(
                                        "data-stream-tokens",
                                        String(tokenCount)
                                    );
                                }
                                if (statusEl) {
                                    statusEl.textContent = `Streaming SSE en vivo: ${tokenCount} fragmentos de tokens recibidos…`;
                                }
                            }
                        } else if (evtName === "tool_call") {
                            toolCallCount += 1;
                            if (isOriginConversationDisplayed()) {
                                appendToolEvent("tool_call", payload, projectId);
                                if (statusEl) {
                                    statusEl.textContent = `Ejecutando herramienta: ${payload.name}…`;
                                }
                            }
                        } else if (evtName === "tool_result") {
                            if (isOriginConversationDisplayed()) {
                                appendToolEvent("tool_result", payload, projectId);
                                if (statusEl) {
                                    statusEl.textContent = `Resultado recibido de herramienta: ${payload.name}.`;
                                }
                            }
                        } else if (evtName === "done") {
                            if (isOriginConversationDisplayed()) {
                                if (tokenContainer) {
                                    tokenContainer.setAttribute(
                                        "data-text-before-done",
                                        tokenContainer.textContent || ""
                                    );
                                }
                                if (!accumulatedTokens && payload.reply && tokenContainer) {
                                    tokenContainer.textContent = payload.reply;
                                } else if (payload.reply && tokenContainer) {
                                    tokenContainer.textContent = payload.reply;
                                }
                                if (statusEl) {
                                    statusEl.textContent = `Turno completado (SSE): ${tokenCount} tokens y ${toolCallCount} llamadas a tools.`;
                                }
                            }
                        } else if (evtName === "error") {
                            if (isOriginConversationDisplayed() && tokenContainer) {
                                tokenContainer.textContent = `Error del agente: ${payload.detail || payload.error || "desconocido"}`;
                            }
                        }
                    }
                    if (listEl && isOriginConversationDisplayed()) {
                        listEl.scrollTop = listEl.scrollHeight;
                    }
                }
            }

            if (state.activeProjectId !== projectId) {
                if (state.activeProjectId && state.chatDomProjectId !== state.activeProjectId) {
                    await refreshChatHistory(state.activeProjectId);
                }
                return;
            }

            const preserveOriginDom = isOriginConversationDisplayed();
            setToast("Respuesta del agente completada.", "success");
            await selectProject(projectId, false, {
                preserveChatDom: preserveOriginDom,
                expectedDomEpoch: preserveOriginDom ? turnDomEpoch : null,
            });
        } catch (err) {
            if (isOriginConversationDisplayed()) {
                if (tokenContainer) {
                    tokenContainer.textContent = `Error de red en chat: ${err.message}`;
                }
                setToast(`Error en chat: ${err.message}`, "error");
            }
        } finally {
            const remaining = Math.max(0, getPendingChatCount(projectId) - 1);
            if (remaining === 0) {
                delete state.pendingChatByProject[projectId];
            } else {
                state.pendingChatByProject[projectId] = remaining;
            }
            syncChatControlsState();
        }
    }

    function parseSseFrame(rawBlock) {
        const lines = rawBlock.split("\n");
        let eventName = "message";
        const dataLines = [];
        for (const rawLine of lines) {
            const line = rawLine.trim();
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) {
                eventName = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trim());
            }
        }
        if (dataLines.length === 0) return null;
        try {
            return {
                event: eventName,
                data: JSON.parse(dataLines.join("\n")),
            };
        } catch (_err) {
            return null;
        }
    }

    /* --- TEMPLATES & JOBS --- */
    async function loadTemplates() {
        try {
            const resp = await apiFetch("/studio/api/templates");
            if (!resp.ok) return;
            const data = await resp.json();
            state.templates = Array.isArray(data.templates) ? data.templates : [];
            renderTemplateDoc();
        } catch (_err) {
            // Ignorar error inicial
        }
    }

    function renderTemplateDoc() {
        const selectEl = document.getElementById("template-select");
        const docCard = document.getElementById("template-doc-card");
        if (!selectEl || !docCard) return;

        const tplId = selectEl.value;
        const tpl = state.templates.find((t) => t.id === tplId);
        if (!tpl) return;

        const doc = tpl.documentation || {};
        const defaultsStr = JSON.stringify(doc.defaults || {});
        docCard.innerHTML = `
            <div><strong>${escapeHtml(tpl.title || tpl.id)}</strong> (<code>${escapeHtml(tpl.category || "video")}</code>)</div>
            <div style="margin-top:0.25rem;"><strong>Uso:</strong> ${escapeHtml(doc.uso || tpl.description || "")}</div>
            <div style="margin-top:0.25rem;"><strong>Defaults 480p/Lightning:</strong> <code>${escapeHtml(defaultsStr)}</code></div>
        `;
    }

    async function refreshJobs() {
        const targetProjectId = state.activeProjectId;
        try {
            const query = targetProjectId
                ? `?project_id=${encodeURIComponent(targetProjectId)}`
                : "";
            const resp = await apiFetch(`/studio/api/jobs${query}`);
            if (!resp.ok || state.activeProjectId !== targetProjectId) return;
            const data = await resp.json();
            if (state.activeProjectId !== targetProjectId) return;
            state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
            renderJobsList();
        } catch (_err) {
            // Ignorar error de red transitorio
        }
    }

    function renderJobsList() {
        const listEl = document.getElementById("jobs-list");
        if (!listEl) return;

        if (state.jobs.length === 0) {
            listEl.innerHTML =
                '<p class="empty-hint">No hay jobs en este proyecto todavía. Lanza un render con plantilla o pídelo en el Chat del Agente.</p>';
            return;
        }

        listEl.innerHTML = state.jobs
            .map((job) => {
                const val = Number(job.progress_value || 0);
                const max = Math.max(Number(job.progress_max || 1), 1);
                const pct =
                    job.status === "done"
                        ? 100
                        : Math.min(100, Math.round((val / max) * 100));
                const statusClass =
                    job.status === "done"
                        ? "status-ok"
                        : job.status === "error"
                        ? "status-err"
                        : "status-accent";

                return `
                <div class="item-card" data-job-card="${escapeHtml(job.id)}">
                    <div class="item-main" style="flex:1 1 280px;">
                        <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">
                            <span class="item-title">Job <code>${escapeHtml(job.id)}</code></span>
                            <span class="status-pill ${statusClass}">${escapeHtml(job.status)} (${pct}%)</span>
                            ${
                                job.template_id
                                    ? `<span class="code-badge">${escapeHtml(job.template_id)}</span>`
                                    : ""
                            }
                        </div>
                        <span class="item-meta">
                            Prompt ComfyUI: <code>${escapeHtml(job.prompt_id || "—")}</code> ·
                            Nodo: <code>${escapeHtml(job.current_node || "completado")}</code>
                        </span>
                        <div class="progress-track" aria-label="Progreso del job ${escapeHtml(job.id)}">
                            <div class="progress-fill" style="width: ${pct}%;"></div>
                        </div>
                    </div>
                    <div class="item-actions">
                        <button
                            type="button"
                            class="btn btn-accent btn-sm"
                            data-action="open-gallery"
                            data-job-id="${escapeHtml(job.id)}"
                        >
                            Ver salida en Galería →
                        </button>
                    </div>
                </div>
            `;
            })
            .join("");
    }

    async function submitTemplateJob(event) {
        event.preventDefault();
        const projectId = await ensureActiveProject();
        if (!projectId) return;

        const tplSelect = document.getElementById("template-select");
        const promptInput = document.getElementById("template-prompt-input");
        const stepsInput = document.getElementById("template-steps-input");
        const assetSelect = document.getElementById("template-asset-select");
        const lastFrameSelect = document.getElementById("template-last-frame-select");
        const submitBtn = document.getElementById("submit-template-btn");

        const templateId = tplSelect ? tplSelect.value : "wan_t2v";
        const promptText = promptInput ? promptInput.value.trim() : "Plano de estudio";
        const stepsVal = stepsInput ? Number(stepsInput.value || 4) : 4;
        const selectedAssetId = assetSelect ? assetSelect.value : "";
        const selectedLastFrameId = lastFrameSelect ? lastFrameSelect.value : "";

        const params = {
            prompt: promptText,
            steps: stepsVal,
        };
        if (selectedAssetId) {
            if (templateId === "vace_inpaint" || templateId === "vace_restyle") {
                params.video_asset_id = selectedAssetId;
            } else if (templateId === "wan_i2v") {
                params.image_asset_id = selectedAssetId;
            } else if (templateId === "wan_flf2v") {
                params.first_frame_asset_id = selectedAssetId;
            }
        }
        if (templateId === "wan_flf2v" && selectedLastFrameId) {
            params.last_frame_asset_id = selectedLastFrameId;
        }

        if (submitBtn) submitBtn.disabled = true;
        setToast(`Encolando render con plantilla ${templateId}…`, "");

        try {
            const resp = await apiFetch(
                `/studio/api/templates/${encodeURIComponent(templateId)}/submit`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        project_id: projectId,
                        params,
                    }),
                }
            );
            if (!resp.ok) {
                const errText = await resp.text();
                setToast(`Error al lanzar plantilla (${resp.status}): ${errText}`, "error");
                return;
            }
            const result = await resp.json();
            setToast(
                `Render completado: Job ${result.job_id} (${templateId}). Salida registrada en Galería.`,
                "success"
            );
            if (state.activeProjectId === projectId) {
                await selectProject(projectId, false);
                await refreshJobs();
                await refreshGallery();
            }
        } catch (err) {
            setToast(`Error al lanzar render: ${err.message}`, "error");
        } finally {
            if (submitBtn) submitBtn.disabled = false;
        }
    }

    /* --- GALLERY, PREVIEW & DOWNLOAD --- */
    async function refreshGallery() {
        const targetProjectId = state.activeProjectId;
        if (!targetProjectId) {
            state.galleryItems = [];
            renderGallery();
            return;
        }
        const filterEl = document.getElementById("gallery-category-filter");
        const category = filterEl ? filterEl.value : "all";

        try {
            const resp = await apiFetch(
                `/studio/api/projects/${encodeURIComponent(targetProjectId)}/gallery?category=${encodeURIComponent(category)}`
            );
            if (!resp.ok || state.activeProjectId !== targetProjectId) return;
            const data = await resp.json();
            if (state.activeProjectId !== targetProjectId) return;
            state.galleryItems = Array.isArray(data.assets) ? data.assets : [];

            if (
                state.galleryItems.length > 0 &&
                (!state.selectedGalleryAssetId ||
                    !state.galleryItems.some((a) => a.id === state.selectedGalleryAssetId))
            ) {
                state.selectedGalleryAssetId = state.galleryItems[0].id;
            }
            renderGallery();
        } catch (_err) {
            // Ignorar error transitorio
        }
    }

    function renderGallery() {
        const countEl = document.getElementById("gallery-items-count");
        const gridEl = document.getElementById("gallery-grid");
        if (countEl) countEl.textContent = String(state.galleryItems.length);
        if (!gridEl) return;

        renderGalleryPreviewStage();

        if (state.galleryItems.length === 0) {
            gridEl.innerHTML =
                '<p class="empty-hint">No hay elementos en esta vista de la galería. Genera un render o sube un medio en Proyectos.</p>';
            return;
        }

        gridEl.innerHTML = state.galleryItems
            .map((asset) => {
                const isSelected = asset.id === state.selectedGalleryAssetId;
                const displayName = asset.original_filename || asset.filename;
                const tplId = (asset.metadata && asset.metadata.template_id) || "";
                return `
                <div class="gallery-card ${isSelected ? "active-item" : ""}" data-gallery-card="${escapeHtml(asset.id)}">
                    <div>
                        <div style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;">
                            <span class="code-badge">${escapeHtml(asset.kind.toUpperCase())}</span>
                            <span class="code-badge">${escapeHtml(asset.category)}</span>
                        </div>
                        <div class="item-title" style="margin-top:0.45rem;">${escapeHtml(displayName)}</div>
                        <div class="item-meta" style="margin-top:0.2rem;">
                            ID: <code>${escapeHtml(asset.id)}</code> · ${formatBytes(asset.size_bytes)}
                            ${tplId ? ` · Plantilla: <code>${escapeHtml(tplId)}</code>` : ""}
                        </div>
                    </div>
                    <div class="item-actions">
                        <button
                            type="button"
                            class="btn btn-primary btn-sm"
                            data-action="preview-asset"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Previsualizar
                        </button>
                        <a
                            href="${escapeHtml(asset.download_url)}"
                            download="${escapeHtml(displayName)}"
                            class="btn btn-accent btn-sm"
                            data-action="download-asset"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Descargar archivo
                        </a>
                    </div>
                </div>
            `;
            })
            .join("");
    }

    function renderGalleryPreviewStage() {
        const stageEl = document.getElementById("gallery-preview-stage");
        if (!stageEl) return;

        const asset =
            state.galleryItems.find((a) => a.id === state.selectedGalleryAssetId) ||
            state.assets.find((a) => a.id === state.selectedGalleryAssetId);

        if (!asset) {
            stageEl.innerHTML = `
                <div class="preview-placeholder">
                    <p>Ningún elemento seleccionado para vista previa. Sube un medio o genera un render para previsualizar y descargar.</p>
                </div>
            `;
            return;
        }

        const displayName = asset.original_filename || asset.filename;
        const tplId = (asset.metadata && asset.metadata.template_id) || "—";
        let mediaPlayerHtml = "";

        if (asset.kind === "image") {
            mediaPlayerHtml = `
                <img
                    src="${escapeHtml(asset.view_url)}"
                    alt="Vista previa de ${escapeHtml(displayName)}"
                >
            `;
        } else if (asset.kind === "video") {
            mediaPlayerHtml = `
                <video
                    controls
                    preload="none"
                    data-preview-video="${escapeHtml(asset.id)}"
                >
                    <source src="${escapeHtml(asset.view_url)}" type="${escapeHtml(asset.mime_type || "video/mp4")}">
                </video>
                <div class="item-meta" style="margin-top:0.35rem;">
                    Contenedor de vídeo listo (<code>${escapeHtml(asset.mime_type || "video/mp4")}</code> · ${formatBytes(asset.size_bytes)})
                </div>
            `;
        } else {
            mediaPlayerHtml = `
                <audio controls preload="none" src="${escapeHtml(asset.view_url)}"></audio>
            `;
        }

        stageEl.innerHTML = `
            <div class="preview-stage-content" data-preview-active-id="${escapeHtml(asset.id)}">
                <div class="preview-media-box">
                    ${mediaPlayerHtml}
                </div>
                <div class="item-main">
                    <h3 style="margin:0 0 0.35rem;">Vista Previa: ${escapeHtml(displayName)}</h3>
                    <p class="item-meta">
                        <strong>Asset ID:</strong> <code>${escapeHtml(asset.id)}</code> ·
                        <strong>Categoría:</strong> <code>${escapeHtml(asset.category)}</code> ·
                        <strong>Tipo:</strong> <code>${escapeHtml(asset.kind)}</code>
                    </p>
                    <p class="item-meta">
                        <strong>Plantilla:</strong> <code>${escapeHtml(tplId)}</code> ·
                        <strong>Job ID:</strong> <code>${escapeHtml(asset.job_id || "subida directa")}</code> ·
                        <strong>Tamaño:</strong> ${formatBytes(asset.size_bytes)}
                    </p>
                    <p id="preview-http-verify" class="item-meta">
                        Endpoint de vista previa: <code>${escapeHtml(asset.view_url)}</code>
                    </p>
                    <div class="item-actions" style="margin-top:0.6rem;">
                        <a
                            id="stage-download-link"
                            href="${escapeHtml(asset.download_url)}"
                            download="${escapeHtml(displayName)}"
                            class="btn btn-primary"
                            data-action="download-asset"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Descargar archivo (${escapeHtml(displayName)})
                        </a>
                        <button
                            type="button"
                            id="stage-verify-download-btn"
                            class="btn btn-secondary"
                            data-action="verify-download"
                            data-asset-id="${escapeHtml(asset.id)}"
                        >
                            Verificar descarga HTTP
                        </button>
                    </div>
                </div>
            </div>
        `;
    }

    async function verifyAssetDownload(assetId) {
        const asset =
            state.galleryItems.find((a) => a.id === assetId) ||
            state.assets.find((a) => a.id === assetId);
        if (!asset) return;

        const bannerEl = document.getElementById("download-status-banner");
        const verifyEl = document.getElementById("preview-http-verify");
        try {
            const resp = await apiFetch(asset.download_url);
            if (!resp.ok) {
                if (bannerEl) {
                    bannerEl.textContent = `Error al verificar descarga HTTP ${resp.status}.`;
                }
                return;
            }
            const blob = await resp.blob();
            const disp = resp.headers.get("content-disposition") || "attachment";
            const msg = `Descarga lista y verificada: ${asset.original_filename || asset.filename} (${blob.size} bytes · ${disp})`;
            if (bannerEl) {
                bannerEl.textContent = msg;
            }
            if (verifyEl) {
                verifyEl.textContent = `Verificado HTTP 200 OK · ${blob.size} bytes recibidos.`;
            }
            setToast(msg, "success");
        } catch (err) {
            if (bannerEl) {
                bannerEl.textContent = `Error verificando descarga: ${err.message}`;
            }
        }
    }

    /* --- TIMELINE EDL --- */
    async function refreshEdl() {
        const targetProjectId = state.activeProjectId;
        if (!targetProjectId) {
            renderEdlView();
            return;
        }
        try {
            const resp = await apiFetch(
                `/studio/api/projects/${encodeURIComponent(targetProjectId)}/edl`
            );
            if (!resp.ok || state.activeProjectId !== targetProjectId) return;
            const edlData = await resp.json();
            if (state.activeProjectId !== targetProjectId) return;
            state.edlData = edlData;
            renderEdlView();
        } catch (_err) {
            if (state.activeProjectId === targetProjectId) {
                renderEdlView();
            }
        }
    }

    function renderEdlView() {
        const badgeEl = document.getElementById("edl-version-badge");
        const summaryEl = document.getElementById("edl-summary-box");
        const trackEl = document.getElementById("edl-timeline-track");
        const cutsListEl = document.getElementById("edl-cuts-list");

        const latest = state.edlData && (state.edlData.latest || state.edlData.edl);
        if (!latest) {
            if (badgeEl) badgeEl.textContent = "Sin versión EDL";
            if (trackEl) {
                trackEl.innerHTML =
                    '<span class="track-empty">Sin cortes definidos aún. Pulsa «Generar EDL de Highlights con Agente».</span>';
            }
            if (cutsListEl) {
                cutsListEl.innerHTML =
                    '<p class="empty-hint">No hay decisiones de corte registradas todavía en este proyecto.</p>';
            }
            return;
        }

        const edlPayload = latest.edl || {};
        const cuts = Array.isArray(edlPayload.cuts) ? edlPayload.cuts : [];
        if (badgeEl) {
            badgeEl.textContent = `EDL v${latest.version} · ${edlPayload.aspect_ratio || "16:9"}`;
        }
        if (summaryEl) {
            summaryEl.innerHTML = `
                <p><strong>Título EDL:</strong> ${escapeHtml(latest.title || edlPayload.title || "EDL")} (Versión <code>v${escapeHtml(latest.version)}</code>)</p>
                <p><strong>Duración estimada:</strong> <code>${escapeHtml(edlPayload.total_duration_sec || 0)}s</code> · <strong>Formato:</strong> <code>${escapeHtml(edlPayload.aspect_ratio || "16:9")}</code></p>
                <p><strong>Notas editoriales:</strong> ${escapeHtml(edlPayload.notes || "Sin notas adicionales")}</p>
            `;
        }

        if (trackEl) {
            if (cuts.length === 0) {
                trackEl.innerHTML = '<span class="track-empty">Versión EDL sin cortes.</span>';
            } else {
                trackEl.innerHTML = cuts
                    .map(
                        (cut, idx) => `
                        <span class="track-segment">
                            Corte #${idx + 1}: ${Number(cut.in_sec).toFixed(1)}s → ${Number(cut.out_sec).toFixed(1)}s
                            (${escapeHtml(cut.reason || "Highlight")})
                        </span>
                    `
                    )
                    .join("");
            }
        }

        if (cutsListEl) {
            cutsListEl.innerHTML = cuts
                .map(
                    (cut, idx) => `
                    <div class="item-card">
                        <div class="item-main">
                            <span class="item-title">Corte #${idx + 1}: ${Number(cut.in_sec).toFixed(2)}s – ${Number(cut.out_sec).toFixed(2)}s</span>
                            <span class="item-meta">
                                Duración: <code>${(Number(cut.out_sec) - Number(cut.in_sec)).toFixed(2)}s</code> ·
                                Hablante: <code>${escapeHtml(cut.speaker || "Locutor")}</code> ·
                                Motivo: ${escapeHtml(cut.reason || "Selección editorial")}
                            </span>
                        </div>
                    </div>
                `
                )
                .join("");
        }
    }

    /* --- SSE PROGRESS STREAM --- */
    function connectProgressSse() {
        if (typeof EventSource === "undefined") return;
        if (state.sseSource) {
            state.sseSource.close();
        }

        const badge = document.getElementById("sse-status-badge");
        const banner = document.getElementById("sse-live-banner");

        try {
            const es = new EventSource("/studio/api/progress");
            state.sseSource = es;

            es.onopen = function () {
                state.sseConnected = true;
                if (badge) {
                    badge.textContent = "SSE: Conectado";
                    badge.className = "status-pill status-ok";
                }
            };

            es.addEventListener("heartbeat", function (evt) {
                if (badge) {
                    badge.textContent = "SSE: Activo";
                    badge.className = "status-pill status-ok";
                }
                if (banner) {
                    try {
                        const data = JSON.parse(evt.data);
                        banner.innerHTML = `Canal SSE <code>/studio/api/progress</code> conectado (heartbeat #${escapeHtml(data.seq || 1)}).`;
                    } catch (_e) {
                        // Ignorar
                    }
                }
            });

            es.addEventListener("progress", function (evt) {
                try {
                    const data = JSON.parse(evt.data);
                    if (banner) {
                        banner.innerHTML = `Progreso en vivo — Job <code>${escapeHtml(data.job_id || "")}</code>: ${escapeHtml(data.value || 0)} / ${escapeHtml(data.max || 1)} (nodo <code>${escapeHtml(data.node || "exec")}</code>)`;
                    }
                    refreshJobs();
                } catch (_e) {
                    // Ignorar
                }
            });

            es.addEventListener("job", function (evt) {
                try {
                    const data = JSON.parse(evt.data);
                    if (banner) {
                        banner.innerHTML = `Actualización de Job <code>${escapeHtml(data.job_id || "")}</code>: estado <strong>${escapeHtml(data.status || "updated")}</strong>`;
                    }
                    refreshJobs();
                    if (data.status === "done") {
                        refreshGallery();
                    }
                } catch (_e) {
                    // Ignorar
                }
            });

            es.addEventListener("edl", function () {
                refreshEdl();
            });

            es.onerror = function () {
                state.sseConnected = false;
                if (badge) {
                    badge.textContent = "SSE: Espera / Reintento";
                    badge.className = "status-pill status-warn";
                }
            };
        } catch (_err) {
            if (badge) {
                badge.textContent = "SSE: No disponible";
            }
        }
    }

    /* --- EVENT LISTENERS & INITIALIZATION --- */
    function bindEvents() {
        // Navegación principal por pestañas
        document.querySelectorAll(".nav-tab[data-view]").forEach((btn) => {
            btn.addEventListener("click", () => {
                switchView(btn.getAttribute("data-view"));
            });
        });

        // Delegación global de clics para botones dinámicos y navegación interna
        document.addEventListener("click", async (event) => {
            const gotoBtn = event.target.closest("[data-goto-view]");
            if (gotoBtn) {
                const targetView = gotoBtn.getAttribute("data-goto-view");
                switchView(targetView);
                return;
            }

            const promptChip = event.target.closest("[data-prompt]");
            if (promptChip) {
                const text = promptChip.getAttribute("data-prompt") || "";
                const input = document.getElementById("chat-message-input");
                if (input) {
                    input.value = text;
                }
                await sendChatMessage(text);
                return;
            }

            const actionEl = event.target.closest("[data-action]");
            if (!actionEl) return;

            const action = actionEl.getAttribute("data-action");
            if (action === "select-project") {
                const pid = actionEl.getAttribute("data-project-id");
                if (pid) await selectProject(pid, true);
            } else if (action === "delete-project") {
                const pid = actionEl.getAttribute("data-project-id");
                if (pid) {
                    const resp = await apiFetch(
                        `/studio/api/projects/${encodeURIComponent(pid)}`,
                        { method: "DELETE" }
                    );
                    if (resp.ok) {
                        if (state.activeProjectId === pid) {
                            state.activeProjectId = null;
                            state.activeProjectDetail = null;
                        }
                        if (state.chatDomProjectId === pid) {
                            state.chatDomProjectId = null;
                            state.chatDomEpoch += 1;
                            const chatListEl =
                                document.getElementById("chat-messages-list");
                            if (
                                chatListEl &&
                                chatListEl.getAttribute("data-chat-project-id") === pid
                            ) {
                                chatListEl.removeAttribute("data-chat-project-id");
                                chatListEl.innerHTML =
                                    '<p class="empty-hint">Sin proyecto activo. Crea o selecciona un proyecto para chatear con el agente.</p>';
                            }
                        }
                        setToast(`Proyecto ${pid} eliminado.`, "success");
                        await refreshProjects();
                    }
                }
            } else if (action === "preview-in-gallery") {
                const aid = actionEl.getAttribute("data-asset-id");
                state.selectedGalleryAssetId = aid;
                const filterEl = document.getElementById("gallery-category-filter");
                if (filterEl) filterEl.value = "all";
                switchView("gallery");
                await verifyAssetDownload(aid);
            } else if (action === "preview-asset") {
                const aid = actionEl.getAttribute("data-asset-id");
                state.selectedGalleryAssetId = aid;
                renderGallery();
                await verifyAssetDownload(aid);
            } else if (action === "download-asset") {
                const aid = actionEl.getAttribute("data-asset-id");
                if (aid) {
                    verifyAssetDownload(aid);
                }
            } else if (action === "verify-download") {
                const aid = actionEl.getAttribute("data-asset-id");
                if (aid) {
                    await verifyAssetDownload(aid);
                }
            } else if (action === "open-gallery") {
                switchView("gallery");
            }
        });

        // Selección de keyframes base64 en el chat
        document.addEventListener("change", (event) => {
            const kfBox = event.target.closest("[data-keyframe-checkbox]");
            if (kfBox) {
                const aid = kfBox.getAttribute("data-keyframe-checkbox");
                if (kfBox.checked) {
                    state.selectedKeyframeIds.add(aid);
                } else {
                    state.selectedKeyframeIds.delete(aid);
                }
            }
        });

        // Crear proyecto
        const createForm = document.getElementById("create-project-form");
        if (createForm) {
            createForm.addEventListener("submit", async (event) => {
                event.preventDefault();
                const nameInput = document.getElementById("project-name-input");
                const aspectSelect = document.getElementById("project-aspect-select");
                const descInput = document.getElementById("project-desc-input");

                const name = nameInput ? nameInput.value.trim() : "";
                if (!name) return;

                const resp = await apiFetch("/studio/api/projects", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        name,
                        aspect_ratio: aspectSelect ? aspectSelect.value : "16:9",
                        description: descInput ? descInput.value.trim() : "",
                    }),
                });
                if (resp.ok) {
                    const created = await resp.json();
                    if (nameInput) nameInput.value = "";
                    if (descInput) descInput.value = "";
                    state.activeProjectId = created.id;
                    setToast(`Proyecto creado: «${created.name}» (${created.id}).`, "success");
                    await refreshProjects();
                } else {
                    setToast(`Error al crear proyecto (${resp.status}).`, "error");
                }
            });
        }

        // Subir medio al proyecto
        const uploadForm = document.getElementById("upload-media-form");
        if (uploadForm) {
            uploadForm.addEventListener("submit", async (event) => {
                event.preventDefault();
                const fileInput = document.getElementById("media-file-input");
                const catSelect = document.getElementById("media-category-select");
                const statusMsg = document.getElementById("upload-status-msg");

                if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
                    setToast("Selecciona un archivo de medio antes de subir.", "error");
                    return;
                }

                const projectId = await ensureActiveProject();
                if (!projectId) return;

                const file = fileInput.files[0];
                const formData = new FormData();
                formData.append("file", file);
                formData.append("category", catSelect ? catSelect.value : "uploads");

                if (statusMsg) statusMsg.textContent = `Subiendo ${file.name}…`;

                const resp = await apiFetch(
                    `/studio/api/projects/${encodeURIComponent(projectId)}/assets`,
                    {
                        method: "POST",
                        body: formData,
                    }
                );
                if (resp.ok) {
                    const uploaded = await resp.json();
                    fileInput.value = "";
                    const msg = `Subida completada: ${uploaded.original_filename} (${uploaded.kind}, ${formatBytes(uploaded.size_bytes)})`;
                    if (statusMsg) statusMsg.textContent = msg;
                    setToast(msg, "success");
                    if (state.activeProjectId === projectId) {
                        await selectProject(projectId, false);
                    }
                } else {
                    const errBody = await resp.text();
                    if (statusMsg) statusMsg.textContent = `Error (${resp.status}): ${errBody}`;
                    setToast(`Error al subir medio (${resp.status}).`, "error");
                }
            });
        }

        // Enviar mensaje de chat
        const chatForm = document.getElementById("chat-form");
        if (chatForm) {
            chatForm.addEventListener("submit", async (event) => {
                event.preventDefault();
                const input = document.getElementById("chat-message-input");
                if (!input) return;
                const text = input.value.trim();
                if (!text) return;
                input.value = "";
                await sendChatMessage(text);
            });
        }

        // Limpiar historial de chat
        const clearChatBtn = document.getElementById("clear-chat-btn");
        if (clearChatBtn) {
            clearChatBtn.addEventListener("click", async () => {
                const targetProjectId = state.activeProjectId;
                if (!targetProjectId) return;
                const resp = await apiFetch(
                    `/studio/api/projects/${encodeURIComponent(targetProjectId)}/chat`,
                    { method: "DELETE" }
                );
                if (resp.ok && state.activeProjectId === targetProjectId) {
                    state.chatDomEpoch += 1;
                    state.toolEvents = [];
                    renderToolEvents();
                    renderChatHistory([], targetProjectId);
                    setToast("Conversación del proyecto reiniciada.", "success");
                }
            });
        }

        // Plantillas y Jobs
        const tplSelect = document.getElementById("template-select");
        if (tplSelect) {
            tplSelect.addEventListener("change", renderTemplateDoc);
        }

        const tplForm = document.getElementById("template-submit-form");
        if (tplForm) {
            tplForm.addEventListener("submit", submitTemplateJob);
        }

        const refreshJobsBtn = document.getElementById("refresh-jobs-btn");
        if (refreshJobsBtn) {
            refreshJobsBtn.addEventListener("click", refreshJobs);
        }

        // Galería
        const galFilter = document.getElementById("gallery-category-filter");
        if (galFilter) {
            galFilter.addEventListener("change", refreshGallery);
        }

        const refreshGalBtn = document.getElementById("refresh-gallery-btn");
        if (refreshGalBtn) {
            refreshGalBtn.addEventListener("click", refreshGallery);
        }

        // Timeline EDL
        const edlProposeBtn = document.getElementById("edl-propose-btn");
        if (edlProposeBtn) {
            edlProposeBtn.addEventListener("click", async () => {
                await sendChatMessage("Propón un EDL de highlights para el clip inicial");
                await refreshEdl();
            });
        }

        const refreshEdlBtn = document.getElementById("refresh-edl-btn");
        if (refreshEdlBtn) {
            refreshEdlBtn.addEventListener("click", refreshEdl);
        }

        // Login / Basic-Auth
        const loginForm = document.getElementById("login-form");
        if (loginForm) {
            const userInput = document.getElementById("login-user-input");
            const passInput = document.getElementById("login-pass-input");
            const simCheck = document.getElementById("login-simulate-edge-checkbox");

            if (userInput) userInput.value = state.auth.username || "admin";
            if (passInput) passInput.value = state.auth.password || "";
            if (simCheck) simCheck.checked = Boolean(state.auth.simulateEdge);

            loginForm.addEventListener("submit", async (event) => {
                event.preventDefault();
                state.auth = {
                    username: userInput ? userInput.value.trim() || "admin" : "admin",
                    password: passInput ? passInput.value : "",
                    simulateEdge: simCheck ? simCheck.checked : false,
                };
                saveAuthToStorage();
                await refreshHealthAndStatus();
                await refreshProjects();
                setToast("Credenciales Basic-Auth actualizadas y verificadas.", "success");
            });
        }

        const clearLoginBtn = document.getElementById("login-clear-btn");
        if (clearLoginBtn) {
            clearLoginBtn.addEventListener("click", async () => {
                state.auth = { username: "admin", password: "", simulateEdge: false };
                saveAuthToStorage();
                const passInput = document.getElementById("login-pass-input");
                const simCheck = document.getElementById("login-simulate-edge-checkbox");
                if (passInput) passInput.value = "";
                if (simCheck) simCheck.checked = false;
                await refreshHealthAndStatus();
                setToast("Credenciales locales limpiadas.", "success");
            });
        }

        const refreshProjectsBtn = document.getElementById("refresh-projects-btn");
        if (refreshProjectsBtn) {
            refreshProjectsBtn.addEventListener("click", refreshProjects);
        }

        const refreshHealthBtn = document.getElementById("refresh-health-btn");
        if (refreshHealthBtn) {
            refreshHealthBtn.addEventListener("click", refreshHealthAndStatus);
        }
    }

    async function initStudioPanel() {
        bindEvents();
        await refreshHealthAndStatus();
        await loadTemplates();
        await refreshProjects();
        connectProgressSse();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initStudioPanel);
    } else {
        initStudioPanel();
    }
})();
