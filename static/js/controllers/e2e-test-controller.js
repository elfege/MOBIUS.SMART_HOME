/**
 * E2E Test Controller
 *
 * Reusable class that manages a 95% viewport modal for testing
 * an automation instance. Each dashboard card's "Test" button
 * creates a new E2ETestModal instance.
 *
 * Architecture:
 *   - Per-hub EventSocket WebSockets (one per distinct hub the
 *     instance's selected devices live on, ws://<hub_ip>/eventsocket)
 *     for live device state updates. No auth token needed (LAN-only).
 *   - SSE from backend (/api/e2e/events/stream) for test execution
 *     progress (step start/complete, scenario summaries).
 *   - REST calls to backend for triggering tests and loading data.
 *     All Hubitat Maker API tokens stay server-side, never exposed.
 *
 * Usage:
 *   const modal = new E2ETestModal(2, 'Office Lights');
 *   modal.open();
 */

import { api, utils } from '../main.js';

// Per-hub EventSocket — one WebSocket per distinct hub the instance's
// devices live on. The previous hardcoded HUB4_WS_URL only ever saw
// events for devices natively paired with hub4 (<LAN_IP>); after
// the multi-hub canonical-PK refactor, instances commonly span Home 1/2/3
// and never hit Hub 4 at all, leaving the e2e modal blind to live state.
//
// EventSocket is unauthenticated on LAN, so the URL only needs the hub IP.

/** Max WebSocket reconnect attempts before giving up — applied per hub. */
const MAX_WS_RECONNECT = 10;

/** Base delay for WebSocket reconnect (multiplied by attempt #), per hub. */
const WS_RECONNECT_BASE_MS = 2000;

/** Build the hub-specific EventSocket URL. */
function _hubWsUrl(hubIp) {
    return `ws://${hubIp}/eventsocket`;
}

/** Build the hub-specific device-edit URL for a Hubitat per-hub id. */
function _hubDeviceEditUrl(hubIp, hubitatId) {
    return `http://${hubIp}/device/edit/${hubitatId}`;
}


/**
 * Static cache: persists log lines and scenario results across
 * close/reopen cycles. Keyed by instanceId.
 * Structure: { [instanceId]: { logLines: [], scenarioResults: {} } }
 */
const _persistedState = {};

export class E2ETestModal {
    /**
     * @param {number} instanceId - App instance ID
     * @param {string} instanceLabel - Display label for the instance
     */
    constructor(instanceId, instanceLabel) {
        this.instanceId = instanceId;
        this.instanceLabel = instanceLabel;

        /**
         * Per-hub EventSocket connections, keyed by hub_ip.
         * One WebSocket per distinct hub that owns any of this instance's
         * devices. Populated in _connectHubWebSockets() after _loadDevices.
         * @type {Object<string, WebSocket>}
         */
        this.hubSockets = {};

        /**
         * Per-hub reconnect counters, keyed by hub_ip.
         * @type {Object<string, number>}
         */
        this._hubReconnectAttempts = {};

        /**
         * Reverse-lookup maps used by the WebSocket message handler:
         *   _hubitatToCanon[hub_ip][hubitat_id] = { canonical_id, label }
         * Built once at modal open via /api/canonical-devices, scoped to
         * just this instance's selections.
         * @type {Object<string, Object<string, {canonical_id: string, label: string}>>}
         */
        this._hubitatToCanon = {};

        /** @type {EventSource|null} Backend SSE connection */
        this.eventSource = null;

        /** @type {HTMLElement|null} Modal root element */
        this.modalEl = null;

        /** @type {Object} Device categories from backend */
        this.devices = {};

        /** @type {Array} Test scenario definitions */
        this.scenarios = [];

        /** @type {Object} Cached device state: { deviceId: { attrName: value } } */
        this._deviceStateMap = {};

        /** @type {Set<string>} All device IDs for this instance (for WS filtering) */
        this._instanceDeviceIds = new Set();

        /**
         * Per-hub reconnect timers, keyed by hub_ip. Used so close() can
         * clear them all when the modal goes away.
         * @type {Object<string, number>}
         */
        this._hubReconnectTimers = {};

        // Ensure persisted state bucket exists for this instance
        if (!_persistedState[this.instanceId]) {
            _persistedState[this.instanceId] = { logLines: [], scenarioResults: {} };
        }
    }

    /**
     * Open the modal: create DOM, load data, connect real-time channels.
     */
    async open() {
        this._createModal();
        this._bindEvents();

        // Load devices and scenarios in parallel
        await Promise.all([
            this._loadDevices(),
            this._loadScenarios()
        ]);

        // Restore any persisted log lines and scenario results from prior session
        this._restorePersistedState();

        // Connect real-time channels after device IDs are known.
        // _connectHubWebSockets resolves canonical PKs → per-hub
        // (hub_ip, hubitat_id) tuples and opens one EventSocket per hub.
        this._connectHubWebSockets();
        this._connectSSE();
    }

    /**
     * Restore persisted log lines and scenario step results from a
     * prior modal session. This keeps test output visible across
     * close/reopen cycles without losing history.
     */
    _restorePersistedState() {
        const state = _persistedState[this.instanceId];
        if (!state) return;

        // Restore log lines
        if (state.logLines.length > 0) {
            const $log = $(`#e2e-log-${this.instanceId}`);
            if ($log.length > 0) {
                // Insert a separator so the user can see where old log ends
                $log.append(
                    '<div class="e2e-log-line e2e-log-info">'
                    + '<span class="e2e-log-time">---</span> '
                    + '(restored from previous session)'
                    + '</div>'
                );
                $log.append(state.logLines.join(''));
                const logEl = $log[0];
                logEl.scrollTop = logEl.scrollHeight;
            }
        }

        // Restore scenario step indicators and summaries
        const results = state.scenarioResults;
        for (const [scenarioId, scenarioData] of Object.entries(results)) {
            // Restore individual step indicators
            for (const [stepIdx, stepData] of Object.entries(scenarioData.steps)) {
                const $step = $(`#e2e-step-${scenarioId}-${stepIdx}`);
                if ($step.length === 0) continue;

                $step.find('.e2e-step-indicator')
                    .removeClass('e2e-pending e2e-running e2e-pass e2e-fail e2e-skip')
                    .addClass(`e2e-${stepData.result}`);
                if (stepData.message) {
                    $step.find('.e2e-step-message').text(stepData.message);
                }
            }

            // Restore scenario summary
            if (scenarioData.summary) {
                const data = scenarioData.summary;
                const $summary = $(`#e2e-summary-${scenarioId}`);
                if (data.failed > 0) {
                    $summary.html(
                        `<span class="e2e-result-fail">${data.passed}/${data.total} passed</span>`
                    );
                } else {
                    $summary.html(
                        `<span class="e2e-result-pass">${data.passed}/${data.total} passed</span>`
                    );
                }
            }
        }
    }

    /**
     * Close the modal: disconnect everything, remove DOM, clean up.
     */
    close() {
        // Disconnect every per-hub WebSocket and clear all reconnect timers.
        for (const ws of Object.values(this.hubSockets || {})) {
            try { ws.close(); } catch (_) { /* already closed */ }
        }
        this.hubSockets = {};
        for (const t of Object.values(this._hubReconnectTimers || {})) {
            clearTimeout(t);
        }
        this._hubReconnectTimers = {};

        // Disconnect SSE
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }

        // Remove Escape key handler
        $(document).off('keydown.e2e');

        // Remove global command functions
        delete window._e2eCommand;
        delete window._e2eWebhook;
        delete window._e2eRunScenario;

        // Remove DOM
        if (this.modalEl) {
            this.modalEl.remove();
            this.modalEl = null;
        }
    }

    // =========================================================================
    // Modal DOM
    // =========================================================================

    /**
     * Build and insert the modal HTML.
     * 3-panel layout: Devices | Test Scenarios | Terminal Log
     */
    _createModal() {
        const html = `
            <div class="e2e-modal-overlay" id="e2e-modal-${this.instanceId}">
                <div class="e2e-modal">
                    <div class="e2e-modal-header">
                        <h2>E2E Test: ${utils.escapeHtml(this.instanceLabel)}</h2>
                        <div class="e2e-modal-actions">
                            <span class="e2e-ws-status">
                                <span class="e2e-ws-dot" id="e2e-ws-dot-${this.instanceId}"></span>
                                <span id="e2e-ws-label-${this.instanceId}">Hubs …</span>
                            </span>
                            <button class="btn btn-primary btn-small e2e-run-all">Run All Tests</button>
                            <button class="btn btn-danger btn-small e2e-stop" disabled title="Stop the currently-running scenario">Stop</button>
                            <button class="btn btn-secondary btn-small e2e-reset" title="Clear scenario pass/fail markers">Reset Results</button>
                            <button class="btn btn-secondary btn-small e2e-clear-log" title="Clear the terminal log pane">Clear Log</button>
                            <button class="btn btn-secondary btn-small e2e-close">Close</button>
                        </div>
                    </div>
                    <div class="e2e-modal-body">
                        <div class="e2e-panel e2e-devices-panel">
                            <h3>Devices</h3>
                            <div class="e2e-device-groups" id="e2e-devices-${this.instanceId}">
                                <p class="e2e-loading">Loading devices...</p>
                            </div>
                        </div>
                        <div class="e2e-gutter" data-gutter="left"></div>
                        <div class="e2e-panel e2e-tests-panel">
                            <div class="e2e-panel-toolbar">
                                <h3>Test Scenarios</h3>
                                <button class="btn btn-secondary btn-small btn-copy e2e-copy-scenarios">Copy</button>
                            </div>
                            <div class="e2e-scenarios" id="e2e-scenarios-${this.instanceId}">
                                <p class="e2e-loading">Loading scenarios...</p>
                            </div>
                        </div>
                        <div class="e2e-gutter" data-gutter="right"></div>
                        <div class="e2e-panel e2e-log-panel">
                            <div class="e2e-panel-toolbar">
                                <h3>Terminal</h3>
                                <button class="btn btn-secondary btn-small btn-copy e2e-copy-log">Copy</button>
                            </div>
                            <div class="e2e-log" id="e2e-log-${this.instanceId}"></div>
                        </div>
                    </div>
                </div>
            </div>
        `;
        $('body').append(html);
        this.modalEl = document.getElementById(`e2e-modal-${this.instanceId}`);
    }

    /**
     * Bind modal-level event handlers.
     */
    _bindEvents() {
        const $modal = $(this.modalEl);

        // Close button
        $modal.find('.e2e-close').on('click', () => this.close());

        // Run All button
        $modal.find('.e2e-run-all').on('click', () => this._runAllScenarios());

        // Stop button — cancel any in-flight scenario run
        $modal.find('.e2e-stop').on('click', () => this._stopRun());

        // Reset Results — clear pass/fail markers from scenario rows in the UI
        $modal.find('.e2e-reset').on('click', () => this._resetScenarioResults());

        // Clear Log — wipe the terminal pane (does NOT cancel a run)
        $modal.find('.e2e-clear-log').on('click', () => this._clearLog());

        // Backdrop click intentionally disabled — prevents accidental
        // loss of test results. Use the Close button or Escape key instead.

        // Close on Escape key
        $(document).on('keydown.e2e', (e) => {
            if (e.key === 'Escape') this.close();
        });

        // Copy terminal log
        $modal.find('.e2e-copy-log').on('click', (e) => {
            const logEl = document.getElementById(`e2e-log-${this.instanceId}`);
            if (logEl) utils.copyToClipboard(logEl.innerText, e.currentTarget, 'Copy');
        });

        // Copy test scenarios
        $modal.find('.e2e-copy-scenarios').on('click', (e) => {
            const scenariosEl = document.getElementById(`e2e-scenarios-${this.instanceId}`);
            if (scenariosEl) utils.copyToClipboard(scenariosEl.innerText, e.currentTarget, 'Copy');
        });

        // Draggable gutter splitters for panel resizing
        this._initGutterDrag($modal);
    }

    /**
     * Initialize draggable gutter handles between panels.
     * Converts the fixed CSS grid to pixel-based columns on first drag.
     * Stores sizes in localStorage for persistence across opens.
     *
     * @param {jQuery} $modal - The modal jQuery wrapper
     */
    _initGutterDrag($modal) {
        const body = $modal.find('.e2e-modal-body')[0];
        if (!body) return;

        const storageKey = 'e2e-panel-sizes';

        // Restore saved sizes if available
        try {
            const saved = localStorage.getItem(storageKey);
            if (saved) {
                const sizes = JSON.parse(saved);
                if (sizes.length === 3) {
                    body.style.gridTemplateColumns =
                        `${sizes[0]}px 10px 1fr 10px ${sizes[2]}px`;
                }
            }
        } catch (_) { /* ignore corrupt data */ }

        /**
         * Shared drag handler for both mouse and touch events.
         * @param {number} startClientX - Initial pointer X
         * @param {string} side - 'left' or 'right' gutter
         * @param {string} moveEvent - 'mousemove' or 'touchmove'
         * @param {string} endEvent - 'mouseup' or 'touchend'
         */
        const startDrag = (startClientX, side, moveEvent, endEvent) => {
            const panels = body.querySelectorAll('.e2e-panel');
            const leftPanel = panels[0];
            const centerPanel = panels[1];
            const rightPanel = panels[2];

            const startLeftW = leftPanel.getBoundingClientRect().width;
            const startCenterW = centerPanel.getBoundingClientRect().width;
            const startRightW = rightPanel.getBoundingClientRect().width;

            const MIN_W = 150;
            const GUTTER_W = 10;

            const onMove = (e) => {
                const clientX = e.touches ? e.touches[0].clientX : e.clientX;
                const dx = clientX - startClientX;

                let leftW = startLeftW;
                let centerW = startCenterW;
                let rightW = startRightW;

                if (side === 'left') {
                    leftW = Math.max(MIN_W, startLeftW + dx);
                    centerW = Math.max(MIN_W, startCenterW - dx);
                    if (centerW <= MIN_W) {
                        centerW = MIN_W;
                        leftW = startLeftW + startCenterW - MIN_W;
                    }
                } else {
                    centerW = Math.max(MIN_W, startCenterW + dx);
                    rightW = Math.max(MIN_W, startRightW - dx);
                    if (rightW <= MIN_W) {
                        rightW = MIN_W;
                        centerW = startCenterW + startRightW - MIN_W;
                    }
                }

                body.style.gridTemplateColumns =
                    `${leftW}px ${GUTTER_W}px ${centerW}px ${GUTTER_W}px ${rightW}px`;
            };

            const onEnd = () => {
                document.removeEventListener(moveEvent, onMove);
                document.removeEventListener(endEvent, onEnd);
                body.classList.remove('e2e-resizing');

                try {
                    const lw = leftPanel.getBoundingClientRect().width;
                    const rw = rightPanel.getBoundingClientRect().width;
                    localStorage.setItem(storageKey, JSON.stringify([
                        Math.round(lw), 0, Math.round(rw)
                    ]));
                } catch (_) { /* storage full — ignore */ }
            };

            body.classList.add('e2e-resizing');
            document.addEventListener(moveEvent, onMove, { passive: false });
            document.addEventListener(endEvent, onEnd);
        };

        // Mouse drag
        $modal.find('.e2e-gutter').on('mousedown', (e) => {
            e.preventDefault();
            startDrag(e.clientX, e.currentTarget.dataset.gutter, 'mousemove', 'mouseup');
        });

        // Touch drag (tablet/phone)
        $modal.find('.e2e-gutter').on('touchstart', (e) => {
            e.preventDefault();
            const touch = e.originalEvent.touches[0];
            startDrag(touch.clientX, e.currentTarget.dataset.gutter, 'touchmove', 'touchend');
        });
    }

    // =========================================================================
    // Data Loading
    // =========================================================================

    /**
     * Load instance's devices with current state from backend.
     * The backend fetches from Hubitat Maker API (token stays server-side).
     */
    async _loadDevices() {
        try {
            const data = await api.get(`/e2e/test/${this.instanceId}/devices`);
            this.devices = data.device_categories || {};

            // Build set of all device IDs for WebSocket event filtering
            this._instanceDeviceIds.clear();
            for (const devices of Object.values(this.devices)) {
                for (const d of devices) {
                    const did = String(d.id);
                    this._instanceDeviceIds.add(did);
                }
            }

            // Resolve hub metadata from the canonical devices table so the
            // device tiles can link to the correct per-device hub edit page
            // (multi-hub aware) AND so _connectHubWebSockets reuses the same
            // map without a second fetch. Keyed by canonical PK because
            // device_selections store canonical PKs post-Phase-5.
            this._canonicalById = {};
            try {
                const all = await api.get('/canonical-devices') || [];
                for (const d of all) {
                    this._canonicalById[String(d.id)] = d;
                }
            } catch (err) {
                this._appendLog(
                    `Could not load canonical devices map: ${err.message}`,
                    'warning'
                );
            }

            this._renderDevices();
            this._appendLog(`Loaded ${this._instanceDeviceIds.size} devices`, 'info');
        } catch (err) {
            $(`#e2e-devices-${this.instanceId}`).html(
                `<p class="e2e-error">Failed to load devices: ${err.message}</p>`
            );
            this._appendLog(`Device load error: ${err.message}`, 'fail');
        }
    }

    /**
     * Load test scenario definitions from backend.
     */
    async _loadScenarios() {
        try {
            this.scenarios = await api.get(`/e2e/test/${this.instanceId}/scenarios`);
            this._renderScenarios();
            this._appendLog(`Loaded ${this.scenarios.length} test scenarios`, 'info');
        } catch (err) {
            $(`#e2e-scenarios-${this.instanceId}`).html(
                `<p class="e2e-error">Failed to load scenarios: ${err.message}</p>`
            );
            this._appendLog(`Scenario load error: ${err.message}`, 'fail');
        }
    }

    // =========================================================================
    // Per-hub EventSockets (multi-hub aware)
    // =========================================================================

    /**
     * Build the hubitat-id → canonical-id reverse-lookup map for this
     * instance's devices, keyed by hub_ip. Then open one EventSocket per
     * distinct hub. Called after _loadDevices() so we know which canonical
     * ids the instance subscribes to.
     */
    async _connectHubWebSockets() {
        // Tear down any stragglers from a previous open.
        for (const ws of Object.values(this.hubSockets || {})) {
            try { ws.close(); } catch (_) {}
        }
        this.hubSockets = {};
        this._hubReconnectAttempts = {};
        this._hubReconnectTimers = this._hubReconnectTimers || {};
        for (const t of Object.values(this._hubReconnectTimers)) clearTimeout(t);
        this._hubReconnectTimers = {};

        // Resolve canonical PKs (from _instanceDeviceIds) → hub_ip + hubitat_id
        // by hitting /api/canonical-devices. Single roundtrip.
        const ids = Array.from(this._instanceDeviceIds);
        if (ids.length === 0) {
            this._setWsStatus(false, 0, 0);
            return;
        }

        let allDevices = [];
        try {
            allDevices = await api.get('/canonical-devices') || [];
        } catch (err) {
            this._appendLog(`Failed to resolve hubs: ${err.message}`, 'fail');
            this._setWsStatus(false, 0, 0);
            return;
        }

        // Build hub_ip → { hubitat_id → {canonical_id, label} } and the
        // distinct list of hubs to connect to.
        this._hubitatToCanon = {};
        const hubIps = new Set();
        const idsAsString = new Set(ids.map(String));
        for (const d of allDevices) {
            if (!idsAsString.has(String(d.id))) continue;
            if (!d.hub_ip || d.hubitat_id == null) continue;
            hubIps.add(d.hub_ip);
            if (!this._hubitatToCanon[d.hub_ip]) this._hubitatToCanon[d.hub_ip] = {};
            this._hubitatToCanon[d.hub_ip][String(d.hubitat_id)] = {
                canonical_id: String(d.id),
                label: d.label,
            };
        }

        if (hubIps.size === 0) {
            this._appendLog(
                `No hub mappings found for instance devices — live state disabled`,
                'warning'
            );
            this._setWsStatus(false, 0, 0);
            return;
        }

        for (const hubIp of hubIps) {
            this._connectOneHub(hubIp);
        }
        // Status text reflects N/M hubs connected (updates per onopen/onclose).
        this._setWsStatus(false, 0, hubIps.size);
    }

    /**
     * Open the EventSocket for one specific hub_ip and wire its handlers.
     * Stored in this.hubSockets so close() can iterate them all.
     */
    _connectOneHub(hubIp) {
        if (this.hubSockets[hubIp]) {
            try { this.hubSockets[hubIp].close(); } catch (_) {}
        }

        this._appendLog(`Connecting to ${hubIp} EventSocket…`, 'ws');

        let ws;
        try {
            ws = new WebSocket(_hubWsUrl(hubIp));
        } catch (e) {
            this._appendLog(`WebSocket error for ${hubIp}: ${e.message}`, 'fail');
            this._refreshWsStatus();
            return;
        }
        this.hubSockets[hubIp] = ws;

        ws.onopen = () => {
            this._hubReconnectAttempts[hubIp] = 0;
            this._appendLog(`${hubIp} EventSocket connected`, 'ws');
            this._refreshWsStatus();
        };

        ws.onmessage = (event) => {
            try {
                const evt = JSON.parse(event.data);
                this._handleHubEvent(evt, hubIp);
            } catch (_) { /* ignore malformed */ }
        };

        ws.onerror = () => {
            this._refreshWsStatus();
        };

        ws.onclose = () => {
            // Drop from active map BEFORE scheduling reconnect so the
            // status indicator reflects reality during the gap.
            if (this.hubSockets[hubIp] === ws) {
                delete this.hubSockets[hubIp];
            }
            this._refreshWsStatus();
            this._scheduleHubReconnect(hubIp);
        };
    }

    /**
     * Handle one raw EventSocket message, scoped to the hub it came from.
     * Filters to events for this instance's devices on THAT hub specifically:
     * mesh-mirror events arriving on a hub other than the device's native
     * hub are ignored here, matching the backend's mesh-mirror-drop policy.
     *
     * Event format from Hubitat:
     *   { deviceId, name, value, displayName, descriptionText, ... }
     */
    _handleHubEvent(evt, hubIp) {
        const hubitatId = String(evt.deviceId || '');
        if (!hubitatId) return;

        const hubMap = this._hubitatToCanon[hubIp];
        const meta = hubMap ? hubMap[hubitatId] : null;
        if (!meta) return; // Not one of our devices on this hub

        const attrName = evt.name;
        const attrValue = evt.value;
        const canonId = meta.canonical_id;

        // Update cached state — keyed by the same id the device tile uses
        // (which is the Maker API per-hub id from _loadDevices).
        if (!this._deviceStateMap[hubitatId]) this._deviceStateMap[hubitatId] = {};
        this._deviceStateMap[hubitatId][attrName] = attrValue;

        // Update DOM (tile keyed on Maker API per-hub id).
        this._updateDeviceTile(hubitatId, attrName, attrValue);

        const displayName = evt.displayName || meta.label || `Device #${canonId}`;
        this._appendLog(
            `${displayName} [canon #${canonId} · ${hubIp} #${hubitatId}]: ${attrName} = ${attrValue}`,
            'device'
        );
    }

    /**
     * Update the WebSocket status indicator in the modal header to reflect
     * the count of currently-connected hubs vs total expected.
     * @param {boolean} _legacyConnected (ignored, retained for callers)
     * @param {number=} connected explicit connected count (optional)
     * @param {number=} total explicit total count (optional)
     */
    _setWsStatus(_legacyConnected, connected, total) {
        const $dot = $(`#e2e-ws-dot-${this.instanceId}`);
        const $label = $(`#e2e-ws-label-${this.instanceId}`);
        if (connected == null) {
            connected = Object.values(this.hubSockets).filter(
                ws => ws && ws.readyState === WebSocket.OPEN
            ).length;
        }
        if (total == null) {
            total = Math.max(
                Object.keys(this.hubSockets).length,
                Object.keys(this._hubitatToCanon || {}).length
            );
        }
        if (connected > 0 && connected === total) {
            $dot.removeClass('disconnected').addClass('connected');
            $label.text(`Hubs ${connected}/${total}`);
        } else if (connected > 0) {
            $dot.removeClass('disconnected').addClass('connected');
            $label.text(`Hubs ${connected}/${total} (partial)`);
        } else {
            $dot.removeClass('connected').addClass('disconnected');
            $label.text(total > 0 ? `Hubs 0/${total} (disconnected)` : 'No hubs');
        }
    }

    /** Recompute the status badge from current socket states. */
    _refreshWsStatus() {
        this._setWsStatus(false);
    }

    /**
     * Schedule a per-hub reconnect with exponential backoff.
     */
    _scheduleHubReconnect(hubIp) {
        const attempts = (this._hubReconnectAttempts[hubIp] || 0) + 1;
        if (attempts > MAX_WS_RECONNECT) {
            this._appendLog(
                `${hubIp} EventSocket: max reconnect attempts reached`, 'fail'
            );
            return;
        }
        if (!this.modalEl) return; // Modal closed

        this._hubReconnectAttempts[hubIp] = attempts;
        const delay = WS_RECONNECT_BASE_MS * attempts;
        this._appendLog(
            `${hubIp} EventSocket reconnecting in ${delay / 1000}s (attempt ${attempts})`,
            'warning'
        );

        this._hubReconnectTimers = this._hubReconnectTimers || {};
        if (this._hubReconnectTimers[hubIp]) {
            clearTimeout(this._hubReconnectTimers[hubIp]);
        }
        this._hubReconnectTimers[hubIp] = setTimeout(() => {
            if (this.modalEl) {
                this._connectOneHub(hubIp);
            }
        }, delay);
    }

    // =========================================================================
    // Backend SSE (test execution progress)
    // =========================================================================

    /**
     * Connect to backend SSE for test step results and scenario progress.
     */
    _connectSSE() {
        if (this.eventSource) {
            this.eventSource.close();
        }

        this.eventSource = new EventSource(
            `/api/e2e/events/stream?instance_id=${this.instanceId}`
        );

        this.eventSource.onopen = () => {
            this._appendLog('Backend SSE connected', 'info');
        };

        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this._handleSSEEvent(data);
            } catch (e) {
                // Ignore parse errors
            }
        };

        this.eventSource.onerror = () => {
            this._appendLog('Backend SSE connection lost, auto-reconnecting...', 'warning');
        };
    }

    /**
     * Handle an SSE event from the backend test runner.
     * @param {Object} data - Event payload
     */
    _handleSSEEvent(data) {
        const type = data.type;

        if (type === 'device_event') {
            // Webhook-routed event — show in terminal
            this._appendLog(
                `[webhook] ${data.device_name || data.device_id}: `
                + `${data.event_name} = ${data.event_value}`,
                'device'
            );
        } else if (type === 'step_start') {
            this._updateStepUI(data.scenario_id, data.step_index, 'running', '');
        } else if (type === 'step_complete') {
            this._updateStepUI(
                data.scenario_id, data.step_index,
                data.result, data.message
            );
            const icon = data.result === 'pass' ? 'PASS'
                       : data.result === 'fail' ? 'FAIL'
                       : 'SKIP';
            this._appendLog(
                `[${icon}] ${data.step_name}: ${data.message} (${data.duration_ms}ms)`,
                data.result
            );
        } else if (type === 'scenario_start') {
            this._appendLog(`--- Starting: ${data.scenario_name} ---`, 'info');
            // Stop is enabled while any scenario is in flight.
            this._setRunning(true);
        } else if (type === 'scenario_complete') {
            this._updateScenarioSummary(data);
            const status = data.failed > 0 ? 'fail' : 'pass';
            this._appendLog(
                `--- Complete: ${data.passed}/${data.total} passed, `
                + `${data.failed} failed, ${data.skipped} skipped ---`,
                status
            );
            // Run-all chains scenario_complete events; the next scenario
            // will re-enable Stop via scenario_start. End-of-run leaves
            // Stop disabled, which is the correct idle state.
            this._setRunning(false);
        } else if (type === 'wait_tick') {
            // Could show countdown in UI; for now just ignore
        }
    }

    // =========================================================================
    // Device Rendering
    // =========================================================================

    /**
     * Render all device tiles grouped by category.
     */
    _renderDevices() {
        const $container = $(`#e2e-devices-${this.instanceId}`);
        let html = '';

        const categoryLabels = {
            motion_sensors: 'Motion Sensors',
            switches: 'Switches',
            pause_buttons: 'Pause Buttons',
            pause_switches: 'Pause Switches',
            contacts: 'Contact Sensors',
            illuminance_sensor: 'Illuminance Sensor'
        };

        for (const [category, devices] of Object.entries(this.devices)) {
            if (!devices || devices.length === 0) continue;

            html += `<div class="e2e-device-group">`;
            html += `<h4 class="e2e-group-title">${categoryLabels[category] || category}</h4>`;

            for (const device of devices) {
                const did = String(device.id);
                const label = device.label || device.name || `Device ${did}`;
                const attrs = this._extractAttributes(device);

                // Per-device hub metadata threaded through by the backend
                // (services/hub_classifier.get_device_by_canonical_id) so
                // the tile can link to the correct hub. Falls back to the
                // Maker API per-hub id if hub info is missing.
                const hubIp = device._hub_ip || '';
                const hubName = device._hub_name || '';
                const hubitatId = String(device._hubitat_id ?? did);
                const canonicalId = device._canonical_id != null ? String(device._canonical_id) : did;
                const linkUrl = hubIp
                    ? _hubDeviceEditUrl(hubIp, hubitatId)
                    : null;

                // Cache initial state
                this._deviceStateMap[did] = attrs;

                html += `<div class="e2e-device-tile" data-device-id="${did}">`;
                if (linkUrl) {
                    html += `  <a class="e2e-hub-link" href="${linkUrl}" target="_blank" rel="noopener" title="Open ${utils.escapeHtml(label)} on ${utils.escapeHtml(hubName || hubIp)}"></a>`;
                }
                html += `  <div class="e2e-device-header">`;
                html += `    <span class="e2e-device-name" title="${utils.escapeHtml(label)}">${utils.escapeHtml(label)}</span>`;
                html += `    <span class="e2e-device-id" title="canon #${canonicalId} · ${hubName || hubIp || 'hub'} #${hubitatId}">#${canonicalId}</span>`;
                html += `  </div>`;
                html += `  <div class="e2e-device-attrs" id="e2e-attrs-${did}">`;
                html += this._renderAttributes(attrs, category);
                html += `  </div>`;

                // Command buttons for controllable devices
                if (category === 'switches' || category === 'pause_switches') {
                    html += `  <div class="e2e-device-controls">`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eCommand('${did}','on')">On</button>`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eCommand('${did}','off')">Off</button>`;

                    // Slider for dimmable devices
                    const caps = device.capabilities || [];
                    const hasDim = caps.some(c =>
                        typeof c === 'string' && c.toLowerCase().includes('switchlevel')
                    );
                    if (hasDim) {
                        html += `    <input type="range" min="0" max="100" value="50" class="e2e-level-slider"
                                       onchange="window._e2eCommand('${did}','setLevel',[parseInt(this.value)])">`;
                    }
                    html += `  </div>`;
                }

                // Webhook injection buttons for sensors
                if (category === 'motion_sensors') {
                    html += `  <div class="e2e-device-controls">`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eWebhook('${did}','motion','active')">Active</button>`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eWebhook('${did}','motion','inactive')">Inactive</button>`;
                    html += `  </div>`;
                }

                // Button event injection
                if (category === 'pause_buttons') {
                    html += `  <div class="e2e-device-controls">`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eWebhook('${did}','held','1')">Held</button>`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eWebhook('${did}','pushed','1')">Pushed</button>`;
                    html += `    <button class="btn btn-small btn-secondary" onclick="window._e2eWebhook('${did}','doubleTapped','1')">DblTap</button>`;
                    html += `  </div>`;
                }

                html += `</div>`; // e2e-device-tile
            }
            html += `</div>`; // e2e-device-group
        }

        $container.html(html);

        // Expose command functions globally for onclick handlers
        window._e2eCommand = (deviceId, command, args) => {
            this._sendCommand(deviceId, command, args);
        };
        window._e2eWebhook = (deviceId, name, value) => {
            this._injectWebhook(deviceId, name, value);
        };
    }

    /**
     * Extract attribute values from a Hubitat device object.
     * Hubitat returns attributes as a list of { name, currentValue }.
     *
     * @param {Object} device - Device data from Hubitat API
     * @returns {Object} { attrName: value }
     */
    _extractAttributes(device) {
        const attrs = {};
        if (Array.isArray(device.attributes)) {
            for (const a of device.attributes) {
                if (a.name) attrs[a.name] = a.currentValue;
            }
        } else if (device.attributes && typeof device.attributes === 'object') {
            Object.assign(attrs, device.attributes);
        }
        return attrs;
    }

    /**
     * Render attribute badges for a device tile.
     * Shows only relevant attributes for the device category.
     *
     * @param {Object} attrs - { attrName: value }
     * @param {string} category - Device category key
     * @returns {string} HTML string
     */
    _renderAttributes(attrs, category) {
        const relevant = {
            motion_sensors: ['motion', 'temperature', 'battery', 'illuminance'],
            switches: ['switch', 'level', 'colorTemperature'],
            pause_buttons: ['numberOfButtons', 'battery'],
            pause_switches: ['switch', 'level'],
            contacts: ['contact', 'battery'],
            illuminance_sensor: ['illuminance']
        };

        const keys = relevant[category] || Object.keys(attrs);
        let html = '';

        for (const key of keys) {
            if (attrs[key] !== undefined && attrs[key] !== null) {
                const stateClass = this._getStateClass(key, attrs[key]);
                html += `<span class="e2e-attr ${stateClass}" data-attr="${key}">`
                      + `${key}: <strong>${attrs[key]}</strong></span>`;
            }
        }
        return html || '<span class="e2e-attr">No data</span>';
    }

    /**
     * Get CSS class for an attribute value (color-coded state).
     */
    _getStateClass(attr, value) {
        if (attr === 'switch') {
            return value === 'on' ? 'e2e-state-on' : 'e2e-state-off';
        }
        if (attr === 'motion') {
            return value === 'active' ? 'e2e-state-active' : 'e2e-state-inactive';
        }
        if (attr === 'contact') {
            return value === 'open' ? 'e2e-state-active' : 'e2e-state-inactive';
        }
        return '';
    }

    /**
     * Update a single device tile's attribute display.
     * Called when a real-time event arrives from any per-hub EventSocket.
     *
     * @param {string} deviceId
     * @param {string} attrName
     * @param {string} attrValue
     */
    _updateDeviceTile(deviceId, attrName, attrValue) {
        const $tile = $(`.e2e-device-tile[data-device-id="${deviceId}"]`);
        if ($tile.length === 0) return;

        const $attr = $tile.find(`[data-attr="${attrName}"]`);
        if ($attr.length > 0) {
            // Update existing attribute badge
            const stateClass = this._getStateClass(attrName, attrValue);
            $attr.removeClass('e2e-state-on e2e-state-off e2e-state-active e2e-state-inactive')
                 .addClass(stateClass)
                 .html(`${attrName}: <strong>${utils.escapeHtml(String(attrValue))}</strong>`);
        } else {
            // New attribute — append
            const stateClass = this._getStateClass(attrName, attrValue);
            $tile.find('.e2e-device-attrs').append(
                `<span class="e2e-attr ${stateClass}" data-attr="${attrName}">`
                + `${attrName}: <strong>${utils.escapeHtml(String(attrValue))}</strong></span>`
            );
        }

        // Flash animation
        $tile.addClass('e2e-flash');
        setTimeout(() => $tile.removeClass('e2e-flash'), 500);
    }

    // =========================================================================
    // Scenario Rendering
    // =========================================================================

    /**
     * Render test scenario cards with step lists.
     */
    _renderScenarios() {
        const $container = $(`#e2e-scenarios-${this.instanceId}`);
        if (!this.scenarios || this.scenarios.length === 0) {
            $container.html(
                '<p class="e2e-empty">No test scenarios available for this instance.</p>'
            );
            return;
        }

        let html = '';
        for (const scenario of this.scenarios) {
            html += `<div class="e2e-scenario" data-scenario-id="${scenario.id}">`;
            html += `  <div class="e2e-scenario-header">`;
            html += `    <div>`;
            html += `      <span class="e2e-scenario-name">${utils.escapeHtml(scenario.name)}</span>`;
            html += `      <span class="e2e-scenario-desc">${utils.escapeHtml(scenario.description)}</span>`;
            html += `    </div>`;
            html += `    <div class="e2e-scenario-actions">`;
            html += `      <span class="e2e-scenario-summary" id="e2e-summary-${scenario.id}"></span>`;
            html += `      <button class="btn btn-primary btn-small" onclick="window._e2eRunScenario('${scenario.id}')">Run</button>`;
            html += `    </div>`;
            html += `  </div>`;
            html += `  <div class="e2e-steps" id="e2e-steps-${scenario.id}">`;
            for (let i = 0; i < scenario.steps.length; i++) {
                const step = scenario.steps[i];
                html += `    <div class="e2e-step" data-step-index="${i}" id="e2e-step-${scenario.id}-${i}">`;
                html += `      <span class="e2e-step-indicator e2e-pending"></span>`;
                html += `      <span class="e2e-step-name">${utils.escapeHtml(step.name)}</span>`;
                html += `      <span class="e2e-step-message"></span>`;
                html += `    </div>`;
            }
            html += `  </div>`;
            html += `</div>`;
        }

        $container.html(html);

        // Expose run function globally
        window._e2eRunScenario = (scenarioId) => {
            this._runScenario(scenarioId);
        };
    }

    /**
     * Update a step's UI indicator and message.
     *
     * @param {string} scenarioId
     * @param {number} stepIndex
     * @param {string} result - 'pending', 'running', 'pass', 'fail', 'skip'
     * @param {string} message
     */
    _updateStepUI(scenarioId, stepIndex, result, message) {
        // Persist step result for survival across close/reopen
        const cache = _persistedState[this.instanceId].scenarioResults;
        if (!cache[scenarioId]) cache[scenarioId] = { steps: {}, summary: null };
        cache[scenarioId].steps[stepIndex] = { result, message };

        const $step = $(`#e2e-step-${scenarioId}-${stepIndex}`);
        if ($step.length === 0) return;

        const $indicator = $step.find('.e2e-step-indicator');
        $indicator
            .removeClass('e2e-pending e2e-running e2e-pass e2e-fail e2e-skip')
            .addClass(`e2e-${result}`);

        if (message) {
            $step.find('.e2e-step-message').text(message);
        }

        // Scroll to current step
        $step[0].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    /**
     * Update scenario summary after completion.
     * @param {Object} data - { scenario_id, passed, total, failed }
     */
    _updateScenarioSummary(data) {
        // Persist summary for survival across close/reopen
        const cache = _persistedState[this.instanceId].scenarioResults;
        if (!cache[data.scenario_id]) cache[data.scenario_id] = { steps: {}, summary: null };
        cache[data.scenario_id].summary = data;

        const $summary = $(`#e2e-summary-${data.scenario_id}`);
        if (data.failed > 0) {
            $summary.html(
                `<span class="e2e-result-fail">${data.passed}/${data.total} passed</span>`
            );
        } else {
            $summary.html(
                `<span class="e2e-result-pass">${data.passed}/${data.total} passed</span>`
            );
        }
    }

    // =========================================================================
    // Actions (commands, webhooks, test runs)
    // =========================================================================

    /**
     * Send a real command to a device via the backend.
     * The backend calls Hubitat Maker API — token stays server-side.
     *
     * @param {string} deviceId
     * @param {string} command - 'on', 'off', 'setLevel', etc.
     * @param {Array} [args] - Optional command arguments
     */
    async _sendCommand(deviceId, command, args) {
        try {
            await api.post(`/devices/${deviceId}/command`, {
                command: command,
                args: args || null
            });
            this._appendLog(`Sent ${command} to device ${deviceId}`, 'command');
        } catch (err) {
            this._appendLog(`Command failed: ${err.message}`, 'fail');
        }
    }

    /**
     * Inject a synthetic webhook event via the backend.
     * This triggers the full webhook routing pipeline — the same path
     * a real Hubitat event takes.
     *
     * @param {string} deviceId
     * @param {string} name - Event name ('motion', 'held', etc.)
     * @param {string} value - Event value ('active', '1', etc.)
     */
    async _injectWebhook(deviceId, name, value) {
        try {
            await api.post('/webhook/event', {
                deviceId: deviceId,
                name: name,
                value: value,
                displayName: `E2E Inject - ${deviceId}`
            });
            this._appendLog(
                `Injected webhook: ${name}=${value} for device ${deviceId}`,
                'command'
            );
        } catch (err) {
            this._appendLog(`Webhook injection failed: ${err.message}`, 'fail');
        }
    }

    /**
     * Run a single test scenario.
     * Resets step indicators before starting.
     *
     * @param {string} scenarioId
     */
    async _runScenario(scenarioId) {
        // Reset step UI for this scenario
        $(`#e2e-steps-${scenarioId} .e2e-step-indicator`)
            .removeClass('e2e-pass e2e-fail e2e-skip e2e-running')
            .addClass('e2e-pending');
        $(`#e2e-steps-${scenarioId} .e2e-step-message`).text('');
        $(`#e2e-summary-${scenarioId}`).text('');

        this._setRunning(true);
        try {
            await api.post(`/e2e/test/${this.instanceId}/run/${scenarioId}`);
        } catch (err) {
            this._appendLog(`Failed to start scenario: ${err.message}`, 'fail');
            this._setRunning(false);
        }
    }

    /**
     * Run all test scenarios sequentially.
     * Resets all step indicators before starting.
     */
    async _runAllScenarios() {
        // Reset all step UIs
        $(this.modalEl).find('.e2e-step-indicator')
            .removeClass('e2e-pass e2e-fail e2e-skip e2e-running')
            .addClass('e2e-pending');
        $(this.modalEl).find('.e2e-step-message').text('');
        $(this.modalEl).find('.e2e-scenario-summary').text('');

        this._setRunning(true);
        try {
            await api.post(`/e2e/test/${this.instanceId}/run-all`);
        } catch (err) {
            this._appendLog(`Failed to start tests: ${err.message}`, 'fail');
            this._setRunning(false);
        }
    }

    /**
     * Toggle Run-All disabled / Stop enabled while a run is in flight.
     * The SSE 'scenario_complete' / 'all_complete' / 'cancelled' handlers
     * call this with false to flip back when the run unwinds.
     */
    _setRunning(isRunning) {
        const $modal = $(this.modalEl);
        $modal.find('.e2e-run-all').prop('disabled', isRunning);
        $modal.find('.e2e-stop').prop('disabled', !isRunning);
    }

    /**
     * Cancel any currently-running scenario via the backend stop endpoint.
     * Backend sets the runner's cancel flag; the next step boundary will
     * mark remaining steps as SKIP and emit scenario_complete.
     */
    async _stopRun() {
        try {
            const res = await api.post(`/e2e/test/${this.instanceId}/stop`);
            if (res?.stopped) {
                this._appendLog('Stop requested — current step will finish, then cancel', 'warning');
            } else {
                this._appendLog('No active run to stop', 'info');
                this._setRunning(false);
            }
        } catch (err) {
            this._appendLog(`Stop failed: ${err.message}`, 'fail');
        }
    }

    /**
     * Reset all scenario pass/fail markers in the UI without touching
     * the backend or the log pane. Use to clear visual noise before a
     * fresh run.
     */
    _resetScenarioResults() {
        const $modal = $(this.modalEl);
        $modal.find('.e2e-step-indicator')
            .removeClass('e2e-pass e2e-fail e2e-skip e2e-running')
            .addClass('e2e-pending');
        $modal.find('.e2e-step-message').text('');
        $modal.find('.e2e-scenario-summary').text('');
        // Wipe the persisted scenarioResults too so the markers don't
        // come back when the modal is closed/reopened.
        const state = _persistedState[this.instanceId];
        if (state) state.scenarioResults = {};
        this._appendLog('Scenario results reset', 'info');
    }

    /**
     * Wipe the terminal log pane and the persisted log buffer.
     */
    _clearLog() {
        const logEl = document.getElementById(`e2e-log-${this.instanceId}`);
        if (logEl) logEl.innerHTML = '';
        const state = _persistedState[this.instanceId];
        if (state) state.logLines = [];
    }

    // =========================================================================
    // Terminal Log
    // =========================================================================

    /**
     * Append a line to the terminal log panel.
     *
     * @param {string} message - Log message
     * @param {string} type - CSS class suffix: 'info', 'pass', 'fail',
     *                        'warning', 'device', 'command', 'ws'
     */
    _appendLog(message, type) {
        // HH:MM:SS.mmm — milliseconds matter for ordering closely-spaced
        // events (e.g. mesh-mirror duplicates that arrive within the same
        // second).
        const _now = new Date();
        const time =
            _now.toLocaleTimeString([], { hour12: false })
            + '.' + String(_now.getMilliseconds()).padStart(3, '0');
        const cssClass = `e2e-log-${type}`;
        const lineHtml =
            `<div class="e2e-log-line ${cssClass}">`
            + `<span class="e2e-log-time">${time}</span> `
            + `${utils.escapeHtml(message)}`
            + `</div>`;

        // Persist for survival across close/reopen
        _persistedState[this.instanceId].logLines.push(lineHtml);

        const $log = $(`#e2e-log-${this.instanceId}`);
        if ($log.length === 0) return;

        $log.append(lineHtml);

        // Auto-scroll to bottom
        const logEl = $log[0];
        logEl.scrollTop = logEl.scrollHeight;
    }
}
