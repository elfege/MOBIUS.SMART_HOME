/**
 * Matter Device Management Controller
 *
 * Handles the Matter management page: server status, commissioning,
 * node listing, Hubitat-to-Matter device mapping, and Hubitat
 * Matter device discovery with auto-commissioning.
 */

import { openDeviceRefreshModal } from '../components/device-refresh-modal.js';

$(document).ready(function () {
    // =========================================================================
    // State
    // =========================================================================

    let matterNodes = [];
    let hubitatDevices = [];
    let mappings = [];
    let discoveredDevices = [];

    // =========================================================================
    // Initialization
    // =========================================================================

    loadAll();

    $('#btn-refresh-status').on('click', loadStatus);
    $('#btn-matter-start').on('click', function () { matterService('start'); });
    $('#btn-matter-restart').on('click', function () { matterService('restart'); });
    $('#btn-matter-stop').on('click', function () { matterService('stop'); });
    $('#btn-refresh-nodes').on('click', loadNodes);
    $('#btn-commission').on('click', commissionDevice);
    $('#btn-create-mapping').on('click', createMapping);
    $('#btn-scan-hubs').on('click', scanHubs);
    $('#btn-commission-all').on('click', function () { commissionAll(); });
    $('#btn-remove-all-discovered').on('click', function () { removeAllDiscovered(false); });
    $('#btn-force-remove-all-discovered').on('click', function () { removeAllDiscovered(true); });
    $('#btn-decommission-all-nodes').on('click', function () { decommissionAll(); });
    $('#btn-refresh-discovered').on('click', loadDiscoveredDevices);

    // Canonical device-cache refresh (separate from the Matter-side
    // 'Refresh' which reloads the discovered-devices list). Opens the
    // global refresh modal — operator enters a device # or 0 for all.
    $('#btn-refresh-canonical').on('click', () => openDeviceRefreshModal());

    // =========================================================================
    // Data Loading
    // =========================================================================

    /**
     * Load all data in parallel: status, nodes, mappings, Hubitat devices, discovered.
     */
    function loadAll() {
        loadStatus();
        loadNodes();
        loadMappings();
        loadHubitatDevices();
        loadDiscoveredDevices();
    }

    /**
     * Fetch matter-server connection status and render the status panel.
     */
    function loadStatus() {
        // Immediate feedback so Refresh isn't perceived as doing nothing while
        // the request is in flight (fixes the "not as soon as I hit refresh"
        // lag). Replaced by the real state when the calls resolve.
        $('#status-panel').html(
            '<div class="status-item"><span class="status-dot checking"></span>' +
            '<span class="status-value">Checking…</span></div>'
        );
        $.getJSON('/api/matter/status')
            .done(function (data) {
                const $panel = $('#status-panel');
                const dot = data.connected ? 'connected' : 'disconnected';
                const label = data.connected ? 'Connected' : 'Disconnected';
                let html = `
                    <div class="status-item">
                        <span class="status-dot ${dot}"></span>
                        <span class="status-label">Status:</span>
                        <span class="status-value">${label}</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">URL:</span>
                        <span class="status-value">${data.url}</span>
                    </div>
                `;
                if (data.server_info) {
                    const info = data.server_info;
                    if (info.fabric_id !== undefined) {
                        html += `
                            <div class="status-item">
                                <span class="status-label">Fabric:</span>
                                <span class="status-value">${info.fabric_id}</span>
                            </div>
                        `;
                    }
                    if (info.sdk_version) {
                        html += `
                            <div class="status-item">
                                <span class="status-label">SDK:</span>
                                <span class="status-value">${info.sdk_version}</span>
                            </div>
                        `;
                    }
                }
                $panel.html(html);

                // Bind copy on click for status values
                $panel.find('.status-value').addClass('copyable').on('click', function () {
                    copyToClipboard($(this).text().trim(), this);
                });

                // Fold in the self-healing watchdog health (connection kept
                // alive + per-node reachability + stale removal candidates).
                loadWatchdogHealth($panel);
            })
            .fail(function () {
                $('#status-panel').html(
                    '<div class="status-item"><span class="status-dot disconnected"></span>' +
                    '<span class="status-value">Cannot reach API</span></div>'
                );
            });
    }

    /**
     * Append the Matter self-healing watchdog health to the status panel:
     * whether the watchdog is running, node reachability (available vs stale),
     * removal-candidate nodes (persistently dead — surfaced for decommission),
     * and the last error. Best-effort + read-only; silent if unavailable.
     */
    function loadWatchdogHealth($panel) {
        $.getJSON('/api/matter/watchdog').done(function (w) {
            if (!w || !w.running) { return; }
            const unreachable = w.nodes_unavailable || [];
            const removal = w.removal_candidates || [];
            let html = `
                <div class="status-item">
                    <span class="status-label">Watchdog:</span>
                    <span class="status-value">running</span>
                </div>
                <div class="status-item">
                    <span class="status-label">Nodes:</span>
                    <span class="status-value">${w.nodes_available}/${w.nodes_total} reachable</span>
                </div>`;
            if (removal.length) {
                html += `
                <div class="status-item">
                    <span class="status-dot disconnected"></span>
                    <span class="status-label">Stale (remove):</span>
                    <span class="status-value">${removal.join(', ')}</span>
                </div>`;
            } else if (unreachable.length) {
                html += `
                <div class="status-item">
                    <span class="status-label">Unreachable:</span>
                    <span class="status-value">${unreachable.join(', ')}</span>
                </div>`;
            }
            if (w.last_error) {
                html += `
                <div class="status-item">
                    <span class="status-label">Last error:</span>
                    <span class="status-value">${w.last_error}</span>
                </div>`;
            }
            $panel.append(html);
        });
    }

    /**
     * Fetch commissioned Matter nodes and render the nodes grid.
     */
    function loadNodes() {
        $.getJSON('/api/matter/nodes')
            .done(function (data) {
                matterNodes = Array.isArray(data) ? data : [];
                renderNodes();
                populateMatterDropdown();
            })
            .fail(function (xhr) {
                const msg = xhr.status === 503
                    ? 'matter-server not reachable. Is the container running?'
                    : 'Failed to load Matter nodes';
                $('#nodes-container').html(
                    `<div class="matter-empty">${msg}</div>`
                );
            });
    }

    /**
     * Fetch existing Hubitat-to-Matter mappings.
     */
    function loadMappings() {
        $.getJSON('/api/matter/map')
            .done(function (data) {
                mappings = Array.isArray(data) ? data : [];
                renderMappings();
            })
            .fail(function () {
                $('#mappings-container').html(
                    '<div class="matter-empty">Failed to load mappings</div>'
                );
            });
    }

    /**
     * Fetch all Hubitat devices for the mapping dropdown.
     */
    function loadHubitatDevices() {
        $.getJSON('/api/devices')
            .done(function (data) {
                hubitatDevices = Array.isArray(data) ? data : [];
                populateHubitatDropdown();
            })
            .fail(function () {
                console.warn('Failed to load Hubitat devices');
            });
    }

    // =========================================================================
    // Rendering
    // =========================================================================

    /** Compact "Nm/Nh/Nd ago" from an age in ms (for the unresponsive badge). */
    function _timeAgoShort(ms) {
        if (ms == null || ms < 0) return 'unknown';
        const m = Math.floor(ms / 60000);
        if (m < 1) return 'just now';
        if (m < 60) return `${m}m ago`;
        const h = Math.floor(m / 60);
        if (h < 24) return `${h}h ago`;
        return `${Math.floor(h / 24)}d ago`;
    }

    /**
     * Responsiveness verdict from a matched device's is_online + last_seen_at.
     * stale = offline or no sighting >30m; longGone = offline or >6h.
     */
    function _staleness(isOnline, lastSeenAt) {
        const lastSeen = lastSeenAt ? new Date(lastSeenAt).getTime() : null;
        const ageMs = lastSeen !== null ? (Date.now() - lastSeen) : null;
        const STALE_MS = 30 * 60 * 1000, LONG_MS = 6 * 60 * 60 * 1000;
        const offline = isOnline === false;
        // Online = responsive; only fall back to last_seen when liveness unknown.
        const unknownLive = isOnline == null;
        return {
            stale: offline || (unknownLive && ageMs !== null && ageMs > STALE_MS),
            longGone: offline || (unknownLive && ageMs !== null && ageMs > LONG_MS),
            offline,
            seenAgo: ageMs !== null ? _timeAgoShort(ageMs) : 'unknown',
        };
    }

    /**
     * Render the Matter nodes grid.
     */
    function renderNodes() {
        const $container = $('#nodes-container');

        if (matterNodes.length === 0) {
            $container.html(
                '<div class="matter-empty">No commissioned Matter devices yet</div>'
            );
            return;
        }

        let html = '<div class="nodes-grid">';
        for (const node of matterNodes) {
            const nodeId = node.node_id || node.nodeId || '?';
            const name = extractNodeName(node) || `Node ${nodeId}`;
            const mapped = mappings.find(m => m.matter_node_id === nodeId);

            // Responsiveness: a node is "unresponsive" if its matched Hubitat
            // device is offline, or it hasn't been seen by the matter_discovery
            // scan in a while. last_seen_at is refreshed every scan (~5 min);
            // 30 min with no sighting = stale (escalates the visual at 6h+).
            const lastSeen = node._last_seen_at ? new Date(node._last_seen_at).getTime() : null;
            const ageMs = lastSeen !== null ? (Date.now() - lastSeen) : null;
            const STALE_MS = 30 * 60 * 1000;
            const LONG_MS = 6 * 60 * 60 * 1000;
            // Liveness is authoritative: an ONLINE node is responsive, period —
            // don't let a stale discovery-scan _last_seen_at (which isn't
            // refreshed for commissioned nodes) label a live device
            // "unresponsive 26d ago" (operator report 2026-07-09, node 101).
            // Fall back to last_seen staleness ONLY when liveness is UNKNOWN.
            const offline = node._is_online === false;
            const unknownLive = node._is_online == null;
            const stale = offline || (unknownLive && ageMs !== null && ageMs > STALE_MS);
            const longGone = offline || (unknownLive && ageMs !== null && ageMs > LONG_MS);
            const seenAgo = lastSeen !== null ? _timeAgoShort(ageMs) : 'unknown';

            // Escalating highlight on the tile for unresponsive devices.
            const cardStyle = longGone
                ? 'style="border:1px solid #c0564a;box-shadow:0 0 0 2px rgba(192,86,74,.35);"'
                : (stale ? 'style="border:1px solid #c79a5a;box-shadow:0 0 0 1px rgba(199,154,90,.30);"' : '');
            const staleBadge = stale
                ? `<span class="node-stale-badge" title="No sighting for ${seenAgo}${offline ? ' (device reported offline)' : ''}"
                         style="margin-left:auto;font-size:.72rem;padding:.1rem .45rem;border-radius:4px;background:${longGone ? 'rgba(192,86,74,.25)' : 'rgba(199,154,90,.22)'};color:${longGone ? '#e6857a' : '#d9b277'};">⚠ unresponsive · ${seenAgo}</span>`
                : '';

            // 5.3 — quick link to the CURRENT canonical device on its hub.
            // Resolved server-side via the exact (hub_ip, hubitat_id) anchor
            // (2026-06-19) — NOT the frozen maker-api id that produced the
            // dead "#660" link. node._mapping_stale=true means the node no
            // longer resolves to any present device (re-paired away / removed).
            const hubIp = node._hub_ip;
            const canonHubId = node._canonical_hubitat_id;   // current admin id
            const canonLabel = node._canonical_label;
            let mappedHtml;
            if (node._mapping_stale) {
                mappedHtml = `<div class="node-mapped node-mapped--stale"
                       title="This Matter node no longer resolves to a present Hubitat device (re-paired or removed). Re-map it.">
                       ⚠ mapping stale — no current device</div>`;
            } else if (hubIp && canonHubId) {
                mappedHtml = `<div class="node-mapped">Mapped to
                       <a href="http://${hubIp}/device/edit/${canonHubId}" target="_blank" rel="noopener"
                          title="Open on the Hubitat hub">${escapeHtml(canonLabel || ('#' + canonHubId))} (#${canonHubId}) ↗</a></div>`;
            } else {
                mappedHtml = '';
            }

            html += `
                <div class="node-card${stale ? ' node-card--stale' : ''}" data-node-id="${nodeId}" ${cardStyle}>
                    <div class="node-card-header">
                        <h4 class="node-name">${escapeHtml(name)}</h4>
                        <span class="node-id">ID: ${nodeId}</span>
                        ${staleBadge}
                    </div>
                    <div class="node-details">
                        ${extractNodeDetails(node)}
                    </div>
                    <div class="node-actions">
                        <button class="btn btn-small btn-secondary btn-test-on"
                                data-node-id="${nodeId}">Test ON</button>
                        <button class="btn btn-small btn-secondary btn-test-off"
                                data-node-id="${nodeId}">Test OFF</button>
                    </div>
                    ${mappedHtml}
                </div>
            `;
        }
        html += '</div>';
        $container.html(html);

        // Bind test buttons
        $container.find('.btn-test-on').on('click', function () {
            testMatterCommand($(this).data('node-id'), 'on');
        });
        $container.find('.btn-test-off').on('click', function () {
            testMatterCommand($(this).data('node-id'), 'off');
        });

        // Node staleness (_is_online/_last_seen_at) just became available;
        // refresh the mappings table so its Remove-button highlights reflect
        // responsiveness too (loadAll fetches nodes + mappings in parallel, so
        // mappings may have rendered before nodes were known).
        if (typeof mappings !== 'undefined' && mappings && mappings.length) {
            renderMappings();
        }
    }

    /**
     * Render the mappings table.
     */
    function renderMappings() {
        const $container = $('#mappings-container');

        if (mappings.length === 0) {
            $container.html(
                '<div class="matter-empty">No device mappings yet</div>'
            );
            return;
        }

        let html = `
            <table class="mappings-table">
                <thead>
                    <tr>
                        <th>Hubitat Device</th>
                        <th>Matter Node</th>
                        <th>Endpoint</th>
                        <th>Name</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>
        `;

        for (const m of mappings) {
            // Resolved CURRENT device, computed server-side from
            // matter_node_id → canonical (services.matter_mapping). m.stale
            // means the mapping no longer points at a present device (the
            // #660 / re-paired-away case). The old client-side check compared
            // canonical d.id to the frozen hubitat_device_id — different id
            // spaces — and was structurally wrong; removed.
            const curLabel = m.canonical_label;
            const curHubId = m.canonical_hubitat_id;

            // Correlate to the enriched node (matterNodes carry _is_online /
            // _last_seen_at) for responsiveness. Highlights the Remove button
            // for long-unresponsive devices — the natural "this one's dead,
            // clean it up" affordance.
            const node = matterNodes.find(
                n => (n.node_id || n.nodeId) === m.matter_node_id
            );
            const st = node
                ? _staleness(node._is_online, node._last_seen_at)
                : { stale: false, longGone: false, seenAgo: 'unknown' };
            const rmStyle = st.longGone
                ? 'style="background:#c0564a;border-color:#c0564a;box-shadow:0 0 0 2px rgba(192,86,74,.45);"'
                : (st.stale ? 'style="box-shadow:0 0 0 2px rgba(199,154,90,.5);"' : '');
            const rmTitle = st.stale
                ? `title="Unresponsive — no sighting for ${st.seenAgo}${st.offline ? ' (device offline)' : ''}; consider removing"`
                : '';
            const staleMark = st.stale
                ? ` <span title="unresponsive · ${st.seenAgo}" style="color:${st.longGone ? '#e6857a' : '#d9b277'};">⚠</span>`
                : '';

            html += `
                <tr${(st.longGone || m.stale) ? ' style="background:rgba(192,86,74,.06);"' : ''}>
                    <td>${m.stale
                        ? `<span style="color:#e6857a;" title="No current device resolves for this mapping (re-paired or removed)">⚠ stale</span> <span style="opacity:.55;">(was #${m.hubitat_device_id})</span>`
                        : `${escapeHtml(curLabel || '(unknown)')} <span style="opacity:.55;">(#${curHubId})</span>`}${staleMark}</td>
                    <td>${m.matter_node_id}</td>
                    <td>${m.matter_endpoint_id}</td>
                    <td>${escapeHtml(m.device_name || '')}</td>
                    <td>
                        <button class="btn btn-small btn-danger btn-delete-mapping"
                                data-device-id="${m.hubitat_device_id}" ${rmStyle} ${rmTitle}>Remove</button>
                    </td>
                </tr>
            `;
        }

        html += '</tbody></table>';
        $container.html(html);

        // Bind delete buttons
        $container.find('.btn-delete-mapping').on('click', function () {
            deleteMapping($(this).data('device-id'));
        });
    }

    /**
     * Populate the Hubitat device dropdown for mapping.
     */
    function populateHubitatDropdown() {
        const $select = $('#map-hubitat-device');
        $select.find('option:not(:first)').remove();

        // Sort devices by label/name
        const sorted = [...hubitatDevices].sort((a, b) =>
            (a.label || a.name || '').localeCompare(b.label || b.name || '')
        );

        for (const d of sorted) {
            const label = d.label || d.name || `Device ${d.id}`;
            $select.append(`<option value="${d.id}">${label} (#${d.id})</option>`);
        }
    }

    /**
     * Populate the Matter node dropdown for mapping.
     */
    function populateMatterDropdown() {
        const $select = $('#map-matter-node');
        $select.find('option:not(:first)').remove();

        for (const node of matterNodes) {
            const nodeId = node.node_id || node.nodeId || '?';
            const name = extractNodeName(node) || `Node ${nodeId}`;
            $select.append(`<option value="${nodeId}">${name} (ID: ${nodeId})</option>`);
        }
    }

    // =========================================================================
    // Actions
    // =========================================================================

    /**
     * Stop / start / restart the matter-server container from the UI.
     *
     * Fires POST /api/matter/service/{action}, which drops a trigger the host
     * watcher acts on (the app can't run docker itself). Reports via modal; a
     * 503 means the host watcher isn't installed yet (run ./start.sh once).
     */
    function matterService(action) {
        const Verb = action.charAt(0).toUpperCase() + action.slice(1);
        if (action === 'stop' &&
            !confirm('Stop matter-server?\n\nAll Matter device control and commissioning halt until you start it again.')) {
            return;
        }
        showModal(`${Verb}ing matter-server`,
            `Sending "${action}" to the matter-server container via the host watcher…`, 'loading');
        $.ajax({ url: `/api/matter/service/${action}`, method: 'POST' })
            .done(function (data) {
                showModal(`matter-server ${action} initiated`,
                    (data.message || `matter-server ${action} initiated.`)
                    + '\n\nRefreshing status shortly…', 'success');
                // Give the host watcher time to run docker, then refresh.
                setTimeout(function () { loadStatus(); loadNodes(); }, 8000);
            })
            .fail(function (xhr) {
                showModal(`matter-server ${action} failed`,
                    xhr.responseJSON?.detail || `${Verb} failed`, 'error');
            });
    }

    /**
     * Commission a new Matter device using the entered pairing code.
     */
    function commissionDevice() {
        const code = $('#pairing-code').val().trim();
        if (!code) {
            showCommissionResult('Enter a pairing code first', 'error');
            return;
        }

        showCommissionResult('Commissioning... this may take 30-60 seconds', 'loading');
        $('#btn-commission').prop('disabled', true);

        $.ajax({
            url: '/api/matter/commission',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ code: code }),
            timeout: 120000  // 2 minute timeout for commissioning
        })
        .done(function (data) {
            showCommissionResult(
                `Device commissioned successfully! Node: ${JSON.stringify(data.node)}`,
                'success'
            );
            $('#pairing-code').val('');
            loadNodes();
        })
        .fail(function (xhr) {
            const detail = xhr.responseJSON?.detail || 'Commission failed';
            showCommissionResult(
                detail + ' — if it fails at the certificate step ("No memory" / SendNOC in the matter-server log), '
                + "the device's fabric table is full (~5 max): factory-reset it and use its FACTORY pairing code.",
                'error');
        })
        .always(function () {
            $('#btn-commission').prop('disabled', false);
        });
    }

    /**
     * Create a new Hubitat-to-Matter mapping.
     */
    function createMapping() {
        const hubitatId = $('#map-hubitat-device').val();
        const matterNodeId = $('#map-matter-node').val();

        if (!hubitatId || !matterNodeId) {
            showToast('Select both a Hubitat device and a Matter node', 'error');
            return;
        }

        // Get device name from Hubitat dropdown text
        const deviceName = $('#map-hubitat-device option:selected').text();

        $.ajax({
            url: '/api/matter/map',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                hubitat_device_id: hubitatId,
                matter_node_id: parseInt(matterNodeId),
                device_name: deviceName
            })
        })
        .done(function () {
            loadMappings();
            loadNodes();
            // Reset selects
            $('#map-hubitat-device').val('');
            $('#map-matter-node').val('');
        })
        .fail(function (xhr) {
            showToast('Failed to create mapping: ' + (xhr.responseJSON?.detail || 'Unknown error'), 'error');
        });
    }

    /**
     * Delete a device mapping.
     */
    function deleteMapping(deviceId) {
        // Non-blocking confirmation via modal
        $('#matter-modal-overlay').remove();
        const html = `
            <div id="matter-modal-overlay" class="matter-modal-overlay">
                <div class="matter-modal" data-type="info">
                    <div class="matter-modal-header"><h4>Remove mapping?</h4></div>
                    <div class="matter-modal-body"><p>Remove mapping for Hubitat device #${escapeHtml(String(deviceId))}?</p></div>
                    <div class="matter-modal-footer">
                        <button class="btn btn-small btn-secondary matter-modal-close">Cancel</button>
                        <button class="btn btn-small btn-primary" id="modal-confirm-delete">Remove</button>
                    </div>
                </div>
            </div>`;
        $('body').append(html);
        $('#matter-modal-overlay').on('click', function (e) {
            if ($(e.target).is('#matter-modal-overlay') || $(e.target).is('.matter-modal-close')) {
                $('#matter-modal-overlay').remove();
            }
        });
        $(document).on('keydown.matterModal', function (e) {
            if (e.key === 'Escape') { $('#matter-modal-overlay').remove(); $(document).off('keydown.matterModal'); }
        });
        $('#modal-confirm-delete').on('click', function () {
            $('#matter-modal-overlay').remove();
            $.ajax({
                url: `/api/matter/map/${deviceId}`,
                method: 'DELETE'
            })
            .done(function () {
                loadMappings();
                loadNodes();
            })
            .fail(function (xhr) {
                showToast('Failed to delete mapping: ' + (xhr.responseJSON?.detail || 'Unknown error'), 'error');
            });
        });
    }

    /**
     * Test ON/OFF for a COMMISSIONED node (nodes grid). The node_id is the
     * Matter node itself, so we command it directly over the fabric — no
     * Hubitat resolution, no mapping gate.
     */
    function testMatterCommand(nodeId, command) {
        const node = matterNodes.find(n => (n.node_id || n.nodeId) === nodeId);
        const label = (node && (node._canonical_label || node._device_name || extractNodeName(node)))
            || `node ${nodeId}`;
        sendMatterTest(nodeId, command, label);
    }

    /**
     * Test ON/OFF for a DISCOVERED device card. We test the MATTER device
     * directly, so the only requirement is that it's commissioned into OUR
     * fabric (has our_node_id) — NOT that it has a Hubitat mapping. If it isn't
     * a node yet, we say to Commission it first.
     */
    function testDiscoveredDevice(uniqueId, command) {
        const d = discoveredDevices.find(x => x.unique_id === uniqueId);
        if (!d) return;
        const label = d.device_name || 'device';
        if (d.our_node_id == null) {
            showModal(
                `Can't test "${label}" — not in our Matter fabric yet`,
                `Test commands the MATTER device directly (via matter-server), so it has to be `
                + `commissioned into our fabric first. Hit "Commission" on this card; once it's one `
                + `of our nodes, Test ON/OFF drives it over Matter — no Hubitat mapping required.`,
                'info'
            );
            return;
        }
        sendMatterTest(d.our_node_id, command, label);
    }

    /**
     * Fire an ON/OFF straight at a Matter NODE via matter-server and report the
     * outcome in a modal. Shared by the nodes grid and the discovered cards.
     *
     * This is a Matter-native test (POST /api/matter/nodes/{id}/command →
     * OnOff cluster) — it does NOT go through Hubitat. The loading spinner is
     * held for a minimum of 2s (operator directive) so a fast round-trip still
     * reads as "it did something".
     */
    function sendMatterTest(nodeId, command, label) {
        const CMD = String(command).toUpperCase();
        showModal(`Testing ${CMD} (Matter)`,
            `Commanding the MATTER device "${label}" (node ${nodeId}) directly via matter-server — `
            + `not through Hubitat…`, 'loading');
        // Minimum 2s spinner regardless of how fast the request returns.
        const minSpinner = new Promise(function (resolve) { setTimeout(resolve, 2000); });
        $.ajax({
            url: `/api/matter/nodes/${nodeId}/command`,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ command: command })
        })
        .done(function () {
            minSpinner.then(function () {
                showModal(`Matter ${CMD} sent`,
                    `Sent "${command}" straight to the MATTER device "${label}" (node ${nodeId}) — `
                    + `over the fabric, not via Hubitat.\n\nWatch the physical device to confirm it actuated.`,
                    'success');
            });
        })
        .fail(function (xhr) {
            minSpinner.then(function () {
                showModal(`Matter ${CMD} failed`,
                    `MATTER device "${label}" (node ${nodeId}): ${xhr.responseJSON?.detail || 'Unknown error'}`,
                    'error');
            });
        });
    }

    // =========================================================================
    // Matter Debug Console (services/matter_debug.py surface)
    // =========================================================================

    let _debugLogTimer = null;
    let _debugLogSeq = 0;

    /**
     * Large per-device debug console: matter-server diagnostics, the node's
     * OperationalCredentials fabric table (per-fabric Remove; orphans/current
     * flagged), node reachability + OnOff state, decommission actions, and a
     * live verbose log tail (GET /api/matter/debug/log). Same data agents see.
     */
    function openMatterDebug(nodeId, label) {
        _debugLogSeq = 0;
        $('#matter-debug-overlay').remove();
        $('body').append(`
            <div id="matter-debug-overlay" class="matter-debug-overlay">
              <div class="matter-debug">
                <div class="matter-debug-head">
                  <h3>Matter debug — ${escapeHtml(label || ('node ' + nodeId))}
                      <span class="dbg-mono">(node ${nodeId})</span></h3>
                  <button class="btn btn-small btn-secondary matter-debug-close">Close</button>
                </div>
                <div class="matter-debug-body">
                  <div class="matter-debug-col">
                    <div class="matter-debug-section" id="dbg-server">matter-server: loading…</div>
                    <div class="matter-debug-section" id="dbg-fabrics">Fabrics: loading…</div>
                    <div class="matter-debug-section" id="dbg-node">Node: loading…</div>
                    <div class="matter-debug-actions" id="dbg-actions"></div>
                  </div>
                  <div class="matter-debug-col matter-debug-logcol">
                    <div class="matter-debug-logbar">Live matter-client log
                      <span class="dbg-mono" id="dbg-logstate"></span></div>
                    <pre class="matter-debug-log" id="dbg-log"></pre>
                  </div>
                </div>
              </div>
            </div>`);
        $('#matter-debug-overlay').on('click', function (e) {
            if ($(e.target).is('#matter-debug-overlay') || $(e.target).is('.matter-debug-close')) closeMatterDebug();
        });
        $(document).on('keydown.matterDebug', function (e) { if (e.key === 'Escape') closeMatterDebug(); });

        loadDebugServer();
        loadDebugFabrics(nodeId, label);
        loadDebugNode(nodeId);
        pollDebugLog();
        _debugLogTimer = setInterval(pollDebugLog, 2000);
    }

    function closeMatterDebug() {
        if (_debugLogTimer) { clearInterval(_debugLogTimer); _debugLogTimer = null; }
        $(document).off('keydown.matterDebug');
        $('#matter-debug-overlay').remove();
    }

    function loadDebugServer() {
        $.getJSON('/api/matter/server/diagnostics').done(function (d) {
            const b = d.breaker || {};
            $('#dbg-server').html(
                `<strong>matter-server</strong> — connected: <b>${d.connected}</b> · `
                + `breaker: <b>${b.state || '?'}</b> (fails ${b.failure_count ?? '?'})<br>`
                + `nodes: <b>${d.nodes_available ?? '?'}/${d.nodes_total ?? '?'}</b> reachable`
                + (d.nodes_unavailable && d.nodes_unavailable.length
                    ? ` · <span class="dbg-bad">unreachable: ${d.nodes_unavailable.join(', ')}</span>` : ''));
        }).fail(function (x) { $('#dbg-server').text('server diag failed: ' + (x.responseJSON?.detail || x.statusText)); });
    }

    function loadDebugFabrics(nodeId, label) {
        $.getJSON(`/api/matter/nodes/${nodeId}/fabrics`).done(function (f) {
            const rows = (f.fabrics || []).map(function (fab) {
                const owner = fab.is_current ? '<b class="dbg-cur">CURRENT (ours)</b>'
                    : (fab.is_ours ? '<b class="dbg-bad">ORPHAN (ours)</b>' : 'other');
                const rm = (fab.is_ours && !fab.is_current)
                    ? `<button class="btn btn-small btn-danger dbg-remove-fabric" data-node="${nodeId}" data-idx="${fab.index}">Remove</button>` : '';
                return `<tr><td>${fab.index}</td><td>${fab.vendor_id}</td>`
                    + `<td>${escapeHtml(fab.label || '')}</td><td>${owner}</td><td>${rm}</td></tr>`;
            }).join('');
            $('#dbg-fabrics').html(
                `<strong>Fabric table — ${f.commissioned_fabrics}/${f.max_fabrics}`
                + `${f.full ? ' <span class="dbg-bad">FULL</span>' : ''}</strong> · our orphans: <b>${f.our_orphan_count}</b>`
                + `<table class="dbg-fab-table"><tr><th>idx</th><th>vendor</th><th>label</th><th>owner</th><th></th></tr>${rows}</table>`);
            $('#dbg-actions').html(
                `<button class="btn btn-small btn-danger dbg-decomm" data-node="${nodeId}" data-keep="true"`
                + ` title="Remove only our ORPHANED fabrics — frees slots, keeps the device controllable">Clear our orphans</button>`
                + ` <button class="btn btn-small btn-danger dbg-decomm" data-node="${nodeId}" data-keep="false"`
                + ` title="Remove ALL our fabrics — fully leave this device">Decommission (leave)</button>`);
            $('.dbg-remove-fabric').off('click').on('click', function () { removeFabric($(this).data('node'), $(this).data('idx')); });
            $('.dbg-decomm').off('click').on('click', function () { decommissionNode($(this).data('node'), String($(this).data('keep')) === 'true'); });
        }).fail(function (x) { $('#dbg-fabrics').text('fabric read failed: ' + (x.responseJSON?.detail || x.statusText)); });
    }

    function loadDebugNode(nodeId) {
        $.getJSON(`/api/matter/nodes/${nodeId}/diagnostics`).done(function (d) {
            $('#dbg-node').html(
                `<strong>Node</strong> — available: <b>${d.available}</b> · OnOff: <b>${d.on_off}</b> · `
                + `level: ${d.current_level ?? '—'} · endpoints: ${d.endpoints} · attrs: ${d.attribute_count}`);
        }).fail(function (x) { $('#dbg-node').text('node diag failed: ' + (x.responseJSON?.detail || x.statusText)); });
    }

    function pollDebugLog() {
        $.getJSON(`/api/matter/debug/log?since_seq=${_debugLogSeq}&limit=100`).done(function (d) {
            const $log = $('#dbg-log');
            if (!$log.length) return;
            if (d.records && d.records.length) {
                const el = $log[0];
                const atBottom = el && (el.scrollHeight - el.scrollTop - el.clientHeight < 40);
                d.records.forEach(function (r) {
                    const cls = r.level === 'ERROR' ? 'dbg-bad' : (r.level === 'WARNING' ? 'dbg-warn' : '');
                    $log.append(`<span class="${cls}">${r.ts.slice(11, 19)} ${r.level[0]} ${escapeHtml(r.msg)}</span>\n`);
                });
                _debugLogSeq = d.last_seq;
                if (atBottom && el) el.scrollTop = el.scrollHeight;
            }
            $('#dbg-logstate').text(`seq ${_debugLogSeq}`);
        });
    }

    function removeFabric(nodeId, idx) {
        if (!confirm(`RemoveFabric index ${idx} from node ${nodeId}?\n\nRemote command (no device reset). Removing an ORPHANED fabric frees one slot.`)) return;
        $.ajax({ url: `/api/matter/nodes/${nodeId}/fabrics/${idx}/remove`, method: 'POST', contentType: 'application/json', data: '{}' })
            .done(function () { showToast(`Removed fabric ${idx}`, 'success'); loadDebugFabrics(nodeId); loadDebugServer(); })
            .fail(function (x) { showToast('RemoveFabric failed: ' + (x.responseJSON?.detail || 'error'), 'error'); });
    }

    function decommissionNode(nodeId, keepCurrent) {
        const msg = keepCurrent
            ? `Clear our ORPHANED fabrics from node ${nodeId}?\n\nFrees slots; keeps the device controllable via our current fabric. Remote — no reset.`
            : `FULLY decommission node ${nodeId}?\n\nRemoves ALL our fabrics — we leave the device (it stays on Hubitat/others). Re-commission to control it again.`;
        if (!confirm(msg)) return;
        showModal(`Decommissioning node ${nodeId}`, keepCurrent ? 'Removing orphaned fabrics…' : 'Removing all our fabrics…', 'loading');
        $.ajax({ url: `/api/matter/nodes/${nodeId}/decommission`, method: 'POST', contentType: 'application/json', data: JSON.stringify({ keep_current: keepCurrent }) })
            .done(function (d) {
                const errs = (d.errors && d.errors.length) ? '\nErrors: ' + JSON.stringify(d.errors) : '';
                showModal(`Node ${nodeId} decommission`, `Removed indices: [${(d.removed_indices || []).join(', ') || 'none'}]${errs}`, errs ? 'error' : 'success');
                if ($('#matter-debug-overlay').length) loadDebugFabrics(nodeId);
                loadNodes();
            })
            .fail(function (x) { showModal('Decommission failed', x.responseJSON?.detail || 'error', 'error'); });
    }

    function decommissionAll() {
        if (!confirm('DECOMMISSION ALL — remove OUR fabrics from every commissioned node?\n\nThis is NOT a device reset; it removes MOBIUS fabrics via RemoveFabric. Next dialog picks orphans-only vs full-leave.')) return;
        const keep = confirm('Keep our CURRENT fabric on each device?\n\nOK = clear ORPHANS only (recommended — devices stay controllable).\nCancel = FULLY LEAVE every device.');
        showModal('Decommission all', 'Sweeping every node…', 'loading');
        $.ajax({ url: '/api/matter/decommission-all', method: 'POST', contentType: 'application/json', data: JSON.stringify({ keep_current: keep }) })
            .done(function (d) { showModal('Decommission all complete', `${d.count} nodes processed (keep_current=${d.keep_current}).`, 'success'); loadNodes(); })
            .fail(function (x) { showModal('Decommission all failed', x.responseJSON?.detail || 'error', 'error'); });
    }

    // =========================================================================
    // Discovery
    // =========================================================================

    /**
     * Load previously discovered Hubitat Matter devices from DB.
     */
    function loadDiscoveredDevices() {
        $.getJSON('/api/matter/hubitat-devices')
            .done(function (data) {
                discoveredDevices = Array.isArray(data) ? data : [];
                renderDiscoveredDevices();
            })
            .fail(function () {
                $('#discovered-container').html(
                    '<div class="matter-empty">Failed to load discovered devices</div>'
                );
            });
    }

    /**
     * Remove (or force-remove) a single discovered device. Soft-delete keeps
     * our row (+ id) and marks it removed, so a re-scan brings it back. force
     * skips the fabric decommission (DB-only) for dead/ghost devices.
     */
    function removeDiscovered(uniqueId, name, force) {
        const Verb = force ? 'Force-remove' : 'Remove';
        if (!confirm(
            `${Verb} "${name}"?\n\n`
            + (force
                ? 'DB-only soft-delete (skips decommission — for dead/ghost devices). '
                : 'Decommissions from our fabric, then soft-deletes. ')
            + 'The row is kept — a re-scan brings it back.')) {
            return;
        }
        showModal(`${Verb}ing "${name}"`,
            (force ? 'Soft-deleting (force)' : 'Decommissioning + soft-deleting') + ` "${name}"…`, 'loading');
        $.ajax({
            url: `/api/matter/discovered/${encodeURIComponent(uniqueId)}/remove`,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ force: !!force })
        })
        .done(function (data) {
            const extra = data.decommissioned ? ' Decommissioned from our fabric.'
                : (data.decommission_error ? ` (decommission: ${data.decommission_error})` : '');
            const body = data.purged
                ? `PURGED — the Hubitat device no longer exists (admin API 404), so the row was deleted entirely and won't resurrect on a re-scan.${extra}`
                : `Soft-deleted — the row + our id are kept, so a re-scan brings it back.${extra}`;
            showModal(data.purged ? `Purged "${name}"` : `Removed "${name}"`, body, 'success');
            loadDiscoveredDevices();
            loadNodes();
        })
        .fail(function (xhr) {
            showModal(`${Verb} failed`, xhr.responseJSON?.detail || 'Unknown error', 'error');
        });
    }

    /**
     * Remove (or force-remove) ALL discovered devices at once. Soft-delete —
     * every row kept + marked removed; a re-scan restores them all.
     */
    function removeAllDiscovered(force) {
        const Verb = force ? 'Force-remove ALL' : 'Remove ALL';
        if (!confirm(
            `${Verb} discovered devices?\n\n`
            + 'Soft-deletes every device (rows kept — a re-scan brings them all back). '
            + (force ? 'Force = DB-only, skips decommission.' : 'Decommissions each from our fabric.'))) {
            return;
        }
        showModal(Verb, 'Removing all discovered devices…', 'loading');
        $.ajax({
            url: '/api/matter/discovered/remove-all',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ force: !!force })
        })
        .done(function (data) {
            showModal('Removed all',
                `Soft-deleted ${data.removed}/${data.total} devices (kept for rediscovery).`, 'success');
            loadDiscoveredDevices();
            loadNodes();
        })
        .fail(function (xhr) {
            showModal(`${Verb} failed`, xhr.responseJSON?.detail || 'Unknown error', 'error');
        });
    }

    /**
     * Recommission a device using a MANUAL pairing/setup code. If it's already
     * one of our nodes, decommission it first (a broken-but-in-fabric node has
     * to be recreated), then commission_with_code. The code comes from the
     * device's QR or Hubitat's "Get Setup Code" (paste it fast — the Hubitat
     * pairing window is short-lived).
     */
    function recommissionDevice(uniqueId, name) {
        const d = discoveredDevices.find(x => x.unique_id === uniqueId);
        if (!d) return;
        const code = window.prompt(
            `Recommission "${name}" into our Matter controller.\n\n`
            + `Paste the pairing / setup code (device QR, or Hubitat → "Get Setup Code").\n`
            + (d.our_node_id != null
                ? `It's currently node ${d.our_node_id} — it will be decommissioned first, then re-paired.`
                : `It isn't in our fabric yet — this pairs it in.`));
        if (!code || !code.trim()) return;
        const pairing = code.trim();

        const commission = function () {
            showModal(`Recommissioning "${name}"`,
                `Pairing with code ${pairing}… this can take up to ~60s. Keep the device powered + on the LAN.`,
                'loading');
            $.ajax({
                url: '/api/matter/commission', method: 'POST', contentType: 'application/json',
                data: JSON.stringify({ code: pairing }), timeout: 120000
            })
            .done(function (data) {
                showModal(`Recommissioned "${name}"`,
                    `Now attached to our Matter controller (node ${data.node && (data.node.node_id || JSON.stringify(data.node))}).`,
                    'success');
                loadNodes(); loadDiscoveredDevices(); loadMappings();
            })
            .fail(function (xhr) {
                const detail = xhr.responseJSON?.detail || 'Unknown error';
                showModal('Recommission failed',
                    `${detail}\n\n`
                    + `Most common cause — the device's Matter fabric table is FULL: a Matter device holds only ~5 `
                    + `fabrics (Hubitat + HomeKit + any prior commissions), and the matter-server log shows this as `
                    + `"CHIP Error 0x0B: No memory" at the SendNOC step. Fixes:\n`
                    + `  1. FACTORY-RESET the device (clears every fabric), then commission with its FACTORY pairing `
                    + `code (device label / QR) — NOT "Get Setup Code" (that shares an already-paired device to another `
                    + `controller, so re-adding it to our own fabric fails).\n`
                    + `  2. Or free a slot by removing the device from another controller (e.g. delete it in Hubitat).\n`
                    + `If the node shows "not available", our decommission can't free its slot remotely — factory reset `
                    + `is the reliable path.`,
                    'error');
            });
        };

        if (d.our_node_id != null) {
            showModal(`Recommissioning "${name}"`,
                `Decommissioning node ${d.our_node_id} from our fabric first…`, 'loading');
            $.ajax({
                url: `/api/matter/devices/${d.our_node_id}/remove`, method: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({ reason: 'recommission (manual code)' })
            }).always(commission);   // proceed whether or not decommission fully succeeded
        } else {
            commission();
        }
    }

    /**
     * Trigger a scan of all Hubitat hubs for Matter devices.
     * Shows progress, then refreshes the discovered devices table.
     */
    function scanHubs() {
        const $status = $('#scan-status');
        const $btn = $('#btn-scan-hubs');

        $status.removeClass('success error loading')
            .addClass('loading')
            .text('Scanning all Hubitat hubs for Matter devices...')
            .show();
        $btn.prop('disabled', true);

        $.ajax({
            url: '/api/matter/discover',
            method: 'POST',
            timeout: 60000
        })
        .done(function (data) {
            const count = data.discovered || 0;
            const errCount = data.errors ? data.errors.length : 0;
            let msg = `Discovered ${count} device(s) across hubs.`;
            if (errCount > 0) {
                msg += ` ${errCount} hub(s) had errors.`;
            }
            $status.removeClass('loading').addClass('success').text(msg);
            loadDiscoveredDevices();
            // Also refresh nodes in case some were already commissioned
            loadNodes();

            // Auto-commission all online, uncommissioned devices
            if (count > 0) {
                $status.text(msg + ' Starting auto-commission...');
                commissionAll($status, msg);
            }
        })
        .fail(function (xhr) {
            const detail = xhr.responseJSON?.detail || 'Scan failed';
            $status.removeClass('loading').addClass('error').text(detail);
        })
        .always(function () {
            $btn.prop('disabled', false);
        });
    }

    /**
     * Render the discovered Hubitat Matter devices table.
     * Shows: name, hub, online status, Maker API match, correction dropdown,
     * commission button.
     */
    function renderDiscoveredDevices() {
        const $container = $('#discovered-container');

        if (discoveredDevices.length === 0) {
            $container.html(
                '<div class="matter-empty">No discovered devices yet. Click "Scan All Hubs" to start.</div>'
            );
            return;
        }

        let html = '<div class="device-cards-grid">';

        for (const d of discoveredDevices) {
            const online = d.is_online;
            const statusClass = online ? 'online' : 'offline';
            const statusLabel = online ? 'Online' : 'Offline';
            const isCommissioned = d.is_commissioned;
            const uid = escapeHtml(d.unique_id);
            const name = escapeHtml(d.device_name || 'Unknown');

            // Confidence badge
            const conf = d.match_confidence || 'none';
            const confClass = conf === 'exact' ? 'badge-success'
                            : conf === 'fuzzy' ? 'badge-warning'
                            : conf === 'manual' ? 'badge-info'
                            : 'badge-none';

            // Matched Hubitat device — link straight to its page on the hub
            // (this IS the Hubitat section, so every card links out to Hubitat;
            // operator directive 2026-07-09).
            const matchDisplay = d.maker_api_device_name
                ? `<a class="matter-hub-link" href="http://${escapeHtml(d.hub_ip)}/device/edit/${d.maker_api_device_id}" target="_blank" rel="noopener" title="Open this device on the Hubitat hub">${escapeHtml(d.maker_api_device_name)} (#${d.maker_api_device_id}) ↗</a>`
                : '<span class="text-muted">No match</span>';

            // Actions. Operator directive 2026-07-09: EVERY card shows a
            // Commission button (previously it was hidden for offline devices —
            // they only got Rescan — and for already-commissioned ones, which
            // only showed a node badge). We keep those as *extra* affordances
            // but always expose Commission (labelled "Re-commission" when the
            // device is already in our fabric). Test ON/OFF is on every card too,
            // each reporting its result in a modal.
            let actionHtml = '';
            actionHtml += `<button class="btn btn-small btn-secondary btn-test-discovered"
                            data-uid="${uid}" data-command="on"
                            title="Send ON to this device and report the result">Test ON</button>`;
            actionHtml += `<button class="btn btn-small btn-secondary btn-test-discovered"
                            data-uid="${uid}" data-command="off"
                            title="Send OFF to this device and report the result">Test OFF</button>`;
            if (!online) {
                actionHtml += `<button class="btn btn-small btn-secondary btn-rescan-device"
                                data-unique-id="${uid}" data-hub-ip="${escapeHtml(d.hub_ip)}"
                                data-node-id="${d.hubitat_node_id}"
                                title="Rescan this device's hub to refresh online status">Rescan</button>`;
            }
            actionHtml += `<button class="btn btn-small btn-primary btn-auto-commission"
                            data-unique-id="${uid}" data-device-name="${name}"
                            title="${isCommissioned
                                ? 'Re-commission: open a fresh pairing window and re-pair into our fabric'
                                : (online ? 'Commission into our Matter fabric'
                                          : 'Try commissioning — device is offline, this may fail')}">${isCommissioned ? 'Re-commission' : 'Commission'}</button>`;
            actionHtml += `<button class="btn btn-small btn-secondary btn-recommission-code"
                            data-uid="${uid}" data-name="${name}"
                            title="Recommission with a manual pairing/setup code — decommissions first if it's already a node, then re-pairs">Recommission (code)</button>`;
            actionHtml += `<button class="btn btn-small btn-danger btn-remove-discovered"
                            data-uid="${uid}" data-name="${name}"
                            title="Remove: decommission from our fabric + soft-delete (row kept, a re-scan brings it back)">Remove</button>`;
            actionHtml += `<button class="btn btn-small btn-danger btn-force-remove-discovered"
                            data-uid="${uid}" data-name="${name}"
                            title="Force remove: DB-only soft-delete, skips decommission — for dead/ghost devices">Force</button>`;
            if (isCommissioned && d.our_node_id != null) {
                actionHtml += `<button class="btn btn-small btn-secondary btn-debug-node"
                                data-node="${d.our_node_id}" data-name="${name}"
                                title="Matter debug console — fabric table, per-fabric remove, decommission, live log">Debug</button>`;
            }

            // Detail rows for expandable section
            const mac = d.mac_address ? escapeHtml(d.mac_address) : '<span class="text-muted">unknown</span>';
            const ip = d.ip_address ? escapeHtml(d.ip_address) : '<span class="text-muted">none</span>';
            const fw = d.firmware_version ? escapeHtml(d.firmware_version) : '—';
            const hw = d.hardware_version ? escapeHtml(d.hardware_version) : '—';
            const serial = d.serial_number ? escapeHtml(d.serial_number) : '—';
            const dni = d.hubitat_dni ? escapeHtml(d.hubitat_dni) : '—';
            const lastSeen = d.last_seen_at ? new Date(d.last_seen_at).toLocaleString() : '—';

            // Accordion card: a one-line clickable header (chevron + name +
            // status + optional node badge) over a body that is COLLAPSED by
            // default (`is-collapsed`). No persistence — every render/reload
            // starts collapsed (operator directive 2026-07-09). All actions +
            // info live in the body, where the action row is free to WRAP
            // instead of crushing the name (the earlier ugly per-char wrap).
            html += `
                <div class="device-card ${statusClass} ${isCommissioned ? 'commissioned' : ''} is-collapsed" data-uid="${uid}">
                    <button type="button" class="device-card-header" aria-expanded="false"
                            title="Click to expand / collapse">
                        <span class="device-card-chevron">&#9656;</span>
                        <strong class="device-card-name">${name}</strong>
                        <span class="status-badge ${statusClass}">${statusLabel}</span>
                        ${isCommissioned ? `<span class="badge badge-success" title="In our Matter fabric as node ${d.our_node_id}">Node ${d.our_node_id}</span>` : ''}
                    </button>
                    <div class="device-card-body">
                        <div class="device-card-actions">
                            ${actionHtml}
                        </div>
                        <div class="device-card-summary">
                            <span class="device-card-mfr">${escapeHtml(d.manufacturer || '')} ${escapeHtml(d.model || '')}</span>
                            <span class="device-card-hub">${escapeHtml(d.hub_name || d.hub_ip)}</span>
                            <span class="badge ${confClass}">${conf}</span>
                        </div>
                        <div class="device-card-match">
                            <span class="match-current">${matchDisplay}</span>
                            <button class="btn-link btn-change-match" data-unique-id="${uid}" title="Change match">edit</button>
                        </div>
                        <div class="device-card-details">
                            <div class="detail-toolbar">
                                <button class="btn btn-secondary btn-small btn-copy btn-copy-details" title="Copy details">Copy</button>
                            </div>
                            <table class="detail-table">
                                <tr><td>MAC Address</td><td>${mac}</td></tr>
                                <tr><td>IP Address</td><td>${ip}</td></tr>
                                <tr><td>Unique ID</td><td><code>${uid}</code></td></tr>
                                <tr><td>DNI</td><td>${dni}</td></tr>
                                <tr><td>Hub Node</td><td>${d.hubitat_node_id || '—'}</td></tr>
                                <tr><td>Hubitat Device ID</td><td>${d.hubitat_device_id ? `<a class="matter-hub-link" href="http://${escapeHtml(d.hub_ip)}/device/edit/${d.hubitat_device_id}" target="_blank" rel="noopener" title="Open on the Hubitat hub">${d.hubitat_device_id} ↗</a>` : '—'}</td></tr>
                                <tr><td>Firmware</td><td>${fw}</td></tr>
                                <tr><td>Hardware</td><td>${hw}</td></tr>
                                <tr><td>Serial</td><td>${serial}</td></tr>
                                <tr><td>Last Seen</td><td>${lastSeen}</td></tr>
                                ${isCommissioned ? `<tr><td>Our Node ID</td><td>${d.our_node_id}</td></tr>` : ''}
                            </table>
                        </div>
                    </div>
                </div>
            `;
        }

        html += '</div>';
        $container.html(html);

        // Bind accordion header toggle (expand/collapse the whole card body).
        // No persistence by design — every render/reload starts collapsed.
        $container.find('.device-card-header').on('click', function () {
            const collapsed = $(this).closest('.device-card')
                .toggleClass('is-collapsed').hasClass('is-collapsed');
            $(this).attr('aria-expanded', String(!collapsed));
        });

        // Bind auto-commission buttons
        $container.find('.btn-auto-commission').on('click', function () {
            autoCommission(
                $(this).data('unique-id'),
                $(this).data('device-name'),
                $(this)
            );
        });

        // Bind per-card Test ON/OFF (discovered devices) — modal-reported.
        $container.find('.btn-test-discovered').on('click', function () {
            testDiscoveredDevice($(this).data('uid'), $(this).data('command'));
        });

        // Bind per-card Remove / Force-remove.
        $container.find('.btn-remove-discovered').on('click', function () {
            removeDiscovered($(this).data('uid'), $(this).data('name'), false);
        });
        $container.find('.btn-force-remove-discovered').on('click', function () {
            removeDiscovered($(this).data('uid'), $(this).data('name'), true);
        });
        $container.find('.btn-debug-node').on('click', function () {
            openMatterDebug($(this).data('node'), $(this).data('name'));
        });
        $container.find('.btn-recommission-code').on('click', function () {
            recommissionDevice($(this).data('uid'), $(this).data('name'));
        });

        // Bind rescan buttons (individual offline device rescan)
        $container.find('.btn-rescan-device').on('click', function () {
            rescanDevice($(this).data('unique-id'), $(this));
        });

        // Bind match-change buttons
        $container.find('.btn-change-match').on('click', function () {
            openMatchEditor($(this).data('unique-id'), $(this).closest('.device-card-match'));
        });

        // Bind copy-details buttons
        $container.find('.btn-copy-details').on('click', function () {
            const $table = $(this).closest('.device-card-details').find('.detail-table');
            copyToClipboard($table[0].innerText, this, 'Copy');
        });
    }

    /**
     * Rescan a single device's hub to refresh its online status.
     */
    function rescanDevice(uniqueId, $btn) {
        $btn.prop('disabled', true).text('Scanning...');
        $.ajax({
            url: '/api/matter/discover',
            method: 'POST',
            timeout: 60000
        })
        .done(function () {
            loadDiscoveredDevices();
        })
        .fail(function () {
            showToast('Rescan failed', 'error');
        })
        .always(function () {
            $btn.prop('disabled', false).text('Rescan');
        });
    }

    /**
     * Auto-commission a single device: opens Hubitat pairing window,
     * commissions into our fabric, creates mapping.
     */
    function autoCommission(uniqueId, deviceName, $btn) {
        $btn.prop('disabled', true).text('Commissioning...');

        showModal(
            `Commissioning "${deviceName}"`,
            '1. Opening pairing window on Hubitat...\n2. Commissioning into our Matter controller...\n3. Linking to the Hubitat device...',
            'loading'
        );

        $.ajax({
            url: '/api/matter/auto-commission',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ unique_id: uniqueId }),
            timeout: 180000  // 3 min timeout for commissioning
        })
        .done(function (data) {
            $btn.replaceWith(
                `<span class="badge badge-success">Commissioned (Node ${data.our_node_id || '?'})</span>`
            );
            showModal(
                `Commissioned "${deviceName}"`,
                `Node ID: ${data.our_node_id || '?'}\nMapped to Hubitat #${data.hubitat_device_id || '?'}`,
                'success'
            );
            loadNodes();
            loadMappings();
            loadDiscoveredDevices();
        })
        .fail(function (xhr) {
            const detail = xhr.responseJSON?.detail || 'Commission failed';
            showModal(`Commission failed: ${deviceName}`, detail, 'error');
            $btn.prop('disabled', false).text('Commission');
        });
    }

    /**
     * Commission ALL online, uncommissioned devices in bulk.
     * Called automatically after scan, or manually via button.
     * @param {jQuery} $status - optional status element to update progress
     * @param {string} baseMsg - optional base message to prepend to status
     */
    function commissionAll($status, baseMsg) {
        const statusEl = $status || $('#scan-status');
        const prefix = baseMsg || '';

        statusEl.removeClass('success error')
            .addClass('loading')
            .text(prefix + ' Commissioning all online devices...')
            .show();

        $.ajax({
            url: '/api/matter/auto-commission-all',
            method: 'POST',
            timeout: 600000  // 10 min — commissioning each device takes time
        })
        .done(function (data) {
            let msg = prefix ? prefix + ' ' : '';
            msg += data.message;
            if (data.failed > 0) {
                msg += ` (${data.failed} failed)`;
                statusEl.removeClass('loading').addClass('success').text(msg);
                // Log failures to console for debugging
                console.warn('Commission failures:', data.results.filter(r => r.status === 'error'));
            } else {
                statusEl.removeClass('loading').addClass('success').text(msg);
            }
            // Refresh everything
            loadNodes();
            loadMappings();
            loadDiscoveredDevices();
        })
        .fail(function (xhr) {
            const detail = xhr.responseJSON?.detail || 'Bulk commission failed';
            statusEl.removeClass('loading').addClass('error')
                .text(prefix + ' Commission error: ' + detail);
        });
    }

    /**
     * Open inline match editor: replaces the match cell with a dropdown
     * of all Maker API devices for manual correction.
     */
    function openMatchEditor(uniqueId, $cell) {
        if (hubitatDevices.length === 0) {
            showToast('Hubitat devices not loaded yet. Wait a moment and try again.', 'info');
            return;
        }

        // Build dropdown options sorted by label
        const sorted = [...hubitatDevices].sort((a, b) =>
            (a.label || a.name || '').localeCompare(b.label || b.name || '')
        );

        let optionsHtml = '<option value="">-- Select device --</option>';
        for (const d of sorted) {
            const label = d.label || d.name || `Device ${d.id}`;
            optionsHtml += `<option value="${d.id}">${escapeHtml(label)} (#${d.id})</option>`;
        }

        $cell.html(`
            <div class="match-editor">
                <select class="mapping-select match-select">${optionsHtml}</select>
                <button class="btn btn-small btn-primary btn-save-match"
                        data-unique-id="${escapeHtml(uniqueId)}">Save</button>
                <button class="btn btn-small btn-secondary btn-cancel-match">Cancel</button>
            </div>
        `);

        // Bind save
        $cell.find('.btn-save-match').on('click', function () {
            const makerDeviceId = $cell.find('.match-select').val();
            if (!makerDeviceId) {
                showToast('Select a Hubitat device.', 'error');
                return;
            }
            saveMatch(uniqueId, makerDeviceId);
        });

        // Bind cancel
        $cell.find('.btn-cancel-match').on('click', function () {
            loadDiscoveredDevices();
        });
    }

    /**
     * Save a manual match correction to the server.
     */
    function saveMatch(uniqueId, makerApiDeviceId) {
        $.ajax({
            url: '/api/matter/hubitat-devices/match',
            method: 'PATCH',
            contentType: 'application/json',
            data: JSON.stringify({
                unique_id: uniqueId,
                maker_api_device_id: makerApiDeviceId
            })
        })
        .done(function () {
            loadDiscoveredDevices();
        })
        .fail(function (xhr) {
            showToast('Failed to save match: ' + (xhr.responseJSON?.detail || 'Unknown error'), 'error');
        });
    }

    // =========================================================================
    // Helpers
    // =========================================================================

    /**
     * Show a result message below the commission form.
     */
    function showCommissionResult(message, type) {
        const $result = $('#commission-result');
        $result.removeClass('success error loading').addClass(type).text(message).show();
    }

    /**
     * Extract a human-readable name from a Matter node object.
     * The structure varies by matter-server version.
     */
    function extractNodeName(node) {
        const nodeId = node.node_id || node.nodeId;

        // First: the RESOLVED current canonical device label (2026-06-20).
        // This is the authoritative current name and keeps the card title
        // consistent with the "Mapped to <label>" line. Prevents the
        // confusing case where Hubitat carries two names for one device
        // (e.g. admin/canonical 'TV POWER' vs Maker-API 'TV') and the card
        // showed the Maker name while mapping to the canonical one.
        if (node._canonical_label) return node._canonical_label;

        // Then: backend-enriched name (Maker/discovery name).
        if (node._device_name) return node._device_name;

        // Second: check our mapping table for a friendly name
        const mapped = mappings.find(m => m.matter_node_id === nodeId);
        if (mapped && mapped.device_name) return mapped.device_name;

        // Third: check discovered devices table for name
        const discovered = discoveredDevices.find(d =>
            d.our_node_id === nodeId || d.our_node_id === String(nodeId)
        );
        if (discovered && discovered.device_name) return discovered.device_name;

        // Fourth: matter-server fields
        if (node.node_label) return node.node_label;
        if (node.name) return node.name;

        // Fourth: Matter Basic Information cluster (40) attributes
        // Attr 5 = NodeLabel, Attr 3 = ProductName, Attr 1 = VendorName (last resort)
        const attrs = node.attributes || {};
        const attrPriority = ['/40/5', '/40/3', '/40/1'];
        for (const suffix of attrPriority) {
            for (const key in attrs) {
                if (key.endsWith(suffix) && typeof attrs[key] === 'string' && attrs[key]) {
                    return attrs[key];
                }
            }
        }

        return null;
    }

    /**
     * Extract human-readable details from a Matter node.
     */
    function extractNodeDetails(node) {
        const parts = [];

        if (node.available !== undefined) {
            parts.push(`<span>Available: ${node.available ? 'Yes' : 'No'}</span>`);
        }

        // Count endpoints
        const endpoints = node.endpoints || [];
        if (endpoints.length > 0) {
            parts.push(`<span>Endpoints: ${endpoints.length}</span>`);
        }

        // Try to find device type from attributes
        const attrs = node.attributes || {};
        const attrCount = Object.keys(attrs).length;
        if (attrCount > 0) {
            parts.push(`<span>Attributes: ${attrCount}</span>`);
        }

        return parts.join('') || '<span>No details available</span>';
    }

    /**
     * Copy text to clipboard with visual feedback on the button.
     * @param {string} text - Text to copy
     * @param {HTMLElement} btn - Button element for feedback
     * @param {string} [label] - Label to restore (default: btn's current text)
     */
    async function copyToClipboard(text, btn, label) {
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
        if (btn) {
            const saved = label || btn.textContent;
            btn.textContent = 'Copied!';
            btn.classList.add('copy-flash');
            setTimeout(() => {
                btn.textContent = saved;
                btn.classList.remove('copy-flash');
            }, 1200);
        }
    }

    /**
     * Escape HTML entities to prevent XSS.
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // =========================================================================
    // Modal — replaces all alert() and confirm() calls
    // =========================================================================

    /**
     * Show a non-blocking modal overlay. Backdrop click or Escape dismisses it.
     * @param {string} title - Modal heading
     * @param {string} body - Message text (newlines become <br>)
     * @param {string} type - 'loading', 'success', 'error', 'info'
     */
    function showModal(title, body, type) {
        // Remove existing modal if any
        $('#matter-modal-overlay').remove();

        const typeClass = type || 'info';
        const showSpinner = type === 'loading';
        const bodyHtml = escapeHtml(body).replace(/\n/g, '<br>');

        const html = `
            <div id="matter-modal-overlay" class="matter-modal-overlay">
                <div class="matter-modal" data-type="${typeClass}">
                    <div class="matter-modal-header">
                        <h4>${escapeHtml(title)}</h4>
                    </div>
                    <div class="matter-modal-body">
                        ${showSpinner ? '<div class="matter-modal-spinner"></div>' : ''}
                        <p>${bodyHtml}</p>
                    </div>
                    ${!showSpinner ? '<div class="matter-modal-footer"><button class="btn btn-small btn-secondary matter-modal-close">OK</button></div>' : ''}
                </div>
            </div>
        `;
        $('body').append(html);

        // Dismiss on backdrop click
        $('#matter-modal-overlay').on('click', function (e) {
            if ($(e.target).is('#matter-modal-overlay') || $(e.target).is('.matter-modal-close')) {
                $('#matter-modal-overlay').remove();
            }
        });

        // Dismiss on Escape
        $(document).on('keydown.matterModal', function (e) {
            if (e.key === 'Escape') {
                $('#matter-modal-overlay').remove();
                $(document).off('keydown.matterModal');
            }
        });
    }

    /**
     * Show a toast notification (brief, auto-dismissing).
     * @param {string} msg - Message text
     * @param {string} type - 'success', 'error', 'info'
     */
    function showToast(msg, type) {
        const typeClass = type || 'info';
        const $toast = $(`<div class="matter-toast matter-toast-${typeClass}">${escapeHtml(msg)}</div>`);
        $('body').append($toast);
        setTimeout(() => $toast.addClass('visible'), 10);
        setTimeout(() => {
            $toast.removeClass('visible');
            setTimeout(() => $toast.remove(), 300);
        }, 3000);
    }
});
