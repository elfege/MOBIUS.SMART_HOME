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
    $('#btn-refresh-nodes').on('click', loadNodes);
    $('#btn-commission').on('click', commissionDevice);
    $('#btn-create-mapping').on('click', createMapping);
    $('#btn-scan-hubs').on('click', scanHubs);
    $('#btn-commission-all').on('click', function () { commissionAll(); });
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
            })
            .fail(function () {
                $('#status-panel').html(
                    '<div class="status-item"><span class="status-dot disconnected"></span>' +
                    '<span class="status-value">Cannot reach API</span></div>'
                );
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
        return {
            stale: offline || (ageMs !== null && ageMs > STALE_MS),
            longGone: offline || (ageMs !== null && ageMs > LONG_MS),
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
            const offline = node._is_online === false;
            const stale = offline || (ageMs !== null && ageMs > STALE_MS);
            const longGone = offline || (ageMs !== null && ageMs > LONG_MS);
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
            showCommissionResult(detail, 'error');
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
     * Send a test command to a Matter node via the device command API.
     * Uses the mapping if available, otherwise sends directly.
     */
    function testMatterCommand(nodeId, command) {
        // Resolve to the CURRENT canonical device id, computed server-side via
        // the exact (hub_ip, hubitat_id) anchor (2026-06-19). The command
        // endpoint takes a CANONICAL devices.id — sending the old frozen
        // maker-api id (#660) hit nothing, which is why Test ON/OFF "failed".
        const node = matterNodes.find(n => (n.node_id || n.nodeId) === nodeId);
        const canonicalId = node && node._canonical_id;

        if (!node || node._mapping_stale || !canonicalId) {
            showToast(
                `Node ${nodeId} doesn't resolve to a current Hubitat device `
                + `(mapping stale or uncommissioned). Re-map it first.`,
                'info'
            );
            return;
        }

        $.ajax({
            url: `/api/devices/${canonicalId}/command`,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ command: command })
        })
        .done(function () {
            console.log(`Test ${command} → canonical #${canonicalId} (Matter node ${nodeId})`);
        })
        .fail(function (xhr) {
            showToast(`Test failed: ${xhr.responseJSON?.detail || 'Unknown error'}`, 'error');
        });
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

            // Maker API match
            const matchDisplay = d.maker_api_device_name
                ? `${escapeHtml(d.maker_api_device_name)} (#${d.maker_api_device_id})`
                : '<span class="text-muted">No match</span>';

            // Actions
            let actionHtml;
            if (isCommissioned) {
                actionHtml = `<span class="badge badge-success">Node ${d.our_node_id}</span>`;
            } else if (!online) {
                actionHtml = `<button class="btn btn-small btn-secondary btn-rescan-device"
                                data-unique-id="${uid}" data-hub-ip="${escapeHtml(d.hub_ip)}"
                                data-node-id="${d.hubitat_node_id}" title="Rescan this device">Rescan</button>`;
            } else {
                actionHtml = `<button class="btn btn-small btn-primary btn-auto-commission"
                            data-unique-id="${uid}"
                            data-device-name="${name}">Commission</button>`;
            }

            // Detail rows for expandable section
            const mac = d.mac_address ? escapeHtml(d.mac_address) : '<span class="text-muted">unknown</span>';
            const ip = d.ip_address ? escapeHtml(d.ip_address) : '<span class="text-muted">none</span>';
            const fw = d.firmware_version ? escapeHtml(d.firmware_version) : '—';
            const hw = d.hardware_version ? escapeHtml(d.hardware_version) : '—';
            const serial = d.serial_number ? escapeHtml(d.serial_number) : '—';
            const dni = d.hubitat_dni ? escapeHtml(d.hubitat_dni) : '—';
            const lastSeen = d.last_seen_at ? new Date(d.last_seen_at).toLocaleString() : '—';

            html += `
                <div class="device-card ${statusClass} ${isCommissioned ? 'commissioned' : ''}" data-uid="${uid}">
                    <div class="device-card-header">
                        <div class="device-card-title">
                            <strong>${name}</strong>
                            <span class="status-badge ${statusClass}">${statusLabel}</span>
                        </div>
                        <div class="device-card-actions">
                            ${actionHtml}
                            <button class="btn-link btn-expand-card" title="Show details">&#9660;</button>
                        </div>
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
                    <div class="device-card-details" style="display:none;">
                        <div class="detail-toolbar">
                            <button class="btn btn-secondary btn-small btn-copy btn-copy-details" title="Copy details">Copy</button>
                        </div>
                        <table class="detail-table">
                            <tr><td>MAC Address</td><td>${mac}</td></tr>
                            <tr><td>IP Address</td><td>${ip}</td></tr>
                            <tr><td>Unique ID</td><td><code>${uid}</code></td></tr>
                            <tr><td>DNI</td><td>${dni}</td></tr>
                            <tr><td>Hub Node</td><td>${d.hubitat_node_id || '—'}</td></tr>
                            <tr><td>Hubitat Device ID</td><td>${d.hubitat_device_id || '—'}</td></tr>
                            <tr><td>Firmware</td><td>${fw}</td></tr>
                            <tr><td>Hardware</td><td>${hw}</td></tr>
                            <tr><td>Serial</td><td>${serial}</td></tr>
                            <tr><td>Last Seen</td><td>${lastSeen}</td></tr>
                            ${isCommissioned ? `<tr><td>Our Node ID</td><td>${d.our_node_id}</td></tr>` : ''}
                        </table>
                    </div>
                </div>
            `;
        }

        html += '</div>';
        $container.html(html);

        // Bind expand/collapse
        $container.find('.btn-expand-card').on('click', function () {
            const $card = $(this).closest('.device-card');
            const $details = $card.find('.device-card-details');
            $details.slideToggle(150);
            $(this).html($details.is(':visible') ? '&#9650;' : '&#9660;');
        });

        // Bind auto-commission buttons
        $container.find('.btn-auto-commission').on('click', function () {
            autoCommission(
                $(this).data('unique-id'),
                $(this).data('device-name'),
                $(this)
            );
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
            '1. Opening pairing window on Hubitat...\n2. Commissioning into matter-server...\n3. Creating Maker API mapping...',
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
                showToast('Select a Maker API device.', 'error');
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
