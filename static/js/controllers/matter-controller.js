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
    // Commission All = USER-initiated bulk (selected hub only). Kept by
    // operator directive; what's banned is any AUTOMATIC trigger of it.
    $('#btn-commission-all').on('click', function () { commissionAll(); });
    // MULTI-SELECT MATTER HUBS: populate the checkbox panel; toggling persists
    // the SET (system_settings.matter_hub_ids). Scanning covers every checked
    // hub; devices are deduped by MAC so one physical device on 2 hubs = one
    // card; commissioning is per-device via the device's own hub (auto).
    loadMatterHub();
    // Open/close the hub panel.
    $(document).on('click', '#matter-hub-toggle', function (e) {
        e.stopPropagation();
        $('#matter-hub-panel').toggle();
    });
    $(document).on('click', function (e) {
        if (!$(e.target).closest('#matter-hub-picker').length) $('#matter-hub-panel').hide();
    });
    // Persist on any checkbox change.
    $(document).on('change', '.matter-hub-cb', function () {
        const ids = $('.matter-hub-cb:checked').map(function () { return parseInt(this.value, 10); }).get();
        if (!ids.length) {
            showToast('Select at least one hub for Matter', 'error');
            $(this).prop('checked', true);   // revert — never allow empty
            return;
        }
        $.ajax({
            url: '/api/matter/hub', method: 'POST', contentType: 'application/json',
            data: JSON.stringify({ hub_ids: ids })
        })
        .done(function (d) {
            showToast(`Matter hubs: ${d.selected.map(h => h.hub_name).join(', ')}`, 'success');
            updateHubToggleLabel(d.selected);
            loadDiscoveredDevices();
        })
        .fail(function (xhr) {
            showToast('Failed to set Matter hubs: ' + (xhr.responseJSON?.detail || 'error'), 'error');
            loadMatterHub();
        });
    });
    $('#btn-remove-all-discovered').on('click', function () { removeAllDiscovered(false); });
    $('#btn-force-remove-all-discovered').on('click', function () { removeAllDiscovered(true); });
    $('#btn-decommission-all-nodes').on('click', function () { decommissionAll(); });
    $('#btn-refresh-discovered').on('click', loadDiscoveredDevices);
    $('#btn-scan-mdns').on('click', scanMdns);
    // Commission a NEW device by code even when it isn't in the mDNS list — the
    // pairing code's discriminator lets the controller find it on the network,
    // so a device doesn't have to be announcing/visible to commission it.
    $('#btn-commission-code-direct').on('click', function () {
        openCommissionModal({
            title: 'Commission new device (code)',
            subtitle: 'no hub — the code finds the device on the network',
            needsCode: true,
            run: (code) => ({ url: '/api/matter/commission', body: { code } }),
        });
    });
    // Mapping section bulk actions: mappings are pure link records, so Remove
    // all touches no device/node state and Rebuild regenerates rows for every
    // non-removed discovered device that has a node in our fabric.
    $('#btn-remove-all-mappings').on('click', function () {
        if (!confirm('Remove ALL Hubitat ↔ Matter mappings?\n\nOnly the link records are deleted — no device or Matter node is touched. You can rebuild them any time.')) return;
        $.ajax({ url: '/api/matter/map', method: 'DELETE' })
            .done(function (d) { showToast(`Removed ${d.removed} mapping(s)`, 'success'); loadMappings(); })
            .fail(function (x) { showToast('Remove-all mappings failed: ' + (x.responseJSON?.detail || 'error'), 'error'); });
    });
    $('#btn-rebuild-mappings').on('click', function () {
        $.ajax({ url: '/api/matter/map/rebuild', method: 'POST' })
            .done(function (d) {
                showToast(`Rebuilt ${d.rebuilt} mapping(s); ${d.skipped} skipped (no node/id)`, 'success');
                loadMappings();
            })
            .fail(function (x) { showToast('Rebuild failed: ' + (x.responseJSON?.detail || 'error'), 'error'); });
    });

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
                        <button class="btn btn-small btn-secondary btn-get-code"
                                data-node-id="${nodeId}" data-device-name="${escapeHtml(name)}"
                                title="Get a pairing code for this device — its stored factory code if we have one, otherwise a fresh code from a commissioning window we open on it">Get Code</button>
                        <button class="btn btn-small btn-danger btn-node-decommission"
                                data-node-id="${nodeId}" data-node-name="${escapeHtml(name)}"
                                title="Remove OUR fabric from this device (frees the slot; the device stays on Hubitat/other fabrics). DB is reconciled: the discovered row returns to 'uncommissioned' and its mapping is deleted.">Decommission</button>
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
        // Per-tile decommission (operator directive 2026-07-12): full leave
        // (keep_current=false) + backend DB reconcile, then refresh all panes.
        $container.find('.btn-node-decommission').on('click', function () {
            const nodeId = $(this).data('node-id');
            const name = $(this).data('node-name') || `node ${nodeId}`;
            if (!confirm(`Decommission "${name}" (node ${nodeId})?\n\n`
                + 'Removes OUR fabric from the device — it stays on Hubitat and any other '
                + 'fabric. The device becomes commissionable again (Commission / Commission All).')) {
                return;
            }
            const $btn = $(this).prop('disabled', true).text('Decommissioning…');
            $.ajax({
                url: `/api/matter/nodes/${nodeId}/decommission`, method: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({ keep_current: false })
            })
            .done(function () {
                showToast(`Decommissioned ${name}`, 'success');
                loadNodes(); loadMappings(); loadDiscoveredDevices();
            })
            .fail(function (xhr) {
                $btn.prop('disabled', false).text('Decommission');
                showToast('Decommission failed: ' + (xhr.responseJSON?.detail || xhr.statusText), 'error');
            });
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
            // Clean, human-readable summary instead of dumping the raw node
            // JSON (the whole attribute tree) into the modal. Endpoint count is
            // derived from the 'endpoint/cluster/attribute' attribute keys.
            const n = data.node || {};
            let epCount = '?';
            if (n.attributes && typeof n.attributes === 'object') {
                epCount = new Set(
                    Object.keys(n.attributes).map(k => String(k).split('/')[0])
                ).size;
            }
            let when = '—';
            if (n.date_commissioned) {
                const d = new Date(n.date_commissioned);
                if (!isNaN(d)) when = d.toLocaleString();
            }
            const notReady = (n.available === false)
                ? ' · not yet available (still interviewing)' : '';
            showCommissionResult(
                `Device commissioned — Node ${n.node_id ?? '?'} · `
                + `${when} · ${epCount} endpoint(s)${notReady}`,
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
     * Fire an ON/OFF straight at a Matter NODE via matter-server, log the
     * attempt to the LEARNING TABLE, and ask the operator for VISUAL
     * CONFIRMATION. Shared by the nodes grid and the discovered cards.
     *
     * This is a Matter-native test (POST /api/matter/nodes/{id}/command →
     * OnOff cluster) — it does NOT go through Hubitat. The loading spinner is
     * held for a minimum of 2s (operator directive) so a fast round-trip still
     * reads as "it did something".
     *
     * Learning loop (operator directive 2026-07-11): when the command settles
     * (success OR failure) the attempt is POSTed to /api/matter/feedback with
     * what the API *claimed*; the result modal then requires the operator's
     * verdict ("It worked" / "It didn't") which is PATCHed onto the same row.
     * Skipping the modal leaves the row 'unverified' — still a data point.
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
                logMatterAttempt(nodeId, command, label, true, null).then(function (fid) {
                    showVerdictModal(fid, nodeId, command, label, true, null);
                });
            });
        })
        .fail(function (xhr) {
            minSpinner.then(function () {
                const d = xhr.responseJSON?.detail;
                const detail = (typeof d === 'string') ? d : (d ? JSON.stringify(d) : 'Unknown error');
                logMatterAttempt(nodeId, command, label, false, detail).then(function (fid) {
                    showVerdictModal(fid, nodeId, command, label, false, detail);
                });
            });
        });
    }

    /**
     * Set a Matter device's level (LevelControl cluster 8) DIRECTLY via
     * matter-server, then open the same verdict modal as on/off. Mirrors
     * sendMatterTest; used by the debug console's "Set level" control.
     */
    function sendMatterLevel(nodeId, level, label) {
        const cmdLabel = 'setLevel ' + level;
        showModal(`Setting level ${level}% (Matter)`,
            `Commanding the MATTER device "${label}" (node ${nodeId}) to ${level}% directly via `
            + `matter-server — not through Hubitat…`, 'loading');
        const minSpinner = new Promise(function (resolve) { setTimeout(resolve, 2000); });
        $.ajax({
            url: `/api/matter/nodes/${nodeId}/command`,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ command: 'level', level: level })
        })
        .done(function () {
            minSpinner.then(function () {
                logMatterAttempt(nodeId, cmdLabel, label, true, null).then(function (fid) {
                    showVerdictModal(fid, nodeId, cmdLabel, label, true, null);
                });
            });
        })
        .fail(function (xhr) {
            minSpinner.then(function () {
                const d = xhr.responseJSON?.detail;
                const detail = (typeof d === 'string') ? d : (d ? JSON.stringify(d) : 'Unknown error');
                logMatterAttempt(nodeId, cmdLabel, label, false, detail).then(function (fid) {
                    showVerdictModal(fid, nodeId, cmdLabel, label, false, detail);
                });
            });
        });
    }

    /**
     * Learning log, step 1 — record the attempt the moment the command
     * settles, BEFORE the operator answers (so even a dismissed modal leaves
     * an 'unverified' row). Resolves to the feedback row id, or null if the
     * logging call itself failed (the verdict modal still shows; verdict
     * buttons then just close without a row to attach to).
     */
    function logMatterAttempt(nodeId, command, label, apiSuccess, apiDetail) {
        return $.ajax({
            url: '/api/matter/feedback',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                node_id: nodeId,
                command: command,
                device_label: label,
                api_success: apiSuccess,
                api_detail: apiDetail
            })
        }).then(function (r) { return r.id; }).catch(function () { return null; });
    }

    /**
     * Learning log, step 2 — VISUAL CONFIRMATION modal (operator: "modal
     * requires visual confirmation"). Replaces the fire-and-forget "watch the
     * physical device" text: the operator's answer is PATCHed onto the
     * learning-log row, with an optional free-text note.
     *
     * Colorblind-safe (PIN.1-5): verdicts carry glyph+label, accents are
     * high-luminance blue (worked) / red (didn't) only — no green.
     * Backdrop click / Skip = row stays 'unverified' (still a data point).
     */
    function showVerdictModal(feedbackId, nodeId, command, label, apiSuccess, apiDetail) {
        $('#matter-modal-overlay').remove();
        const CMD = String(command).toUpperCase();
        const title = apiSuccess
            ? `Matter ${CMD} sent — did the device actually do it?`
            : `Matter ${CMD} FAILED at the API — did the device react anyway?`;
        const bodyText = apiSuccess
            ? `Sent "${command}" to "${label}" (node ${nodeId}) over the fabric — not via Hubitat.\n\n`
              + `Look at the physical device. Your answer is saved to the learning log.`
            : `${apiDetail || 'Unknown error'}\n\n`
              + `Look at the device anyway — your answer is saved to the learning log.`;
        const html = `
            <div id="matter-modal-overlay" class="matter-modal-overlay">
                <div class="matter-modal" data-type="${apiSuccess ? 'info' : 'error'}">
                    <div class="matter-modal-header">
                        <h4>${escapeHtml(title)}</h4>
                    </div>
                    <div class="matter-modal-body">
                        <p>${escapeHtml(bodyText).replace(/\n/g, '<br>')}</p>
                        <input type="text" id="matter-verdict-notes"
                               placeholder="optional note (e.g. 'flickered but stayed on')"
                               style="width:100%; margin-top:.6rem; border-radius:4px;
                                      padding:.35rem .5rem; font:inherit; font-size:.85rem;">
                    </div>
                    <div class="matter-modal-footer" style="display:flex; gap:.5rem; justify-content:flex-end;">
                        <button class="btn btn-small btn-secondary matter-verdict-btn" data-verdict="worked"
                                style="border-color:var(--color-terminal-blue,#89b4fa); color:var(--color-terminal-blue,#89b4fa);">
                            &#10004; It worked</button>
                        <button class="btn btn-small btn-secondary matter-verdict-btn" data-verdict="failed"
                                style="border-color:var(--color-toast-error,#e57373); color:var(--color-toast-error,#e57373);">
                            &#10006; It didn't</button>
                        <button class="btn btn-small btn-secondary matter-modal-close"
                                title="Close without answering — this attempt stays logged as 'unverified'">Skip</button>
                    </div>
                </div>
            </div>`;
        $('body').append(html);
        $('#matter-modal-overlay').on('click', function (e) {
            if ($(e.target).is('#matter-modal-overlay') || $(e.target).is('.matter-modal-close')) {
                $('#matter-modal-overlay').remove();   // row stays 'unverified'
            }
        });
        $('#matter-modal-overlay .matter-verdict-btn').on('click', function () {
            const verdict = $(this).data('verdict');
            const notes = ($('#matter-verdict-notes').val() || '').trim() || null;
            const close = function () { $('#matter-modal-overlay').remove(); };
            if (feedbackId == null) { close(); return; }   // attempt-logging failed — nothing to attach to
            $.ajax({
                url: `/api/matter/feedback/${feedbackId}/verdict`,
                method: 'PATCH',
                contentType: 'application/json',
                data: JSON.stringify({ verdict: verdict, notes: notes })
            })
            .done(function () {
                showToast(`Logged: ${verdict === 'worked' ? '✔ worked' : "✖ didn't work"}`, 'info');
                close();
            })
            .fail(function (x) {
                showToast('Verdict save failed: ' + (x.responseJSON?.detail || 'error'), 'error');
                close();
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
    function openMatterDebug(nodeId, label, uniqueId) {
        // A card with no node (never commissioned, or decommissioned) still
        // opens the console — server diagnostics + live log work regardless;
        // the node-scoped sections and command buttons need a node id, and the
        // commission buttons need the discovered device's uniqueId.
        const hasNode = nodeId !== null && nodeId !== undefined && String(nodeId) !== '';
        const hasUid = uniqueId !== null && uniqueId !== undefined && String(uniqueId) !== '';
        _debugLogSeq = 0;
        $('#matter-debug-overlay').remove();
        $('body').append(`
            <div id="matter-debug-overlay" class="matter-debug-overlay">
              <div class="matter-debug">
                <div class="matter-debug-head">
                  <h3>Matter debug — ${escapeHtml(label || (hasNode ? 'node ' + nodeId : 'device'))}
                      <span class="dbg-mono">${hasNode ? `(node ${nodeId})` : '(not commissioned)'}</span></h3>
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
                      <span class="dbg-mono" id="dbg-logstate"></span>
                      <button class="btn btn-small btn-secondary" id="dbg-copylog" style="float:right;"
                              title="Copy the whole log to the clipboard">Copy logs</button></div>
                    <pre class="matter-debug-log" id="dbg-log"></pre>
                  </div>
                </div>
              </div>
            </div>`);
        // Close ONLY via the explicit Close button — NOT on backdrop click.
        // Operator directive 2026-07-13: clicking outside (to select/copy log
        // text or paste a value) was dismissing the modal and losing its state.
        $('#matter-debug-overlay').on('click', function (e) {
            if ($(e.target).is('.matter-debug-close')) closeMatterDebug();
        });
        $(document).on('keydown.matterDebug', function (e) { if (e.key === 'Escape') closeMatterDebug(); });

        // Action buttons — rendered UNCONDITIONALLY here (for any node-bearing
        // device). They previously lived inside loadDebugFabrics().done(), so a
        // failing fabric read (e.g. the node isn't in the current controller →
        // 500, now a clean error) left the console with NO actions at all —
        // "fucking useless" per the operator, twice. on/off/level command the
        // MATTER device directly (not via Hubitat); decommission removes our
        // fabrics. Same actions as the cards. With NO node (never commissioned
        // / decommissioned) there is nothing to command — the console still
        // shows server diagnostics + the live log.
        if (!hasNode) {
            $('#dbg-fabrics').text('Fabrics: not commissioned — no node in our fabric.');
            $('#dbg-node').text('Node: not commissioned — commission the device to get node-level state.');
            // The actions a NOT-commissioned device needs: get it INTO our
            // fabric. Same flows as the card — Commission (opens a pairing
            // window on the Matter hub) and Commission with a manual code.
            if (hasUid) {
                $('#dbg-actions').html(
                    `<div class="dbg-act-row">`
                    + `<button class="btn btn-small btn-primary dbg-commission" data-uid="${uniqueId}">Commission</button>`
                    + ` <button class="btn btn-small btn-secondary dbg-commission-code" data-uid="${uniqueId}">Commission (code)</button>`
                    + `</div>`);
                $('#dbg-actions .dbg-commission').on('click', function () {
                    autoCommission($(this).data('uid'), label, $(this));
                });
                $('#dbg-actions .dbg-commission-code').on('click', function () {
                    recommissionDevice($(this).data('uid'), label);
                });
            } else {
                $('#dbg-actions').html(
                    '<span class="dbg-warn">No device id in this context — commission from the device card.</span>');
            }
            loadDebugServer();
            pollDebugLog();
            _debugLogTimer = setInterval(pollDebugLog, 2000);
            return;
        }
        $('#dbg-actions').html(
            `<div class="dbg-act-row">`
            + `<button class="btn btn-small btn-secondary dbg-cmd" data-node="${nodeId}" data-cmd="on">Test ON</button>`
            + ` <button class="btn btn-small btn-secondary dbg-cmd" data-node="${nodeId}" data-cmd="off">Test OFF</button>`
            + ` <input type="number" id="dbg-level-val" class="dbg-level-input" min="0" max="100" value="50" title="level 0-100">`
            + ` <button class="btn btn-small btn-secondary dbg-level" data-node="${nodeId}">Set level</button>`
            + `</div>`
            + `<div class="dbg-act-row">`
            + (hasUid
                ? `<button class="btn btn-small btn-secondary dbg-recomm-code" data-uid="${uniqueId}"`
                  + ` title="Decommission first if needed, then re-pair with a manual pairing/setup code">Recommission (code)</button> `
                : '')
            + `<button class="btn btn-small btn-danger dbg-decomm" data-node="${nodeId}" data-keep="true"`
            + ` title="Remove only our ORPHANED fabrics — frees slots, keeps the device controllable">Clear our orphans</button>`
            + ` <button class="btn btn-small btn-danger dbg-decomm" data-node="${nodeId}" data-keep="false"`
            + ` title="Remove ALL our fabrics — fully leave this device">Decommission (leave)</button>`
            + `</div>`);
        $('#dbg-actions .dbg-recomm-code').on('click', function () {
            recommissionDevice($(this).data('uid'), label);
        });
        $('#dbg-actions .dbg-cmd').on('click', function () {
            sendMatterTest($(this).data('node'), $(this).data('cmd'), label);
        });
        $('#dbg-actions .dbg-level').on('click', function () {
            const v = parseInt($('#dbg-level-val').val(), 10);
            if (isNaN(v) || v < 0 || v > 100) { showToast('Enter a level 0-100', 'error'); return; }
            sendMatterLevel($(this).data('node'), v, label);
        });
        $('#dbg-actions .dbg-decomm').on('click', function () {
            decommissionNode($(this).data('node'), String($(this).data('keep')) === 'true');
        });

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
            // Action buttons (on/off/level/decommission) are rendered
            // UNCONDITIONALLY in openMatterDebug — NOT here — so they survive a
            // failed fabric read. Only the per-fabric Remove buttons (in the
            // table above) are wired here.
            $('.dbg-remove-fabric').off('click').on('click', function () { removeFabric($(this).data('node'), $(this).data('idx')); });
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
        // TWO-STEP FLOW, abort-safe at every step (2026-07-12 — the previous
        // version overloaded confirm()'s Cancel button to mean "FULLY LEAVE
        // every device", so there was NO safe way to dismiss the dialog).
        // Step 1 offers the safe default; Cancel there goes to step 2, where
        // the destructive option needs its OWN explicit OK; Cancel = abort.
        let keep;
        if (confirm('DECOMMISSION ALL — safe option first:\n\n'
                  + 'OK = clear ORPHANED fabrics only (recommended — devices stay '
                  + 'controllable via our current fabric).\n'
                  + 'Cancel = show the full-leave option instead.')) {
            keep = true;
        } else if (confirm('FULLY LEAVE every device?\n\n'
                  + 'Removes ALL our fabrics from every commissioned node (NOT a device '
                  + 'reset — devices stay on Hubitat/other fabrics). Rows return to '
                  + '"uncommissioned" and their mappings are deleted; re-commission any time.\n\n'
                  + 'OK = fully leave every device.\nCancel = do nothing.')) {
            keep = false;
        } else {
            return;   // real abort path
        }
        showModal('Decommission all', keep ? 'Clearing orphaned fabrics on every node…'
                                           : 'Fully leaving every node…', 'loading');
        $.ajax({ url: '/api/matter/decommission-all', method: 'POST', contentType: 'application/json', data: JSON.stringify({ keep_current: keep }) })
            .done(function (d) {
                const rec = d.reconciled
                    ? ` DB reconciled: ${d.reconciled.devices_reconciled} device row(s) uncommissioned, ${d.reconciled.mappings_deleted} mapping(s) deleted.`
                    : '';
                showModal('Decommission all complete',
                          `${d.count} nodes processed (keep_current=${d.keep_current}).${rec}`, 'success');
                loadNodes(); loadMappings(); loadDiscoveredDevices();
            })
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
        const d = (discoveredDevices || []).find(x => x.unique_id === uniqueId) || {};
        const hasNode = d.our_node_id != null;
        // Recommission via the LIVE-LOG modal; if it's already a node of ours,
        // decommission it first (preStep) so the fabric slot is freed before
        // the re-pair. The code field takes a Hubitat "Get Setup Code" / HomeKit
        // "Pair with Other Platforms" code (already-paired devices) or the
        // factory QR/label (factory-new). CHIP-level cause streams in the log.
        openCommissionModal({
            title: `Recommission ${name} (code)`,
            subtitle: hasNode ? `currently node ${d.our_node_id} — decommissioned first` : 'not yet in our fabric',
            needsCode: true,
            preStep: hasNode ? function () {
                return $.ajax({
                    url: `/api/matter/devices/${d.our_node_id}/remove`, method: 'POST',
                    contentType: 'application/json',
                    data: JSON.stringify({ reason: 'recommission (manual code)' })
                });
            } : null,
            run: (code) => ({ url: '/api/matter/commission', body: { code } }),
        });
    }

    /**
     * Scan the LAN DIRECTLY over mDNS (_matterc._udp) for Matter devices in
     * commissioning mode — no hub involved. Results are LIVE (not persisted):
     * a device only announces while its pairing window is open. Devices also
     * known via a hub are deduped (MAC → IP) and flagged, not duplicated;
     * commissioning a flagged one ADDS our fabric via multi-admin.
     */
    function scanMdns() {
        const $btn = $('#btn-scan-mdns');
        const $c = $('#mdns-container');
        $btn.prop('disabled', true).text('Scanning…');
        $c.html('<div class="matter-empty">Browsing mDNS for commissionable Matter devices…</div>');
        $.ajax({ url: '/api/matter/discover-mdns', method: 'POST', timeout: 60000 })
        .done(function (d) {
            const devs = d.devices || [];
            if (!devs.length) {
                $c.html('<div class="matter-empty">No devices in pairing mode right now. '
                    + 'Put a device in commissioning mode (factory-new, or open a pairing window) and re-scan.</div>');
                return;
            }
            let html = '';
            devs.forEach(function (m, i) {
                const dup = m.hub_match
                    ? `<span class="mdns-dup-chip" title="Same physical device as hub-discovered '${escapeHtml(m.hub_match.device_name || '')}' on ${escapeHtml(m.hub_match.hub_name || '?')} (matched by MAC/IP). Commissioning here ADDS our fabric — it does not duplicate the device.">= ${escapeHtml(m.hub_match.device_name || 'hub device')} @${escapeHtml(m.hub_match.hub_name || '?')}</span>`
                    : '<span class="mdns-new-chip" title="Not known to any hub — a pure Matter-direct device. No Hubitat fallback applies.">mDNS-only</span>';
                const title = m.device_name || `vendor ${m.vendor_id} / product ${m.product_id}`;
                html += `<div class="mdns-card">
                    <div class="mdns-card-head"><strong>${escapeHtml(title)}</strong> ${dup}</div>
                    <div class="mdns-card-body">
                        IP: ${escapeHtml(m.ipv4 || '—')} · MAC: ${escapeHtml(m.mac || '—')} ·
                        discriminator: ${m.long_discriminator ?? '—'} · mode: ${m.commissioning_mode}
                    </div>
                    <div class="mdns-card-actions">
                        <button class="btn btn-small btn-primary mdns-commission" data-i="${i}"
                                title="Commission this device directly into our Matter fabric — paste its QR string or numeric pairing code">Commission (code)</button>
                    </div>
                </div>`;
            });
            $c.html(html);
            $c.find('.mdns-commission').on('click', function () {
                const m = devs[$(this).data('i')];
                openCommissionModal({
                    title: `Commission ${m.device_name || 'device'} — direct, no hub`,
                    subtitle: `${m.ipv4 || ''} ${m.mac ? '· ' + m.mac : ''}`,
                    needsCode: true,
                    run: (code) => ({ url: '/api/matter/commission', body: { code } }),
                });
            });
        })
        .fail(function (xhr) {
            $c.html(`<div class="matter-empty">mDNS scan failed: ${escapeHtml(xhr.responseJSON?.detail || 'error')}</div>`);
        })
        .always(function () { $btn.prop('disabled', false).text('Scan mDNS'); });
    }

    /**
     * Load the Matter hub selection (single-hub policy): fills the header
     * dropdown with every hub and selects THE Matter hub (persisted choice, or
     * the main/primary hub by default).
     */
    // =========================================================================
    // Commission modal with LIVE matter-client log (all sources: mDNS, hub
    // card, recommission). Commissioning is the highest-stakes Matter op and
    // was previously a blind spinner + opaque result; now the operator watches
    // the CHIP-level log stream as it happens (same tail as the debug console).
    // =========================================================================
    let _commLogTimer = null, _commLogSeq = 0, _commSrvOffset = -1, _commSrvAvail = false;

    function _commAppend($log, html) {
        const el = $log[0];
        const atBottom = el && (el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        $log.append(html);
        if (atBottom && el) el.scrollTop = el.scrollHeight;
    }

    function pollCommLog() {
        const $log = $('#comm-log');
        if (!$log.length) return;
        // 1) client op-log (our WS commands/results)
        $.getJSON(`/api/matter/debug/log?since_seq=${_commLogSeq}&limit=120`).done(function (d) {
            if (d.records && d.records.length) {
                let html = '';
                d.records.forEach(function (r) {
                    const cls = r.level === 'ERROR' ? 'dbg-bad' : (r.level === 'WARNING' ? 'dbg-warn' : '');
                    html += `<span class="${cls}">${r.ts.slice(11, 19)} <b>[cli]</b> ${escapeHtml(r.msg)}</span>\n`;
                });
                _commAppend($log, html);
                _commLogSeq = d.last_seq;
            }
            $('#comm-logstate').text(`cli seq ${_commLogSeq}${_commSrvAvail ? ' · srv live' : ''}`);
        });
        // 2) matter-server CHIP log (the AddNOC / attestation / error-code detail)
        $.getJSON(`/api/matter/server-log?since=${_commSrvOffset}`).done(function (s) {
            _commSrvAvail = !!s.available;
            if (s.available && s.lines && s.lines.length && _commSrvOffset >= 0) {
                let html = '';
                s.lines.forEach(function (ln) {
                    const low = ln.toLowerCase();
                    const cls = (low.includes('error') || low.includes('(133)') || low.includes('failed')
                                 || low.includes('invalidcommand')) ? 'dbg-bad'
                              : (low.includes('warn') ? 'dbg-warn' : 'dbg-srv');
                    html += `<span class="${cls}"><b>[srv]</b> ${escapeHtml(ln)}</span>\n`;
                });
                _commAppend($log, html);
            }
            if (s.available) _commSrvOffset = s.offset;  // first poll just seeds the tail offset
        });
    }

    function closeCommissionModal() {
        stopQrScan();  // release the camera if a scan is in progress
        if (_commLogTimer) { clearInterval(_commLogTimer); _commLogTimer = null; }
        $(document).off('keydown.matterComm');
        $('#matter-commission-overlay').remove();
    }

    // =========================================================================
    // QR scan — read the device label's MT:… code with this device's camera
    // (operator request 2026-07-13). Uses the native BarcodeDetector where the
    // browser has one (Chrome/Edge/Android); falls back to the vendored jsQR
    // (static/js/vendor/jsQR.js, MIT, lazy-loaded on first use) elsewhere
    // (Safari/iOS). The QR payload carries the FULL 12-bit discriminator —
    // strictly more reliable than the 11-digit manual code's 4-bit short form.
    // getUserMedia REQUIRES a secure context: https:// (or localhost) — on a
    // plain http:// LAN URL the browser hides the camera API entirely.
    // =========================================================================
    /**
     * Copy a log <pre>'s full text to the clipboard (operator request
     * 2026-07-13: diagnose outside the modal without retyping). Prefers the
     * async Clipboard API (needs a secure context); falls back to the
     * selection + execCommand path for plain-http LAN URLs.
     * @param {string} selector - the <pre> to copy (e.g. '#comm-log')
     */
    function copyLogText(selector) {
        const text = $(selector).text();
        if (!text) { showToast('Log is empty', 'info'); return; }
        const ok = () => showToast('Log copied to clipboard', 'success');
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(ok,
                () => showToast('Copy refused by the browser', 'error'));
            return;
        }
        // http:// fallback: hidden textarea + execCommand.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); ok(); }
        catch (e) { showToast('Copy failed: ' + e, 'error'); }
        document.body.removeChild(ta);
    }

    // Delegated handlers — the buttons live inside dynamically-built modals.
    $(document).on('click', '#comm-copylog', function () { copyLogText('#comm-log'); });
    $(document).on('click', '#dbg-copylog', function () { copyLogText('#dbg-log'); });

    // =========================================================================
    // GET CODE — a pairing code for any device (operator request 2026-07-13).
    //
    // ONE button, four honest sources, resolved server-side
    // (services/matter_pairing_codes): the device's stored FACTORY code from the
    // vault · a FRESH code from a commissioning window we open on our own fabric
    // · a FRESH code from a window opened by the Hubitat hub that owns it · or,
    // if the operator supplies the printed label code, that code REPAIRED to
    // target the discriminator the device is really advertising.
    //
    // When none applies the backend answers 409 with the reason — a Matter
    // passcode is a SPAKE2+ secret that cannot be derived from what a device
    // broadcasts, so for a device we administer nowhere and never vaulted, no
    // code can exist. We show that explanation rather than a button that fails.
    // =========================================================================
    const CODE_SOURCE_LABEL = {
        vault_factory:  'Factory code (from the vault)',
        ecm_our_fabric: 'Fresh code — window opened on our fabric',
        ecm_hubitat:    'Fresh code — window opened by the Hubitat hub',
        repaired_label: 'Your label code, repaired',
    };

    function showCodeModal(data, deviceName) {
        $('#matter-code-overlay').remove();
        const expires = data.expires_in_s
            ? `<div class="dbg-mono" style="margin-top:6px;">Valid for ${Math.round(data.expires_in_s / 60)} minutes — use it now.</div>`
            : '';
        const qrRow = data.qr_code
            ? `<div style="margin-top:12px;">
                 <div class="matter-debug-logbar">QR payload
                   <button class="btn btn-small btn-secondary" id="code-copy-qr" style="float:right;">Copy QR</button></div>
                 <pre class="matter-debug-log" id="code-qr" style="max-height:70px;">${escapeHtml(data.qr_code)}</pre>
               </div>`
            : '';
        $('body').append(`
          <div id="matter-code-overlay" class="matter-debug-overlay">
            <div class="matter-debug" style="max-width:560px;">
              <div class="matter-debug-head">
                <h3>Pairing code <span class="dbg-mono">${escapeHtml(deviceName || '')}</span></h3>
                <button class="btn btn-small btn-secondary matter-code-close">Close</button>
              </div>
              <div class="comm-body">
                <div class="comm-status success">${escapeHtml(CODE_SOURCE_LABEL[data.source] || data.source)}</div>
                <div class="matter-debug-logbar">Manual code
                  <button class="btn btn-small btn-secondary" id="code-copy-manual" style="float:right;">Copy code</button></div>
                <pre class="matter-debug-log" id="code-manual"
                     style="font-size:22px;letter-spacing:2px;max-height:60px;">${escapeHtml(data.manual_code)}</pre>
                ${expires}
                <p style="margin-top:10px;color:var(--text-dim,#c9c9d1);">${escapeHtml(data.detail || '')}</p>
                ${qrRow}
              </div>
            </div>
          </div>`);
        // Close ONLY via the button (never on backdrop — same rule as the other modals).
        $('#matter-code-overlay').on('click', function (e) {
            if ($(e.target).is('.matter-code-close')) $('#matter-code-overlay').remove();
        });
        $('#code-copy-manual').on('click', function () { copyLogText('#code-manual'); });
        $('#code-copy-qr').on('click', function () { copyLogText('#code-qr'); });
    }

    /**
     * Ask the backend for a pairing code for one device.
     * @param {object} body - {unique_id} or {our_node_id}, plus optional label_code
     * @param {string} deviceName - for the modal header
     */
    function getPairingCode(body, deviceName) {
        showToast('Getting a pairing code…', 'info');
        $.ajax({
            url: '/api/matter/pairing-code', method: 'POST',
            contentType: 'application/json', data: JSON.stringify(body), timeout: 60000,
        })
        .done(function (data) { showCodeModal(data, deviceName); })
        .fail(function (xhr) {
            // 409 = no source applies. That is an ANSWER, not a crash: show the
            // backend's explanation in full rather than a generic failure toast.
            const detail = xhr.responseJSON?.detail
                || `HTTP ${xhr.status}: could not get a code`;
            showModal(xhr.status === 409 ? 'No pairing code can be produced'
                                         : 'Could not get a pairing code',
                      detail, 'error');
        });
    }

    // Node cards (our fabric) and discovered-device cards (Hubitat fabric).
    $(document).on('click', '.btn-get-code', function () {
        const $b = $(this);
        const uid = $b.data('unique-id');
        const nodeId = $b.data('node-id');
        const name = $b.data('device-name') || '';
        getPairingCode(uid ? { unique_id: String(uid) }
                           : { our_node_id: Number(nodeId) }, name);
    });

    let _qrStream = null;   // active camera MediaStream (null = not scanning)
    let _qrTimer = null;    // decode-loop interval handle

    function stopQrScan() {
        if (_qrTimer) { clearInterval(_qrTimer); _qrTimer = null; }
        if (_qrStream) { _qrStream.getTracks().forEach(t => t.stop()); _qrStream = null; }
        $('#comm-qr-wrap').remove();
        $('#comm-scan').prop('disabled', false).text('📷 Scan');
    }

    function qrHit(text) {
        // Any decoded payload lands in the input (MT:… expected; the backend
        // accepts both QR strings and numeric codes), then the camera stops.
        $('#comm-code-input').val(String(text).trim());
        showToast('QR code captured', 'success');
        stopQrScan();
    }

    async function startQrScan() {
        if (_qrStream) { stopQrScan(); return; }  // toggle off
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showToast('Camera unavailable — open the app over https:// (secure context required)', 'error');
            return;
        }
        $('#comm-scan').prop('disabled', true).text('starting…');
        try {
            _qrStream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'environment' }, audio: false,
            });
        } catch (e) {
            $('#comm-scan').prop('disabled', false).text('📷 Scan');
            showToast('Camera refused: ' + (e.name || e), 'error');
            return;
        }
        $('#comm-qr-wrap').remove();
        $('.comm-code-row').after(
            '<div id="comm-qr-wrap" style="margin:8px 0;">' +
            '<video id="comm-qr-video" playsinline muted autoplay ' +
            'style="width:100%;max-height:260px;border-radius:8px;background:#000;object-fit:cover;"></video>' +
            '</div>');
        const video = document.getElementById('comm-qr-video');
        video.srcObject = _qrStream;
        await video.play().catch(() => {});
        $('#comm-scan').prop('disabled', false).text('Stop scan');

        if ('BarcodeDetector' in window) {
            // Native path — hardware-accelerated where available.
            const detector = new window.BarcodeDetector({ formats: ['qr_code'] });
            _qrTimer = setInterval(async function () {
                if (!_qrStream || video.readyState < 2) return;
                try {
                    const codes = await detector.detect(video);
                    if (codes.length) qrHit(codes[0].rawValue);
                } catch (e) { /* transient frame errors — keep scanning */ }
            }, 300);
        } else {
            // Fallback path — lazy-load the vendored decoder, scan via canvas.
            $.getScript('/static/js/vendor/jsQR.js').done(function () {
                const canvas = document.createElement('canvas');
                const ctx = canvas.getContext('2d', { willReadFrequently: true });
                _qrTimer = setInterval(function () {
                    if (!_qrStream || video.readyState < 2) return;
                    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
                    ctx.drawImage(video, 0, 0);
                    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
                    const hit = window.jsQR(img.data, img.width, img.height);
                    if (hit && hit.data) qrHit(hit.data);
                }, 350);
            }).fail(function () {
                showToast('QR decoder failed to load', 'error');
                stopQrScan();
            });
        }
    }

    /**
     * Open the live-log commission modal.
     * @param {object} opts
     *   title       header text
     *   subtitle    grey mono subtitle (e.g. the IP)
     *   needsCode   true = show a pairing-code input; false = one Start button
     *   code        prefill for the code input
     *   preStep     optional async () => Promise run before the commission POST
     *               (e.g. decommission-first for recommission)
     *   run         (code) => ({url, body}) — the commission request
     *   successText optional (resp) => string
     */
    function openCommissionModal(opts) {
        closeCommissionModal();
        _commLogSeq = 0; _commSrvOffset = -1; _commSrvAvail = false;
        const codeRow = opts.needsCode
            ? `<div class="comm-code-row">
                 <input type="text" id="comm-code-input" class="comm-code-input"
                        placeholder="Paste pairing code — MT:… QR string or numeric" value="${escapeHtml(opts.code || '')}">
                 <button class="btn btn-small btn-secondary" id="comm-scan"
                         title="Scan the device label's QR with this device's camera (needs https)">📷 Scan</button>
                 <button class="btn btn-small btn-secondary" id="comm-fixcode"
                         title="Commissioning says 'no device discovered' but the device IS in pairing mode? Its advertised discriminator has drifted from its label. This keeps the passcode and re-targets the code at what the device is actually broadcasting.">Fix code</button>
                 <button class="btn btn-small btn-primary" id="comm-start">Commission</button>
               </div>`
            : `<div class="comm-code-row"><button class="btn btn-small btn-primary" id="comm-start">Start commissioning</button></div>`;
        $('body').append(`
          <div id="matter-commission-overlay" class="matter-debug-overlay">
            <div class="matter-debug matter-commission">
              <div class="matter-debug-head">
                <h3>${escapeHtml(opts.title)} <span class="dbg-mono">${escapeHtml(opts.subtitle || '')}</span></h3>
                <button class="btn btn-small btn-secondary matter-debug-close">Close</button>
              </div>
              <div class="comm-body">
                ${codeRow}
                <div id="comm-status" class="comm-status">Live matter-client log streaming — start when ready.</div>
                <div class="matter-debug-logbar">Live matter-client log <span class="dbg-mono" id="comm-logstate"></span>
                  <button class="btn btn-small btn-secondary" id="comm-copylog" style="float:right;"
                          title="Copy the whole log to the clipboard">Copy logs</button></div>
                <pre class="matter-debug-log comm-log" id="comm-log"></pre>
              </div>
            </div>
          </div>`);
        // Close ONLY via the explicit Close button — NOT on backdrop click.
        // Operator directive 2026-07-13: clicking outside (to copy a pairing
        // code or select log text) was killing the modal mid-commission and
        // losing the code input + live log.
        $('#matter-commission-overlay').on('click', function (e) {
            if ($(e.target).is('.matter-debug-close')) closeCommissionModal();
        });
        $(document).on('keydown.matterComm', function (e) { if (e.key === 'Escape') closeCommissionModal(); });
        $('#comm-scan').on('click', startQrScan);
        // "Fix code": re-target the typed/scanned code at the discriminator the
        // device is ACTUALLY advertising (the 2026-07-13 plug rescue, automated).
        // Rewrites the input in place and explains what changed.
        $('#comm-fixcode').on('click', function () {
            const code = $('#comm-code-input').val().trim();
            if (!code) { showToast('Paste or scan a code first', 'error'); return; }
            const $btn = $(this).prop('disabled', true).text('checking…');
            $.ajax({ url: '/api/matter/pairing-code/repair', method: 'POST',
                     contentType: 'application/json',
                     data: JSON.stringify({ code: code }), timeout: 30000 })
            .done(function (r) {
                $('#comm-code-input').val(r.manual_code);
                showModal(r.changed ? 'Code repaired' : 'Code is already correct',
                          r.detail, r.changed ? 'success' : 'info');
            })
            .fail(function (xhr) {
                showModal('Could not repair the code',
                          xhr.responseJSON?.detail || `HTTP ${xhr.status}`, 'error');
            })
            .always(function () { $btn.prop('disabled', false).text('Fix code'); });
        });
        $('#comm-start').on('click', function () {
            const code = opts.needsCode ? $('#comm-code-input').val().trim() : null;
            if (opts.needsCode && !code) { showToast('Paste a pairing code first', 'error'); return; }
            stopQrScan();  // release the camera before the commission run
            runCommission(opts, code);
        });
        // Stream the log immediately, so pre-commission activity is visible too.
        pollCommLog();
        _commLogTimer = setInterval(pollCommLog, 1000);
    }

    function runCommission(opts, code) {
        $('#comm-start').prop('disabled', true).text('Working…');
        const setStatus = (cls, txt) => $('#comm-status').removeClass('loading success error').addClass(cls).text(txt);
        const doPost = function () {
            const rb = opts.run(code);
            setStatus('loading', 'Commissioning — watch the log below (can take 30–90s)…');
            $.ajax({ url: rb.url, method: 'POST', contentType: 'application/json',
                     data: JSON.stringify(rb.body || {}), timeout: 180000 })
            .done(function (r) {
                const node = (r.node && (r.node.node_id ?? r.node)) ?? r.our_node_id;
                const cleaned = (r.orphans_cleared && r.orphans_cleared.length)
                    ? ` · self-cleaned ${r.orphans_cleared.length} orphan fabric(s)` : '';
                setStatus('success', opts.successText ? opts.successText(r)
                    : `Commissioned — node ${node ?? '?'}${cleaned}.`);
                pollCommLog();
                loadNodes(); loadDiscoveredDevices(); loadMappings();
            })
            .fail(function (xhr) {
                let msg = xhr.responseJSON?.detail;
                if (!msg) {
                    msg = (xhr.status === 502 || xhr.status === 504 || xhr.status === 0)
                        ? `HTTP ${xhr.status || 'timeout'} with no detail — commissioning can outlast a proxy timeout; `
                          + `it may still have completed (see the log below / Refresh for a new node).`
                        : `HTTP ${xhr.status}: ${xhr.statusText || 'commission failed'}`;
                }
                setStatus('error', 'Failed: ' + msg);
                pollCommLog();
            })
            .always(function () {
                $('#comm-start').prop('disabled', false).text(opts.needsCode ? 'Commission' : 'Start commissioning');
            });
        };
        if (opts.preStep) {
            setStatus('loading', 'Preparing (decommissioning existing node first)…');
            Promise.resolve(opts.preStep()).then(doPost).catch(doPost);  // proceed even if pre-step errors
        } else {
            doPost();
        }
    }

    function updateHubToggleLabel(selected) {
        const $t = $('#matter-hub-toggle');
        if (!$t.length) return;
        const names = (selected || []).map(h => h.hub_name);
        $t.text(names.length ? `Matter hubs: ${names.join(', ')} ▾` : 'Select Matter hubs ▾');
    }

    function loadMatterHub() {
        $.getJSON('/api/matter/hub').done(function (d) {
            const $panel = $('#matter-hub-panel');
            if (!$panel.length) return;
            const selIds = new Set(d.selected_ids || []);
            $panel.empty();
            (d.hubs || []).forEach(function (h) {
                // Thread badge: C-8 family hubs have a BUILT-IN Thread border
                // router — decides whether Thread Matter devices can commission
                // through this hub (the "?" beside the picker explains it).
                const thread = h.has_thread_br ? ' · Thread' : '';
                const hw = h.hardware_version ? ` [${h.hardware_version}]` : '';
                const label = `${h.hub_name} (${h.hub_ip})${hw}${thread}${h.is_primary ? ' — main' : ''}`;
                $panel.append(
                    `<label class="matter-hub-opt"><input type="checkbox" class="matter-hub-cb" value="${h.id}"`
                    + `${selIds.has(h.id) ? ' checked' : ''}> ${escapeHtml(label)}</label>`);
            });
            updateHubToggleLabel(d.selected || []);
        }).fail(function () {
            $('#matter-hub-panel').html('<div class="matter-hub-opt">hubs unavailable</div>');
        });
    }

    /**
     * Trigger a scan of THE selected Matter hub for Matter devices
     * (single-hub policy — never scans the other hubs).
     * Shows progress, then refreshes the discovered devices table.
     * NEVER auto-commissions anything.
     */
    function scanHubs() {
        const $status = $('#scan-status');
        const $btn = $('#btn-scan-hubs');

        $status.removeClass('success error loading')
            .addClass('loading')
            .text('Scanning the selected Matter hub for Matter devices...')
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
            let msg = `Discovered ${count} device(s) on the Matter hub.`;
            if (errCount > 0) {
                msg += ` ${errCount} hub(s) had errors.`;
            }
            $status.removeClass('loading').addClass('success').text(msg);
            loadDiscoveredDevices();
            // Also refresh nodes in case some were already commissioned
            loadNodes();
            // NOTE: scanning NEVER auto-commissions. The scan→commissionAll
            // auto-chain that used to live here (since 2026-02-21) was removed
            // 2026-07-11 — it mass-commissioned every device after each scan
            // click, saturating device fabric slots ("CHIP 0x0B No memory").
            // Bulk commissioning is ONLY the explicit "Commission All" button.
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
                '<div class="matter-empty">No discovered devices yet. Click "Scan Hub" to scan the selected Matter hub.</div>'
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
            actionHtml += `<button class="btn btn-small btn-secondary btn-get-code"
                            data-unique-id="${uid}" data-device-name="${name}"
                            title="Get a pairing code for this device — its stored factory code if we have one, otherwise a fresh code from a window opened by the hub that owns it">Get Code</button>`;
            actionHtml += `<button class="btn btn-small btn-danger btn-remove-discovered"
                            data-uid="${uid}" data-name="${name}"
                            title="Remove: decommission from our fabric + soft-delete (row kept, a re-scan brings it back)">Remove</button>`;
            actionHtml += `<button class="btn btn-small btn-danger btn-force-remove-discovered"
                            data-uid="${uid}" data-name="${name}"
                            title="Force remove: DB-only soft-delete, skips decommission — for dead/ghost devices">Force</button>`;
            // Debug on EVERY card — commissioned or not (operator ask: a
            // decommissioned device must still open the console for server
            // diagnostics + the live log; node-scoped sections degrade
            // gracefully when there's no node).
            actionHtml += `<button class="btn btn-small btn-secondary btn-debug-node"
                            data-node="${d.our_node_id != null ? d.our_node_id : ''}" data-name="${name}" data-uid="${uid}"
                            title="Matter debug console — fabric table, per-fabric remove, commission/decommission, live log">Debug</button>`;

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
                            ${(d.also_on_hubs && d.also_on_hubs.length)
                                ? `<span class="also-on-chip" title="Same physical device (matched by MAC) is also paired on: ${escapeHtml(d.also_on_hubs.join(', '))}. Shown once; commissioned once via ${escapeHtml(d.hub_name || d.hub_ip)}.">+${d.also_on_hubs.length} hub${d.also_on_hubs.length > 1 ? 's' : ''}</span>`
                                : ''}
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
            openMatterDebug($(this).data('node'), $(this).data('name'), $(this).data('uid'));
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
        // Hub-card commission — via the LIVE-LOG modal so the operator sees the
        // openPairingWindow → PASE/CASE → SendNOC steps as they happen. No code
        // input: the backend fetches the setup code from the device's own hub.
        openCommissionModal({
            title: `Commission ${deviceName}`,
            subtitle: 'opens a pairing window on the device’s hub, then commissions',
            needsCode: false,
            run: () => ({ url: '/api/matter/auto-commission', body: { unique_id: uniqueId } }),
        });
    }

    /**
     * Commission ALL online, uncommissioned devices on the selected Matter hub.
     * USER-INITIATED ONLY — runs from the "Commission All" button click and
     * nothing else (the automatic after-scan trigger was removed 2026-07-11).
     * @param {jQuery} $status - optional status element to update progress
     * @param {string} baseMsg - optional base message to prepend to status
     */
    function commissionAll($status, baseMsg) {
        // Explicit-user gate, both ends: this confirm dialog here, and the
        // backend 409s any call that doesn't carry {confirmed:true} — so bulk
        // commissioning can never fire without the operator asking.
        //
        // SEQUENTIAL + BACKGROUND (2026-07-12): the backend now commissions
        // strictly ONE device at a time (Hubitat can only run one pairing
        // window), with a settle pause between devices, as a background run.
        // POST returns immediately; we POLL .../status and live-update the
        // banner until the run finishes.
        if (!confirm(
            'Commission ALL online, uncommissioned devices on the selected Matter hubs?\n\n'
            + 'Devices are commissioned ONE AT A TIME (a pairing window per device, '
            + 'with a short settle pause between them so the hub keeps up). '
            + 'Expect roughly 20-30s per device. Devices on unselected hubs are not touched.')) {
            return;
        }
        const statusEl = $status || $('#scan-status');
        const prefix = baseMsg ? baseMsg + ' ' : '';

        statusEl.removeClass('success error')
            .addClass('loading')
            .text(prefix + 'Starting sequential commission run...')
            .show();

        // Poll the background run until it reports running:false, painting
        // progress (done/total + current device) into the banner as it goes.
        function pollRun() {
            $.ajax({ url: '/api/matter/auto-commission-all/status', method: 'GET' })
            .done(function (st) {
                if (st.running) {
                    const cur = st.current ? ` — ${st.current}` : '';
                    statusEl.text(`${prefix}Commissioning ${st.done}/${st.total}`
                        + ` (ok ${st.ok}, failed ${st.failed})${cur}`);
                    setTimeout(pollRun, 2500);
                    return;
                }
                // Finished (or no run state after an app restart mid-run).
                const msg = st.message || 'Commission run finished';
                const cls = (st.failed > 0 || st.aborted) ? 'error' : 'success';
                statusEl.removeClass('loading').addClass(cls).text(prefix + msg);
                if (st.results) {
                    const errs = st.results.filter(r => r.status === 'error');
                    if (errs.length) console.warn('Commission failures:', errs);
                }
                // Refresh everything
                loadNodes();
                loadMappings();
                loadDiscoveredDevices();
            })
            .fail(function () {
                // Transient poll failure (reload/restart) — keep trying briefly.
                setTimeout(pollRun, 4000);
            });
        }

        $.ajax({
            url: '/api/matter/auto-commission-all',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ confirmed: true }),
            timeout: 30000  // start-only: the run itself is backgrounded
        })
        .done(function (data) {
            if (!data.started) {   // nothing to do (0 devices)
                statusEl.removeClass('loading').addClass('success')
                    .text(prefix + (data.message || 'Nothing to commission'));
                return;
            }
            statusEl.text(`${prefix}${data.message} — waiting for first device...`);
            setTimeout(pollRun, 1500);
        })
        .fail(function (xhr) {
            // 409 "already in progress" → just attach to the running job.
            if (xhr.status === 409 && (xhr.responseJSON?.detail || '').includes('already in progress')) {
                statusEl.text(prefix + 'A run is already in progress — attaching...');
                setTimeout(pollRun, 1000);
                return;
            }
            const detail = xhr.responseJSON?.detail || 'Bulk commission failed';
            statusEl.removeClass('loading').addClass('error')
                .text(prefix + 'Commission error: ' + detail);
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

    // =========================================================================
    // MATTER TERMINAL DOCK (2026-07-12) — persistent right-side live terminal
    // streaming the SAME two feeds as the commission modal, page-wide:
    //   [cli] /api/matter/debug/log     (our WS commands/results, seq cursor)
    //   [srv] /api/matter/server-log    (matterjs CHIP log, byte-offset cursor)
    // Separate cursors from the modal's so both can run at once. Collapsible
    // (edge tab), drag-resizable via the left grip (320px..95vw), both
    // persisted in localStorage. Polling stops while collapsed or hidden.
    // =========================================================================
    const _DOCK_POLL_MS = 1500, _DOCK_MAX_LINES = 2500;
    let _dockTimer = null, _dockCliSeq = 0, _dockSrvOffset = -1, _dockPaused = false;

    function _dockAppend(html) {
        const $log = $('#matter-dock-log');
        const el = $log[0];
        if (!el) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        $log.append(html);
        // Trim the oldest lines so an all-night session can't grow unbounded.
        const kids = el.children;
        if (kids.length > _DOCK_MAX_LINES) {
            for (let i = kids.length - _DOCK_MAX_LINES; i > 0; i--) el.removeChild(el.firstChild);
        }
        if (atBottom) el.scrollTop = el.scrollHeight;
    }

    function _dockPoll() {
        if (_dockPaused) return;
        const showCli = $('#dock-show-cli').is(':checked');
        const showSrv = $('#dock-show-srv').is(':checked');
        // 1) client op-log — cursor always advances (even when hidden) so
        //    re-enabling [cli] doesn't dump the backlog.
        $.getJSON(`/api/matter/debug/log?since_seq=${_dockCliSeq}&limit=120`).done(function (d) {
            if (d.records && d.records.length) {
                if (showCli) {
                    let html = '';
                    d.records.forEach(function (r) {
                        const cls = r.level === 'ERROR' ? 'dbg-bad' : (r.level === 'WARNING' ? 'dbg-warn' : '');
                        html += `<span class="${cls}">${r.ts.slice(11, 19)} <b>[cli]</b> ${escapeHtml(r.msg)}</span>\n`;
                    });
                    _dockAppend(html);
                }
                _dockCliSeq = d.last_seq;
            }
            $('#matter-dock-state').text(`cli seq ${_dockCliSeq} · srv @${_dockSrvOffset}${_dockPaused ? ' · PAUSED' : ''}`);
        });
        // 2) matterjs CHIP log — first poll only seeds the tail offset.
        $.getJSON(`/api/matter/server-log?since=${_dockSrvOffset}`).done(function (s) {
            if (s.available && s.lines && s.lines.length && _dockSrvOffset >= 0 && showSrv) {
                let html = '';
                s.lines.forEach(function (ln) {
                    const low = ln.toLowerCase();
                    const cls = (low.includes('error') || low.includes('(133)') || low.includes('failed')
                                 || low.includes('invalidcommand')) ? 'dbg-bad'
                              : (low.includes('warn') ? 'dbg-warn' : 'dbg-srv');
                    html += `<span class="${cls}"><b>[srv]</b> ${escapeHtml(ln)}</span>\n`;
                });
                _dockAppend(html);
            }
            if (s.available) _dockSrvOffset = s.offset;
        });
    }

    function _dockSetCollapsed(collapsed) {
        $('#matter-dock').toggleClass('collapsed', collapsed);
        localStorage.setItem('matterDock.collapsed', collapsed ? '1' : '0');
        if (collapsed) {
            if (_dockTimer) { clearInterval(_dockTimer); _dockTimer = null; }
        } else if (!_dockTimer) {
            _dockPoll();
            _dockTimer = setInterval(_dockPoll, _DOCK_POLL_MS);
        }
    }

    function initMatterDock() {
        const $dock = $('#matter-dock');
        if (!$dock.length) return;

        // Restore persisted width + collapsed state (default: collapsed).
        const w = parseInt(localStorage.getItem('matterDock.width') || '480', 10);
        $dock.css('width', Math.min(Math.max(w, 320), window.innerWidth * 0.95) + 'px');
        _dockSetCollapsed(localStorage.getItem('matterDock.collapsed') !== '0');

        $('#matter-dock-tab').on('click', function () {
            _dockSetCollapsed(!$dock.hasClass('collapsed'));
        });
        $('#dock-clear').on('click', function () { $('#matter-dock-log').empty(); });
        $('#dock-pause').on('click', function () {
            _dockPaused = !_dockPaused;
            $(this).text(_dockPaused ? 'Resume' : 'Pause');
        });

        // Drag-to-resize: grip tracks the pointer; width = viewport right
        // edge minus pointer X, clamped [320px .. 95vw], persisted on release.
        $('#matter-dock-grip').on('mousedown', function (e) {
            e.preventDefault();
            $dock.addClass('dragging');
            $('body').addClass('matter-dock-resizing');
            function onMove(ev) {
                const px = Math.min(Math.max(window.innerWidth - ev.clientX, 320),
                                    window.innerWidth * 0.95);
                $dock.css('width', px + 'px');
            }
            function onUp() {
                $(document).off('mousemove', onMove).off('mouseup', onUp);
                $dock.removeClass('dragging');
                $('body').removeClass('matter-dock-resizing');
                localStorage.setItem('matterDock.width', parseInt($dock.css('width'), 10));
            }
            $(document).on('mousemove', onMove).on('mouseup', onUp);
        });
    }

    initMatterDock();
});
