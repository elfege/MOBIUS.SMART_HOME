/**
 * Matter Device Management Controller
 *
 * Handles the Matter management page: server status, commissioning,
 * node listing, and Hubitat-to-Matter device mapping.
 */

$(document).ready(function () {
    // =========================================================================
    // State
    // =========================================================================

    let matterNodes = [];
    let hubitatDevices = [];
    let mappings = [];

    // =========================================================================
    // Initialization
    // =========================================================================

    loadAll();

    $('#btn-refresh-status').on('click', loadStatus);
    $('#btn-refresh-nodes').on('click', loadNodes);
    $('#btn-commission').on('click', commissionDevice);
    $('#btn-create-mapping').on('click', createMapping);

    // =========================================================================
    // Data Loading
    // =========================================================================

    /**
     * Load all data in parallel: status, nodes, mappings, Hubitat devices.
     */
    function loadAll() {
        loadStatus();
        loadNodes();
        loadMappings();
        loadHubitatDevices();
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
