/**
 * Matter Device Management Controller
 *
 * Handles the Matter management page: server status, commissioning,
 * node listing, Hubitat-to-Matter device mapping, and Hubitat
 * Matter device discovery with auto-commissioning.
 */

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
    $('#btn-refresh-discovered').on('click', loadDiscoveredDevices);

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

            html += `
                <div class="node-card" data-node-id="${nodeId}">
                    <div class="node-card-header">
                        <h4 class="node-name">${escapeHtml(name)}</h4>
                        <span class="node-id">ID: ${nodeId}</span>
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
                    ${mapped
                        ? `<div class="node-mapped">Mapped to Hubitat #${mapped.hubitat_device_id}</div>`
                        : ''
                    }
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
            // Try to find Hubitat device name
            const hDevice = hubitatDevices.find(
                d => String(d.id) === String(m.hubitat_device_id)
            );
            const hName = hDevice ? hDevice.label || hDevice.name : '';

            html += `
                <tr>
                    <td>#${m.hubitat_device_id} ${escapeHtml(hName)}</td>
                    <td>${m.matter_node_id}</td>
                    <td>${m.matter_endpoint_id}</td>
                    <td>${escapeHtml(m.device_name || '')}</td>
                    <td>
                        <button class="btn btn-small btn-danger btn-delete-mapping"
                                data-device-id="${m.hubitat_device_id}">Remove</button>
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
            alert('Select both a Hubitat device and a Matter node');
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
            alert('Failed to create mapping: ' + (xhr.responseJSON?.detail || 'Unknown error'));
        });
    }

    /**
     * Delete a device mapping.
     */
    function deleteMapping(deviceId) {
        if (!confirm(`Remove mapping for Hubitat device #${deviceId}?`)) return;

        $.ajax({
            url: `/api/matter/map/${deviceId}`,
            method: 'DELETE'
        })
        .done(function () {
            loadMappings();
            loadNodes();
        })
        .fail(function (xhr) {
            alert('Failed to delete mapping: ' + (xhr.responseJSON?.detail || 'Unknown error'));
        });
    }

    /**
     * Send a test command to a Matter node via the device command API.
     * Uses the mapping if available, otherwise sends directly.
     */
    function testMatterCommand(nodeId, command) {
        // Find if this node has a Hubitat mapping
        const mapping = mappings.find(m => m.matter_node_id === nodeId);

        if (mapping) {
            // Send via Hubitat device command (which triggers dual-command)
            $.ajax({
                url: `/api/devices/${mapping.hubitat_device_id}/command`,
                method: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({ command: command })
            })
            .done(function () {
                console.log(`Test ${command} sent to Hubitat #${mapping.hubitat_device_id} + Matter node ${nodeId}`);
            })
            .fail(function (xhr) {
                alert(`Test failed: ${xhr.responseJSON?.detail || 'Unknown error'}`);
            });
        } else {
            alert(`Node ${nodeId} is not mapped to a Hubitat device. Create a mapping first.`);
        }
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
            const count = data.discovered ? data.discovered.length : 0;
            const errCount = data.errors ? data.errors.length : 0;
            let msg = `Discovered ${count} device(s) across hubs.`;
            if (errCount > 0) {
                msg += ` ${errCount} hub(s) had errors.`;
            }
            $status.removeClass('loading').addClass('success').text(msg);
            loadDiscoveredDevices();
            // Also refresh nodes in case some were already commissioned
            loadNodes();
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

        let html = `
            <table class="discovery-table">
                <thead>
                    <tr>
                        <th>Device Name</th>
                        <th>Hub</th>
                        <th>Status</th>
                        <th>Maker API Match</th>
                        <th>Confidence</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
        `;

        for (const d of discoveredDevices) {
            const online = d.is_online;
            const statusClass = online ? 'online' : 'offline';
            const statusLabel = online ? 'Online' : 'Offline';
            const isCommissioned = d.is_commissioned;

            // Confidence badge
            const conf = d.match_confidence || 'none';
            const confClass = conf === 'exact' ? 'badge-success'
                            : conf === 'fuzzy' ? 'badge-warning'
                            : conf === 'manual' ? 'badge-info'
                            : 'badge-none';

            // Maker API match display
            const matchDisplay = d.maker_api_device_name
                ? `${escapeHtml(d.maker_api_device_name)} (#${d.maker_api_device_id})`
                : '<span class="text-muted">No match</span>';

            // Commission button state
            let actionHtml;
            if (isCommissioned) {
                actionHtml = `<span class="badge badge-success">Commissioned (Node ${d.our_node_id})</span>`;
            } else if (!online) {
                actionHtml = '<span class="text-muted">Offline</span>';
            } else {
                actionHtml = `
                    <button class="btn btn-small btn-primary btn-auto-commission"
                            data-unique-id="${escapeHtml(d.unique_id)}"
                            data-device-name="${escapeHtml(d.device_name || '')}">
                        Commission
                    </button>
                `;
            }

            html += `
                <tr class="${isCommissioned ? 'row-commissioned' : ''}">
                    <td>
                        <strong>${escapeHtml(d.device_name || 'Unknown')}</strong>
                        <div class="device-meta">${escapeHtml(d.manufacturer || '')} ${escapeHtml(d.model || '')}</div>
                    </td>
                    <td>${escapeHtml(d.hub_name || d.hub_ip)}</td>
                    <td><span class="status-badge ${statusClass}">${statusLabel}</span></td>
                    <td>
                        <div class="match-cell">
                            <span class="match-current">${matchDisplay}</span>
                            <button class="btn-link btn-change-match"
                                    data-unique-id="${escapeHtml(d.unique_id)}"
                                    title="Change match">edit</button>
                        </div>
                    </td>
                    <td><span class="badge ${confClass}">${conf}</span></td>
                    <td>${actionHtml}</td>
                </tr>
            `;
        }

        html += '</tbody></table>';
        $container.html(html);

        // Bind auto-commission buttons
        $container.find('.btn-auto-commission').on('click', function () {
            autoCommission(
                $(this).data('unique-id'),
                $(this).data('device-name'),
                $(this)
            );
        });

        // Bind match-change buttons
        $container.find('.btn-change-match').on('click', function () {
            openMatchEditor($(this).data('unique-id'), $(this).closest('td'));
        });
    }

    /**
     * Auto-commission a single device: opens Hubitat pairing window,
     * commissions into our fabric, creates mapping.
     */
    function autoCommission(uniqueId, deviceName, $btn) {
        if (!confirm(`Commission "${deviceName}" into our Matter fabric?\n\nThis will:\n1. Open a pairing window on Hubitat\n2. Commission into our matter-server\n3. Map to its Maker API counterpart`)) {
            return;
        }

        $btn.prop('disabled', true).text('Commissioning...');

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
            // Refresh tables
            loadNodes();
            loadMappings();
            loadDiscoveredDevices();
        })
        .fail(function (xhr) {
            const detail = xhr.responseJSON?.detail || 'Commission failed';
            alert(`Auto-commission failed: ${detail}`);
            $btn.prop('disabled', false).text('Commission');
        });
    }

    /**
     * Open inline match editor: replaces the match cell with a dropdown
     * of all Maker API devices for manual correction.
     */
    function openMatchEditor(uniqueId, $cell) {
        if (hubitatDevices.length === 0) {
            alert('Hubitat devices not loaded yet. Wait a moment and try again.');
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
                alert('Select a Maker API device.');
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
            alert('Failed to save match: ' + (xhr.responseJSON?.detail || 'Unknown error'));
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
        // Try common fields
        if (node.node_label) return node.node_label;
        if (node.name) return node.name;

        // Try attributes path for Basic Information cluster (cluster 40)
        const attrs = node.attributes || {};
        for (const key in attrs) {
            // key format: "endpoint/cluster/attribute"
            if (key.includes('/40/') && typeof attrs[key] === 'string') {
                return attrs[key];
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
     * Escape HTML entities to prevent XSS.
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
});
