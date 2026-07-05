/**
 * Dashboard Controller
 *
 * Manages the main dashboard view showing all automation instances.
 *
 * Real-time updates via WebSocket (replaces 30s polling):
 *   - On connect: receives full instances_snapshot
 *   - On device_event: patches the affected card(s) with new event info
 *   - On instance_update: patches the affected card's metadata
 *   - Keepalive pings prevent stale connections
 *
 * Card body click → opens KPI modal (95vh detail view with charts).
 */

import { api, utils } from '../main.js';

export class DashboardController {
    /**
     * @param {string} containerId - ID of the container element
     */
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.instances = [];
        /** @type {WebSocket|null} */
        this.ws = null;
        /** Reconnect backoff timer */
        this._reconnectDelay = 1000;
        /** Max reconnect delay (30s) */
        this._maxReconnectDelay = 30000;
        /** Track recent events per instance (last 5, for mini-KPI on card) */
        this.recentEvents = {};
        // Restore debug panel state from localStorage
        const saved = JSON.parse(localStorage.getItem('debugPanels') || '{}');
        this.openDebugPanels = new Set(saved.open || []);
        this.debugSizes = saved.sizes || {};
        // Collapsible-group state. ALL groups default collapsed on page load
        // (Apps section, Drivers section, any future section). The user's
        // explicit expand/collapse choice is persisted to localStorage so a
        // re-render triggered by Run / Update / Pause / Resume / Delete
        // does NOT pop closed groups back open. Stored as the set of group
        // ids that are CURRENTLY EXPANDED — empty set = everything wrapped.
        // (See memory: feedback-dashboard-groups-collapsed-default.)
        let savedExpanded;
        try {
            savedExpanded = JSON.parse(
                localStorage.getItem('dashboardExpandedGroups') || '[]'
            );
        } catch (_) {
            savedExpanded = [];
        }
        this.expandedGroups = new Set(savedExpanded);
        // App-type id → display name (e.g. "Screen Time Planner"). Loaded from
        // /api/app-types in init() so groups/cards never show a hardcoded value.
        this.appTypeNames = {};
    }

    /**
     * Persist the expanded-groups set to localStorage. Called whenever
     * `toggleGroup` flips a group's state.
     */
    _saveExpandedGroups() {
        try {
            localStorage.setItem(
                'dashboardExpandedGroups',
                JSON.stringify([...this.expandedGroups])
            );
        } catch (_) {
            // Storage full / disabled — non-fatal, the page just won't
            // remember choice across re-renders this session.
        }
    }

    /**
     * Initialize the dashboard.
     *
     * Loads instances via HTTP first (so the dashboard renders immediately),
     * then opens a WebSocket for real-time updates. This way the UI is never
     * blocked by a WebSocket connection failure.
     */
    async init() {
        // Load app-type names first so the first render shows real names
        // (e.g. "Screen Time Planner") instead of the 'Automation' fallback.
        await this.loadAppTypes();
        // Render immediately from HTTP — never gate the UI on WebSocket
        await this.loadInstances();

        // Drivers section (standalone device controllers; static for now).
        this.renderDrivers();

        // Then open WebSocket for real-time push updates
        this._connectWebSocket();
    }

    /* =========================================================================
       WebSocket — Real-time updates
       ========================================================================= */

    /**
     * Establish WebSocket connection to /ws/dashboard.
     * On connect, the server sends an instances_snapshot with full data.
     * Subsequent messages are incremental (device_event, instance_update).
     */
    _connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws/dashboard`;

        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            console.log('Dashboard WS connected');
            this._wsConnected = true;
            this._reconnectDelay = 1000;
        };

        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                this._handleWsMessage(msg);
            } catch (e) {
                console.warn('Dashboard WS parse error:', e);
            }
        };

        this.ws.onclose = (event) => {
            // Code 1006 with no prior open = endpoint doesn't exist (404).
            // Back off aggressively to avoid log spam.
            if (!this._wsConnected) {
                this._reconnectDelay = Math.min(
                    this._reconnectDelay * 2,
                    this._maxReconnectDelay
                );
                console.log(
                    `Dashboard WS endpoint unavailable, retry in ${this._reconnectDelay / 1000}s`
                );
            } else {
                console.log('Dashboard WS disconnected, reconnecting...');
                this._reconnectDelay = Math.min(
                    this._reconnectDelay * 1.5,
                    this._maxReconnectDelay
                );
            }
            this._wsConnected = false;
            setTimeout(() => this._connectWebSocket(), this._reconnectDelay);
        };

        this.ws.onerror = (err) => {
            console.error('Dashboard WS error:', err);
        };
    }

    /**
     * Handle incoming WebSocket messages.
     * @param {object} msg - Parsed JSON message
     */
    _handleWsMessage(msg) {
        switch (msg.type) {
            case 'instances_snapshot':
                // Full instance list — initial load or periodic sync
                this.instances = msg.instances || [];
                this.render();
                this.updateStatusSummary();
                break;

            case 'device_event':
                // A device changed state — update affected cards
                this._onDeviceEvent(msg);
                break;

            case 'instance_update':
                // Instance metadata changed (paused, settings, etc.)
                this._onInstanceUpdate(msg);
                break;

            case 'ping':
                // Keepalive — no action needed
                break;

            default:
                console.debug('Dashboard WS unknown message type:', msg.type);
        }
    }

    /**
     * Handle a real-time device event.
     * Updates the mini-KPI on affected cards and appends to open debug panels.
     * @param {object} evt - device_event message
     */
    _onDeviceEvent(evt) {
        const instanceIds = evt.instance_ids || [];

        for (const instId of instanceIds) {
            // Track recent events for mini-KPI
            if (!this.recentEvents[instId]) {
                this.recentEvents[instId] = [];
            }
            this.recentEvents[instId].unshift({
                device_name: evt.device_name,
                event_name: evt.event_name,
                event_value: evt.event_value,
                time: evt._ts || new Date().toISOString()
            });
            // Keep only last 20 per instance
            if (this.recentEvents[instId].length > 20) {
                this.recentEvents[instId].length = 20;
            }

            // Update the mini-KPI on the card
            this._updateCardMiniKpi(instId);

            // Append to open debug panel (if visible)
            this._appendDebugEvent(instId, evt);
        }
    }

    /**
     * Handle an instance metadata update.
     * Re-fetches instances and re-renders only if data actually changed.
     * @param {object} msg - instance_update message
     */
    async _onInstanceUpdate(msg) {
        // Refetch to get the latest state
        try {
            this.instances = await api.get('/instances');
            this.render();
            this.updateStatusSummary();
        } catch (e) {
            console.error('Failed to refresh instances after update:', e);
        }
    }

    /**
     * Update the mini-KPI section on a single card without full re-render.
     * @param {number} instId - Instance ID
     */
    _updateCardMiniKpi(instId) {
        const el = document.getElementById(`mini-kpi-${instId}`);
        if (!el) return;

        const events = this.recentEvents[instId] || [];
        const count = events.length;
        const last = events[0];

        let html = `<span class="mini-kpi-count">${count} event${count !== 1 ? 's' : ''}</span>`;
        if (last) {
            const time = new Date(last.time).toLocaleTimeString([], {
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
            html += `<span class="mini-kpi-last">${utils.escapeHtml(last.device_name)}: `
                + `${utils.escapeHtml(last.event_name)}=${utils.escapeHtml(last.event_value)} `
                + `<span class="mini-kpi-time">${time}</span></span>`;
        }

        el.innerHTML = html;
    }

    /**
     * Append a live event to an open debug panel without full refresh.
     * @param {number} instId - Instance ID
     * @param {object} evt - device_event data
     */
    _appendDebugEvent(instId, evt) {
        const output = document.getElementById(`debug-output-${instId}`);
        if (!output) return;
        // Only append if the debug panel is visible
        const panel = document.getElementById(`debug-${instId}`);
        if (!panel || panel.style.display === 'none') return;

        const time = new Date(evt._ts || Date.now()).toLocaleTimeString();
        const line = document.createElement('div');
        line.className = 'debug-line';
        // Match refreshDebug() — link device name to Hubitat admin when
        // hub_ip + native id are present on the live event.
        const deviceLabel = utils.escapeHtml(evt.device_name || evt.device_id);
        const hubIp = evt.hub_ip;
        const nativeId = evt.device_id || evt.hubitat_device_id;
        const deviceHtml = (hubIp && nativeId)
            ? `<a class="debug-device" href="http://${utils.escapeHtml(hubIp)}/device/edit/${utils.escapeHtml(String(nativeId))}" target="_blank" rel="noopener" title="Open on hub ${utils.escapeHtml(hubIp)}">${deviceLabel}</a>`
            : `<span class="debug-device">${deviceLabel}</span>`;
        line.innerHTML = `<span class="debug-time">${time}</span> `
            + `${deviceHtml} `
            + `<span class="debug-event">${utils.escapeHtml(evt.event_name)}</span>`
            + `<span class="debug-value">= ${utils.escapeHtml(evt.event_value || '')}</span>`;

        // Append: newest at the bottom, chronological top-to-bottom
        // (terminal-scrollback convention).
        // Sticky-scroll: keep the view pinned to the bottom only when
        // the user was already at the bottom — preserves scrollback
        // position if they scrolled up to read older events.
        const wasAtBottom = (output.scrollHeight - output.scrollTop
                             - output.clientHeight) < 4;
        output.appendChild(line);

        // Cap at 100 lines (drop oldest from the top)
        while (output.children.length > 100) {
            output.removeChild(output.firstChild);
        }

        if (wasAtBottom) {
            output.scrollTop = output.scrollHeight;
        }
    }

    /* =========================================================================
       Fallback: HTTP-based refresh (used after user actions)
       ========================================================================= */

    /**
     * Load all instances from API (fallback for user-triggered actions).
     * After the action, the next WebSocket snapshot will keep things in sync.
     */
    async loadInstances() {
        try {
            this.instances = await api.get('/instances');
            this.render();
            this.updateStatusSummary();
        } catch (error) {
            console.error('Failed to load instances:', error);
            this.container.innerHTML = `
                <div class="error-message">
                    <p>Failed to load automations. Please try again.</p>
                    <button class="btn btn-primary" onclick="location.reload()">Retry</button>
                </div>
            `;
        }
    }

    /* =========================================================================
       Rendering
       ========================================================================= */

    /**
     * Render instance cards
     */
    render() {
        if (this.instances.length === 0) {
            this.container.innerHTML = '';
            document.getElementById('empty-state').style.display = 'block';
            return;
        }

        document.getElementById('empty-state').style.display = 'none';

        // Group instances by app type so the Apps section shows one collapsible
        // group per app (app name → its instances). Cards themselves are
        // unchanged (renderCard).
        const groups = {};
        for (const inst of this.instances) {
            (groups[inst.app_type_id] = groups[inst.app_type_id] || []).push(inst);
        }
        this.container.innerHTML = Object.keys(groups).map(typeId => {
            const insts = groups[typeId];
            const name = this.getAppTypeName(Number(typeId));
            const cards = insts.map(inst => this.renderCard(inst)).join('');
            const n = insts.length;
            const gridId = `app-group-${typeId}`;
            // Default-collapsed. Only render expanded if the user has
            // explicitly opened this group earlier in the session (state
            // persisted via toggleGroup → _saveExpandedGroups). Re-renders
            // triggered by Run/Update/Pause/etc. preserve the user's choice
            // — closed stays closed, open stays open.
            const isExpanded = this.expandedGroups.has(gridId);
            const ariaExp = isExpanded ? 'true' : 'false';
            const caret = isExpanded ? '▾' : '▸';
            const gridStyle = isExpanded ? '' : ' style="display:none;"';
            return `
                <div class="app-group" data-app="${typeId}">
                    <button class="app-group-header" aria-expanded="${ariaExp}"
                            onclick="dashboard.toggleGroup(this, '${gridId}')">
                        <span class="app-group-caret">${caret}</span>
                        <span class="app-group-name">${utils.escapeHtml(name)}</span>
                        <span class="app-group-count">${n} instance${n !== 1 ? 's' : ''}</span>
                    </button>
                    <div class="instances-grid app-group-instances" id="${gridId}"${gridStyle}>${cards}</div>
                </div>`;
        }).join('');

        // Bind event handlers
        this.bindEvents();

        // Restore open debug panels and saved sizes
        for (const id of this.openDebugPanels) {
            const panel = document.getElementById(`debug-${id}`);
            if (panel) {
                panel.style.display = 'block';
                const output = document.getElementById(`debug-output-${id}`);
                if (output && this.debugSizes[id]) {
                    output.style.height = this.debugSizes[id];
                }
                this.refreshDebug(id);
                // Kick off the runtime-status poller for pre-opened
                // panels too. toggleDebug only fires this on user-
                // initiated open; without this line, panels restored
                // from localStorage on page load stay stuck on the
                // "Loading status…" placeholder forever.
                this._startRuntimeStatusPoll(parseInt(id, 10));
            }
        }

        // Bind resize observer for debug outputs
        this.container.querySelectorAll('.debug-output').forEach(output => {
            const observer = new ResizeObserver(() => {
                const id = output.id.replace('debug-output-', '');
                this.debugSizes[id] = output.style.height;
                this.saveDebugState();
            });
            observer.observe(output);
        });

        // Update mini-KPIs for any cached recent events
        for (const instId of Object.keys(this.recentEvents)) {
            this._updateCardMiniKpi(Number(instId));
        }
    }

    /**
     * Render a single instance card.
     *
     * The card-body is clickable and opens the KPI modal.
     * Actions buttons are in the card-actions section below.
     *
     * @param {object} inst - Instance data
     * @returns {string} HTML string
     */
    renderCard(inst) {
        const isPaused = inst.is_paused;
        const deviceCount = this.countDevices(inst.device_selections);
        const lastActivity = inst.last_activity_at
            ? this._timeAgo(inst.last_activity_at)
            : 'No activity';
        const errorCount = inst.error_count || 0;

        return `
            <div class="instance-card ${isPaused ? 'paused' : ''}" data-id="${inst.id}">
                <div class="card-header">
                    <h3>${utils.escapeHtml(inst.label)}</h3>
                    <span class="app-type-badge">${this.getAppTypeName(inst.app_type_id)}</span>
                </div>
                <div class="card-body card-body-clickable"
                     onclick="dashboard.openKpi(${inst.id}, '${utils.escapeHtml(inst.label).replace(/'/g, "\\'")}')"
                     title="Click for detailed KPIs">
                    <div class="card-body-top">
                        <span class="status-indicator ${isPaused ? 'paused' : 'active'}">
                            ${isPaused ? 'PAUSED' : 'ACTIVE'}
                        </span>
                        <div class="card-stats">
                            <span class="card-stat">${deviceCount} device${deviceCount !== 1 ? 's' : ''}</span>
                            <span class="card-stat-sep">&middot;</span>
                            <span class="card-stat">${lastActivity}</span>
                            ${errorCount > 0 ? `<span class="card-stat-sep">&middot;</span><span class="card-stat card-stat-error">${errorCount} error${errorCount !== 1 ? 's' : ''}</span>` : ''}
                        </div>
                    </div>
                    <div class="mini-kpi" id="mini-kpi-${inst.id}">
                        <span class="mini-kpi-hint">Click for KPIs</span>
                    </div>
                </div>
                <div class="card-actions">
                    <button class="action-icon-btn" title="Run now"
                            aria-label="Run instance" onclick="dashboard.runInstance(${inst.id})">&#9654;</button>
                    <button class="action-icon-btn" title="Update from DB"
                            aria-label="Update instance" onclick="dashboard.updateInstance(${inst.id})">&#8635;</button>
                    <button class="action-icon-btn" title="${isPaused ? 'Resume' : 'Pause'}"
                            aria-label="${isPaused ? 'Resume' : 'Pause'} instance"
                            onclick="dashboard.togglePause(${inst.id}, ${isPaused})">${isPaused ? '&#9654;' : '&#10074;&#10074;'}</button>
                    <button class="action-icon-btn" title="Edit"
                            aria-label="Edit instance" onclick="location.href='/instance/${inst.id}'">&#9998;</button>
                    <button class="action-icon-btn" title="Debug panel"
                            aria-label="Toggle debug panel" onclick="dashboard.toggleDebug(${inst.id})">&#9881;</button>
                    <button class="action-icon-btn" title="Open test runner"
                            aria-label="Open test runner" onclick="dashboard.openTest(${inst.id}, '${utils.escapeHtml(inst.label).replace(/'/g, "\\'")}')">&#10003;</button>
                    <button class="action-icon-btn action-icon-danger" title="Delete"
                            aria-label="Delete instance" onclick="dashboard.deleteInstance(${inst.id})">&#10005;</button>
                </div>
                <div class="debug-panel" id="debug-${inst.id}" style="display:none;">
                    <!-- Live runtime status: countdown to next AML turn-off,
                         current mode, motion-active verdict. Polled while
                         the debug panel is open. -->
                    <div class="debug-status" id="debug-status-${inst.id}">
                        <span class="debug-status-loading">Loading status…</span>
                    </div>
                    <div class="debug-toolbar">
                        <span class="debug-title">Event Log</span>
                        <div class="debug-toolbar-actions">
                            <button class="action-icon-btn btn-copy" title="Copy log to clipboard"
                                    aria-label="Copy log"
                                    onclick="dashboard.copyDebug(${inst.id}, this)">&#9112;</button>
                            <button class="action-icon-btn" title="Refresh log"
                                    aria-label="Refresh log"
                                    onclick="dashboard.refreshDebug(${inst.id})">&#8635;</button>
                        </div>
                    </div>
                    <div class="debug-output" id="debug-output-${inst.id}"></div>
                </div>
            </div>
        `;
    }

    /**
     * Count total devices across all categories
     * @param {object} selections - Device selections
     * @returns {number} Total count
     */
    countDevices(selections) {
        if (!selections) return 0;
        return Object.values(selections).reduce((sum, arr) => sum + (arr ? arr.length : 0), 0);
    }

    /**
     * Get app type display name
     * @param {number} typeId - App type ID
     * @returns {string} Display name
     */
    getAppTypeName(typeId) {
        // Data-driven: real display_name from /api/app-types (loaded in init).
        // 'Automation' is only a last-resort fallback (map not yet loaded, or an
        // unknown type id created after this page loaded).
        return this.appTypeNames[typeId] || 'Automation';
    }

    /**
     * Load the app-type id → display_name map from /api/app-types so groups and
     * card badges show the actual app name (e.g. "Screen Time Planner") instead
     * of a hardcoded value.
     */
    async loadAppTypes() {
        try {
            const types = await api.get('/app-types');
            const map = {};
            for (const t of (types || [])) map[t.id] = t.display_name;
            this.appTypeNames = map;
        } catch (e) {
            console.warn('Failed to load app types:', e);
        }
    }

    /**
     * Update the status summary
     */
    updateStatusSummary() {
        const total = this.instances.length;
        const paused = this.instances.filter(i => i.is_paused).length;
        const active = total - paused;

        document.getElementById('instances-count').textContent =
            `${total} automation${total !== 1 ? 's' : ''} (${active} active, ${paused} paused)`;
    }

    /**
     * Bind event handlers
     */
    bindEvents() {
        window.dashboard = this;
    }

    /**
     * Toggle a collapsible group (app group or driver group). `btn` is the
     * clicked header button; `gridId` is the instances grid it controls.
     *
     * Persists the new state to ``this.expandedGroups`` (saved to
     * localStorage) so re-renders triggered by Run/Update/Pause/Resume/
     * Delete preserve the user's choice. Without this persistence the next
     * instance-action click would re-render every group back to the default
     * (collapsed) — the original bug Elfege flagged.
     */
    toggleGroup(btn, gridId) {
        const grid = document.getElementById(gridId);
        if (!grid) return;
        const open = grid.style.display !== 'none';
        grid.style.display = open ? 'none' : '';
        btn.setAttribute('aria-expanded', String(!open));
        const caret = btn.querySelector('.app-group-caret');
        if (caret) caret.textContent = open ? '▸' : '▾';
        // After toggle: open=true means "was open, now closed", so the
        // NEW state is collapsed → remove from the expanded set. open=false
        // means "was closed, now open" → add.
        if (open) {
            this.expandedGroups.delete(gridId);
        } else {
            this.expandedGroups.add(gridId);
        }
        this._saveExpandedGroups();
    }

    /**
     * Render the Drivers section — standalone device controllers (not app
     * instances). For now the only driver is the Samsung TV with one device;
     * drilling in (driver → device → controller) opens the existing
     * /samsung-tv page. Static for now — promote to data-driven when a second
     * driver lands.
     */
    async renderDrivers() {
        const el = document.getElementById('drivers-container');
        if (!el) return;
        window.dashboard = this;  // ensure inline onclick handlers resolve

        // Data-driven: one card per samsung_tv_instances row (add/remove/edit
        // live at /samsung-tv/manage). No longer hardcoded to a single TV.
        let tvs = [];
        try {
            const d = await fetch('/samsung-tv/api/list').then(r => r.ok ? r.json() : null);
            if (d && Array.isArray(d.instances)) tvs = d.instances;
        } catch (_) { /* leave empty */ }

        const tvCards = tvs.length ? tvs.map(t => {
            const badge = t._is_running
                ? '<span class="status-indicator active">RUNNING</span>'
                : '<span class="status-indicator">STOPPED</span>';
            const addr = utils.escapeHtml(t.tv_ip || '') + (t.port ? ':' + t.port : '');
            return `
                    <div class="instance-card driver-instance-card" style="cursor:pointer;"
                         onclick="window.location='/samsung-tv/manage'"
                         title="Manage TVs">
                        <div class="card-header">
                            <h3>${utils.escapeHtml(t.label || 'TV')}</h3>
                            <span class="app-type-badge">Samsung TV</span>
                        </div>
                        <div class="card-body">
                            <div class="card-body-top">
                                ${badge}
                                <div class="card-stats"><span class="card-stat">${addr}</span></div>
                            </div>
                        </div>
                    </div>`;
        }).join('') : `
                    <div class="instance-card driver-instance-card" style="cursor:pointer;"
                         onclick="window.location='/samsung-tv/manage'" title="Add a TV">
                        <div class="card-header"><h3>No TVs yet</h3></div>
                        <div class="card-body"><div class="card-body-top">
                            <div class="card-stats"><span class="card-stat">+ Add a TV →</span></div>
                        </div></div>
                    </div>`;

        el.innerHTML = `
            <div class="app-group" data-driver="samsung_tv">
                <button class="app-group-header" aria-expanded="false"
                        onclick="dashboard.toggleGroup(this, 'driver-group-samsung_tv')">
                    <span class="app-group-caret">▸</span>
                    <span class="app-group-name">Samsung TV</span>
                    <span class="app-group-count">${tvs.length} device${tvs.length === 1 ? '' : 's'}</span>
                </button>
                <div class="instances-grid app-group-instances" id="driver-group-samsung_tv"
                     style="display:none;">
                    ${tvCards}
                </div>
            </div>`;

        // Sonos driver (the second driver). Local UPnP speakers; drilling in
        // opens the /sonos controller (TTS, set/restore/lock volume, play, stop).
        let sonosCount = 0;
        try {
            const sp = await fetch('/api/sonos/speakers').then(r => r.ok ? r.json() : null);
            if (sp && sp.speakers) {
                sonosCount = new Set(Object.values(sp.speakers)).size;  // distinct rooms
            }
        } catch (_) { /* keep 0 */ }
        el.insertAdjacentHTML('beforeend', `
            <div class="app-group" data-driver="sonos">
                <button class="app-group-header" aria-expanded="false"
                        onclick="dashboard.toggleGroup(this, 'driver-group-sonos')">
                    <span class="app-group-caret">▸</span>
                    <span class="app-group-name">Sonos</span>
                    <span class="app-group-count">${sonosCount} room${sonosCount === 1 ? '' : 's'}</span>
                </button>
                <div class="instances-grid app-group-instances" id="driver-group-sonos"
                     style="display:none;">
                    <div class="instance-card driver-instance-card" style="cursor:pointer;"
                         onclick="window.location='/sonos'"
                         title="Open the Sonos controller">
                        <div class="card-header">
                            <h3>Sonos Speakers</h3>
                            <span class="app-type-badge">Sonos</span>
                        </div>
                        <div class="card-body">
                            <div class="card-body-top">
                                <span class="status-indicator active">CONTROLLER</span>
                                <div class="card-stats">
                                    <span class="card-stat">Open controller →</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`);
    }

    /* =========================================================================
       Instance Actions
       ========================================================================= */

    /**
     * Toggle pause state for an instance
     * @param {number} instanceId - Instance ID
     * @param {boolean} isPaused - Current pause state
     */
    async togglePause(instanceId, isPaused) {
        try {
            if (isPaused) {
                await api.post(`/instances/${instanceId}/resume`);
            } else {
                // Explicit reason — flags this pause as user-initiated.
                // AML's on_mode_change only auto-resumes when the reason is
                // 'mode_exclusion', so 'ui_button' (and any other manually
                // assigned reason) is safe from accidental auto-resume on
                // mode flips.
                //
                // Pause duration is per-instance: read pauseDuration +
                // pauseDurationUnit from the instance's settings, convert
                // to minutes. 0 = indefinite (no auto-resume; user must
                // hit Resume manually). Fallback to 60 minutes only if
                // neither setting is declared by the app type. Surfaced
                // 2026-06-16 — the previous hardcoded 60 was silently
                // auto-resuming pauses the user intended to be indefinite.
                // Universal pause contract (2026-06-16): every app declares
                // pauseDuration + pauseDurationUnit (Seconds|Minutes) +
                // resumeOnModeChange. We send duration_seconds when the unit
                // is Seconds so sub-minute pauses don't degenerate to
                // indefinite via integer-divide rounding.
                const inst = this.instances.find(i => i.id === instanceId);
                const settings = (inst && inst.settings) || {};
                const body = { reason: 'ui_button' };
                if ('pauseDuration' in settings) {
                    const raw = parseInt(settings.pauseDuration, 10);
                    if (!isNaN(raw) && raw >= 0) {
                        const unit = (settings.pauseDurationUnit || 'Minutes');
                        if (unit === 'Seconds') {
                            body.duration_seconds = raw;
                        } else {
                            body.duration_minutes = raw;
                        }
                    } else {
                        body.duration_minutes = 60;
                    }
                } else {
                    // App type pre-dates the universal contract — legacy
                    // 60-minute pause stays as the safe fallback.
                    body.duration_minutes = 60;
                }
                await api.post(`/instances/${instanceId}/pause`, body);
            }
            await this.loadInstances();
        } catch (error) {
            utils.notify(`Failed to ${isPaused ? 'resume' : 'pause'} instance: ${error.message}`, 'error');
        }
    }

    /**
     * Start an instance
     * @param {number} instanceId - Instance ID
     */
    async runInstance(instanceId) {
        try {
            await api.post(`/instances/${instanceId}/run`);
            utils.notify('Instance started');
            await this.loadInstances();
        } catch (error) {
            utils.notify(`Failed to start: ${error.message}`, 'error');
        }
    }

    /**
     * Reload an instance (stop + start with current config)
     * @param {number} instanceId - Instance ID
     */
    async updateInstance(instanceId) {
        try {
            await api.post(`/instances/${instanceId}/update`);
            utils.notify('Instance reloaded');
            await this.loadInstances();
        } catch (error) {
            utils.notify(`Failed to reload: ${error.message}`, 'error');
        }
    }

    /* =========================================================================
       Debug Panel
       ========================================================================= */

    /**
     * Persist debug panel state to localStorage
     */
    saveDebugState() {
        localStorage.setItem('debugPanels', JSON.stringify({
            open: [...this.openDebugPanels],
            sizes: this.debugSizes
        }));
    }

    /**
     * Toggle debug panel for an instance
     * @param {number} instanceId - Instance ID
     */
    async toggleDebug(instanceId) {
        const panel = document.getElementById(`debug-${instanceId}`);
        if (!panel) return;

        const isVisible = panel.style.display !== 'none';
        panel.style.display = isVisible ? 'none' : 'block';

        if (isVisible) {
            this.openDebugPanels.delete(instanceId);
            this._stopRuntimeStatusPoll(instanceId);
        } else {
            this.openDebugPanels.add(instanceId);
            this._startRuntimeStatusPoll(instanceId);
            await this.refreshDebug(instanceId);
        }
        this.saveDebugState();
    }

    /**
     * Load recent events into the debug panel
     * @param {number} instanceId - Instance ID
     */
    async refreshDebug(instanceId) {
        const output = document.getElementById(`debug-output-${instanceId}`);
        if (!output) return;

        output.innerHTML = '<span class="debug-loading">Loading events...</span>';

        try {
            const events = await api.get(`/instances/${instanceId}/events`);
            if (!events || events.length === 0) {
                output.innerHTML = '<span class="debug-empty">No events routed to this instance yet.</span>';
                return;
            }

            // API returns events newest-first (received_at.desc). Reverse
            // so the panel reads chronologically with the most-recent
            // event at the BOTTOM — same convention as terminal scrollback
            // and chat logs. The auto-scroll below then lands on the
            // newest event, not the oldest one.
            const chronological = events.slice().reverse();
            output.innerHTML = chronological.map(evt => {
                const time = new Date(evt.received_at).toLocaleTimeString();
                // Link the device name to its Hubitat admin page when we
                // have both hub_ip and the native hubitat_device_id.
                // Hubitat's per-device admin URL is /device/edit/<id>.
                // target=_blank so investigation doesn't lose dashboard context.
                const deviceLabel = utils.escapeHtml(
                    evt.device_name || evt.hubitat_device_id
                );
                const hubIp = evt.hub_ip;
                const nativeId = evt.hubitat_device_id;
                const deviceHtml = (hubIp && nativeId)
                    ? `<a class="debug-device" href="http://${utils.escapeHtml(hubIp)}/device/edit/${utils.escapeHtml(String(nativeId))}" target="_blank" rel="noopener" title="Open on hub ${utils.escapeHtml(hubIp)}">${deviceLabel}</a>`
                    : `<span class="debug-device">${deviceLabel}</span>`;
                return `<div class="debug-line">`
                    + `<span class="debug-time">${time}</span> `
                    + `${deviceHtml} `
                    + `<span class="debug-event">${utils.escapeHtml(evt.event_type)}</span>`
                    + `<span class="debug-value">= ${utils.escapeHtml(evt.event_value || '')}</span>`
                    + `</div>`;
            }).join('');

            // Scroll to bottom (newest) after the chronological reverse.
            output.scrollTop = output.scrollHeight;
        } catch (error) {
            output.innerHTML = `<span class="debug-error">Error: ${error.message}</span>`;
        }
    }

    /* =========================================================================
       Live runtime status (debug panel header)
       ========================================================================= */

    /**
     * Start polling /api/instances/{id}/runtime-status every 2s while the
     * debug panel is open. Also runs a 1s local-tick that just decrements
     * the displayed countdown between server polls so the timer feels live.
     */
    _startRuntimeStatusPoll(instanceId) {
        this._runtimeTimers = this._runtimeTimers || {};
        this._stopRuntimeStatusPoll(instanceId);  // idempotent

        // Cached snapshot per instance, mutated by the server poll, read
        // by the local tick.
        const snap = { remaining: null, mode: null, isMotion: null,
                       paused: null, label: null };
        this._runtimeTimers[instanceId] = { snap };

        const renderTick = () => this._renderRuntimeStatus(instanceId, snap);
        const serverPoll = async () => {
            try {
                const data = await api.get(`/instances/${instanceId}/runtime-status`);
                snap.remaining = data.remaining_seconds;
                snap.mode = data.current_mode;
                snap.isMotion = data.is_motion_active;
                snap.paused = data.is_paused;
                snap.label = data.label;
                snap.lastMotionAt = data.last_motion_time
                    ? new Date(data.last_motion_time)
                    : null;
                snap.timeoutSeconds = data.timeout_seconds;
                // 'seconds' | 'minutes' — drives countdown formatting.
                snap.timeUnit = data.time_unit || 'seconds';
                renderTick();
            } catch (err) {
                const el = document.getElementById(`debug-status-${instanceId}`);
                if (el) el.innerHTML = `<span class="debug-status-error">runtime-status: ${utils.escapeHtml(err.message)}</span>`;
            }
        };

        // Server every 2s; local decrement every 1s.
        serverPoll();
        this._runtimeTimers[instanceId].server = setInterval(serverPoll, 2000);
        this._runtimeTimers[instanceId].local = setInterval(() => {
            if (snap.remaining !== null && snap.remaining > -3600) {
                snap.remaining -= 1;
                renderTick();
            }
        }, 1000);
    }

    _stopRuntimeStatusPoll(instanceId) {
        const t = (this._runtimeTimers || {})[instanceId];
        if (!t) return;
        if (t.server) clearInterval(t.server);
        if (t.local) clearInterval(t.local);
        delete this._runtimeTimers[instanceId];
    }

    _renderRuntimeStatus(instanceId, snap) {
        const el = document.getElementById(`debug-status-${instanceId}`);
        if (!el) return;

        // Format a duration in seconds using the instance's configured
        // timeUnit. If the user picked minutes, show "5m" / "5m 23s";
        // if seconds, show "30s". Below 1m always falls back to seconds
        // either way — "0m 18s" reads worse than "18s".
        const fmt = (secs) => {
            const t = Math.max(0, Math.floor(secs));
            if (snap.timeUnit === 'minutes') {
                const m = Math.floor(t / 60);
                const s = t % 60;
                if (m === 0) return `${s}s`;
                return s === 0 ? `${m}m` : `${m}m ${s}s`;
            }
            // 'seconds' (default): use compact "Xm Ys" for >60s, else "Ys"
            if (t >= 60) {
                const m = Math.floor(t / 60);
                const s = t % 60;
                return s === 0 ? `${m}m` : `${m}m ${s}s`;
            }
            return `${t}s`;
        };

        // Countdown rendering. Three states:
        //   remaining > 0  → "off in N"
        //   remaining ≤ 0  → "timeout elapsed (Nago)"
        //   remaining null → "timeout: N (no recent motion)"
        //                    — surfaces the configured window even when
        //                      we have no last_motion_time to anchor to.
        let countdownHtml;
        if (snap.remaining === null) {
            const tHtml = snap.timeoutSeconds
                ? fmt(snap.timeoutSeconds)
                : '?';
            countdownHtml = `<span class="debug-status-muted">timeout: ${tHtml} (no recent motion)</span>`;
        } else if (snap.remaining > 0) {
            countdownHtml = `<span class="debug-status-countdown" title="last motion: ${snap.lastMotionAt ? snap.lastMotionAt.toLocaleTimeString() : '?'}; timeout: ${fmt(snap.timeoutSeconds || 0)}">off in ${fmt(snap.remaining)}</span>`;
        } else {
            const overdue = Math.abs(Math.floor(snap.remaining));
            countdownHtml = `<span class="debug-status-overdue">timeout elapsed (${fmt(overdue)} ago)</span>`;
        }

        const modeHtml = snap.mode
            ? `<span class="debug-status-mode">mode: <strong>${utils.escapeHtml(snap.mode)}</strong></span>`
            : `<span class="debug-status-muted">mode: —</span>`;

        const motionHtml = snap.isMotion === true
            ? `<span class="debug-status-motion-active">motion: active</span>`
            : snap.isMotion === false
                ? `<span class="debug-status-motion-inactive">motion: inactive</span>`
                : `<span class="debug-status-muted">motion: —</span>`;

        const pausedHtml = snap.paused
            ? `<span class="debug-status-paused">PAUSED</span>`
            : '';

        el.innerHTML = [countdownHtml, modeHtml, motionHtml, pausedHtml]
            .filter(Boolean).join(' • ');
    }

    /**
     * Copy the debug panel's text content to clipboard.
     * @param {number} instanceId - Instance ID
     * @param {HTMLElement} btn - The copy button (for visual feedback)
     */
    copyDebug(instanceId, btn) {
        const output = document.getElementById(`debug-output-${instanceId}`);
        if (!output) return;
        utils.copyToClipboard(output.innerText, btn, 'Copy');
    }

    /* =========================================================================
       KPI Modal
       ========================================================================= */

    /**
     * Open the KPI modal for an instance.
     * Lazy-loads the kpi-modal module on first use.
     *
     * @param {number} instanceId - Instance ID
     * @param {string} instanceLabel - Instance display label
     */
    async openKpi(instanceId, instanceLabel) {
        try {
            const { openKpiModal } = await import('../components/kpi-modal.js');
            openKpiModal(instanceId, instanceLabel);
        } catch (error) {
            console.error('Failed to load KPI modal:', error);
            utils.notify('Failed to open KPI view: ' + error.message, 'error');
        }
    }

    /* =========================================================================
       E2E Test Modal
       ========================================================================= */

    /**
     * Open the E2E test modal for an instance.
     * Lazy-loads the E2ETestModal class on first use.
     *
     * @param {number} instanceId - Instance ID
     * @param {string} instanceLabel - Instance display label
     */
    async openTest(instanceId, instanceLabel) {
        try {
            const { E2ETestModal } = await import('./e2e-test-controller.js');
            const modal = new E2ETestModal(instanceId, instanceLabel);
            modal.open();
        } catch (error) {
            console.error('Failed to load E2E test module:', error);
            utils.notify('Failed to open test suite: ' + error.message, 'error');
        }
    }

    /* =========================================================================
       Delete
       ========================================================================= */

    /**
     * Delete an instance
     * @param {number} instanceId - Instance ID
     */
    async deleteInstance(instanceId) {
        if (!confirm('Are you sure you want to delete this automation?')) {
            return;
        }

        try {
            await api.delete(`/instances/${instanceId}`);
            await this.loadInstances();
            utils.notify('Automation deleted');
        } catch (error) {
            utils.notify(`Failed to delete: ${error.message}`, 'error');
        }
    }

    /* =========================================================================
       Utilities
       ========================================================================= */

    /**
     * Convert a timestamp to a short "X ago" string.
     * @param {string} isoStr - ISO timestamp
     * @returns {string}
     */
    _timeAgo(isoStr) {
        const diff = Date.now() - new Date(isoStr).getTime();
        const secs = Math.floor(diff / 1000);
        if (secs < 60) return `${secs}s ago`;
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        const days = Math.floor(hrs / 24);
        return `${days}d ago`;
    }
}
