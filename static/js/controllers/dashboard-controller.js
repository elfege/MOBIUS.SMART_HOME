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
    }

    /**
     * Initialize the dashboard
     */
    async init() {
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

        this.ws.onclose = () => {
            console.log('Dashboard WS disconnected, reconnecting...');
            setTimeout(() => this._connectWebSocket(), this._reconnectDelay);
            this._reconnectDelay = Math.min(
                this._reconnectDelay * 1.5,
                this._maxReconnectDelay
            );
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
        line.innerHTML = `<span class="debug-time">${time}</span> `
            + `<span class="debug-device">${utils.escapeHtml(evt.device_name || evt.device_id)}</span> `
            + `<span class="debug-event">${utils.escapeHtml(evt.event_name)}</span>`
            + `<span class="debug-value">= ${utils.escapeHtml(evt.event_value || '')}</span>`;

        // Prepend (newest first) to match the existing convention
        output.insertBefore(line, output.firstChild);

        // Cap at 100 lines
        while (output.children.length > 100) {
            output.removeChild(output.lastChild);
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

        this.container.innerHTML = this.instances.map(inst => this.renderCard(inst)).join('');

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
                    <button class="btn btn-secondary btn-small" onclick="dashboard.runInstance(${inst.id})">
                        Run
                    </button>
                    <button class="btn btn-secondary btn-small" onclick="dashboard.updateInstance(${inst.id})">
                        Update
                    </button>
                    <button class="btn btn-secondary btn-small" onclick="dashboard.togglePause(${inst.id}, ${isPaused})">
                        ${isPaused ? 'Resume' : 'Pause'}
                    </button>
                    <button class="btn btn-secondary btn-small" onclick="location.href='/instance/${inst.id}'">
                        Edit
                    </button>
                    <button class="btn btn-secondary btn-small" onclick="dashboard.toggleDebug(${inst.id})">
                        Debug
                    </button>
                    <button class="btn btn-secondary btn-small" onclick="dashboard.openTest(${inst.id}, '${utils.escapeHtml(inst.label).replace(/'/g, "\\'")}')">
                        Test
                    </button>
                    <button class="btn btn-danger btn-small" onclick="dashboard.deleteInstance(${inst.id})">
                        Delete
                    </button>
                </div>
                <div class="debug-panel" id="debug-${inst.id}" style="display:none;">
                    <div class="debug-toolbar">
                        <span class="debug-title">Event Log</span>
                        <div class="debug-toolbar-actions">
                            <button class="btn btn-secondary btn-small btn-copy" onclick="dashboard.copyDebug(${inst.id}, this)">Copy</button>
                            <button class="btn btn-secondary btn-small" onclick="dashboard.refreshDebug(${inst.id})">Refresh</button>
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
        const types = {
            1: 'Motion Lighting'
        };
        return types[typeId] || 'Automation';
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
                await api.post(`/instances/${instanceId}/pause`, {
                    duration_minutes: 60
                });
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
        } else {
            this.openDebugPanels.add(instanceId);
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

            output.innerHTML = events.map(evt => {
                const time = new Date(evt.received_at).toLocaleTimeString();
                return `<div class="debug-line">`
                    + `<span class="debug-time">${time}</span> `
                    + `<span class="debug-device">${utils.escapeHtml(evt.device_name || evt.hubitat_device_id)}</span> `
                    + `<span class="debug-event">${utils.escapeHtml(evt.event_type)}</span>`
                    + `<span class="debug-value">= ${utils.escapeHtml(evt.event_value || '')}</span>`
                    + `</div>`;
            }).join('');

            output.scrollTop = output.scrollHeight;
        } catch (error) {
            output.innerHTML = `<span class="debug-error">Error: ${error.message}</span>`;
        }
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
