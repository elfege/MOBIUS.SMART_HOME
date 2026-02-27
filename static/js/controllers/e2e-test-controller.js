/**
 * E2E Test Controller
 *
 * Reusable class that manages a 95% viewport modal for testing
 * an automation instance. Each dashboard card's "Test" button
 * creates a new E2ETestModal instance.
 *
 * Architecture:
 *   - Direct WebSocket to Hub4 (ws://<LAN_IP>/eventsocket)
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

/** Hub4 EventSocket URL (unauthenticated on LAN) */
const HUB4_WS_URL = 'ws://<LAN_IP>/eventsocket';

/** Hub4 base URL for device edit pages */
const HUB4_DEVICE_URL = 'http://<LAN_IP>/device/edit';

/** Max WebSocket reconnect attempts before giving up */
const MAX_WS_RECONNECT = 10;

/** Base delay for WebSocket reconnect (multiplied by attempt #) */
const WS_RECONNECT_BASE_MS = 2000;


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

        /** @type {WebSocket|null} Hub4 EventSocket connection */
        this.hubSocket = null;

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

        /** @type {number} WebSocket reconnect attempt counter */
        this._wsReconnectAttempts = 0;

        /** @type {number|null} WebSocket reconnect timer */
        this._wsReconnectTimer = null;

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

        // Connect real-time channels after device IDs are known
        this._connectHubWebSocket();
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
        // Disconnect WebSocket
        if (this.hubSocket) {
            this.hubSocket.close();
            this.hubSocket = null;
        }
        if (this._wsReconnectTimer) {
            clearTimeout(this._wsReconnectTimer);
            this._wsReconnectTimer = null;
        }

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
                                <span id="e2e-ws-label-${this.instanceId}">Hub4 WS</span>
                            </span>
                            <button class="btn btn-primary btn-small e2e-run-all">Run All Tests</button>
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
    // Hub4 WebSocket (direct to Hubitat EventSocket)
    // =========================================================================

    /**
     * Connect to Hub4's EventSocket for live device state.
     * No auth needed — unauthenticated on LAN.
     * Events are filtered to only process this instance's devices.
     */
    _connectHubWebSocket() {
        if (this.hubSocket) {
            this.hubSocket.close();
        }

        this._appendLog(`Connecting to Hub4 EventSocket...`, 'ws');

        try {
            this.hubSocket = new WebSocket(HUB4_WS_URL);

            this.hubSocket.onopen = () => {
                this._wsReconnectAttempts = 0;
                this._setWsStatus(true);
                this._appendLog('Hub4 WebSocket connected', 'ws');
            };

            this.hubSocket.onmessage = (event) => {
                try {
                    const evt = JSON.parse(event.data);
                    this._handleHubEvent(evt);
                } catch (e) {
                    // Ignore malformed messages
                }
            };

            this.hubSocket.onerror = () => {
                this._setWsStatus(false);
            };

            this.hubSocket.onclose = () => {
                this._setWsStatus(false);
                this._scheduleWsReconnect();
            };
        } catch (e) {
            this._appendLog(`WebSocket connection error: ${e.message}`, 'fail');
            this._setWsStatus(false);
        }
    }

    /**
     * Handle a raw event from Hub4 EventSocket.
     * Only processes events for devices in this instance.
     *
     * Event format from Hubitat:
     *   { deviceId, name, value, displayName, descriptionText, ... }
     */
    _handleHubEvent(evt) {
        const deviceId = String(evt.deviceId || '');
        if (!deviceId || !this._instanceDeviceIds.has(deviceId)) {
            return; // Not our device
        }

        const attrName = evt.name;
        const attrValue = evt.value;

        // Update cached state
        if (!this._deviceStateMap[deviceId]) {
            this._deviceStateMap[deviceId] = {};
        }
        this._deviceStateMap[deviceId][attrName] = attrValue;

        // Update DOM
        this._updateDeviceTile(deviceId, attrName, attrValue);

        // Log to terminal
        const displayName = evt.displayName || `Device ${deviceId}`;
        this._appendLog(
            `${displayName}: ${attrName} = ${attrValue}`,
            'device'
        );
    }

    /**
     * Update WebSocket status indicator in modal header.
     * @param {boolean} connected
     */
    _setWsStatus(connected) {
        const $dot = $(`#e2e-ws-dot-${this.instanceId}`);
        const $label = $(`#e2e-ws-label-${this.instanceId}`);
        if (connected) {
            $dot.removeClass('disconnected').addClass('connected');
            $label.text('Hub4 WS');
        } else {
            $dot.removeClass('connected').addClass('disconnected');
            $label.text('Hub4 WS (disconnected)');
        }
    }

    /**
     * Schedule WebSocket reconnect with exponential backoff.
     */
    _scheduleWsReconnect() {
        if (this._wsReconnectAttempts >= MAX_WS_RECONNECT) {
            this._appendLog('Hub4 WebSocket: max reconnect attempts reached', 'fail');
            return;
        }
        if (!this.modalEl) return; // Modal closed

        this._wsReconnectAttempts++;
        const delay = WS_RECONNECT_BASE_MS * this._wsReconnectAttempts;
        this._appendLog(
            `Hub4 WebSocket reconnecting in ${delay / 1000}s (attempt ${this._wsReconnectAttempts})`,
            'warning'
        );

        this._wsReconnectTimer = setTimeout(() => {
            if (this.modalEl) {
                this._connectHubWebSocket();
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
        } else if (type === 'scenario_complete') {
            this._updateScenarioSummary(data);
            const status = data.failed > 0 ? 'fail' : 'pass';
            this._appendLog(
                `--- Complete: ${data.passed}/${data.total} passed, `
                + `${data.failed} failed, ${data.skipped} skipped ---`,
                status
            );
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

                // Cache initial state
                this._deviceStateMap[did] = attrs;

                html += `<div class="e2e-device-tile" data-device-id="${did}">`;
                html += `  <a class="e2e-hub-link" href="${HUB4_DEVICE_URL}/${did}" target="_blank" rel="noopener" title="Open in Hubitat"></a>`;
                html += `  <div class="e2e-device-header">`;
                html += `    <span class="e2e-device-name" title="${utils.escapeHtml(label)}">${utils.escapeHtml(label)}</span>`;
                html += `    <span class="e2e-device-id">#${did}</span>`;
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
     * Called when a real-time event arrives from Hub4 WebSocket.
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

        try {
            await api.post(`/e2e/test/${this.instanceId}/run/${scenarioId}`);
        } catch (err) {
            this._appendLog(`Failed to start scenario: ${err.message}`, 'fail');
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

        try {
            await api.post(`/e2e/test/${this.instanceId}/run-all`);
        } catch (err) {
            this._appendLog(`Failed to start tests: ${err.message}`, 'fail');
        }
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
        const time = new Date().toLocaleTimeString();
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
