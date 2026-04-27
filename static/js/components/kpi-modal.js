/**
 * KPI Modal Component
 *
 * Opens a full-screen (95vh) modal showing detailed metrics for a single
 * automation instance. Data comes from /api/instances/{id}/metrics.
 *
 * Sections:
 *   1. Overview — status, uptime, event totals, error count
 *   2. Activity Timeline — hourly bar chart (Chart.js)
 *   3. Device Activity — per-device event stats table
 *   4. Event Breakdown — pie chart by event type
 *   5. Instance Config — settings and device selections summary
 *
 * Uses Chart.js (loaded via CDN in base.html) for visualizations.
 * Follows the dtm- (device-tile-modal) CSS prefix pattern → kpi-
 */

import { api, utils } from '../main.js';

/* =============================================================================
   State
   ============================================================================= */

/** Currently open modal backdrop (jQuery) */
let $activeBackdrop = null;

/** Chart.js instances (destroyed on modal close to prevent memory leaks) */
let _charts = [];

/** Auto-refresh interval for live data */
let _refreshInterval = null;

/* =============================================================================
   Public API
   ============================================================================= */

/**
 * Open the KPI modal for an instance.
 *
 * @param {number} instanceId - Instance ID
 * @param {string} instanceLabel - Display label for the header
 */
export async function openKpiModal(instanceId, instanceLabel) {
    if ($activeBackdrop) {
        closeKpiModal();
    }

    $activeBackdrop = _createBackdrop();
    const $modal = $activeBackdrop.find('.kpi-modal');
    $modal.html('<div class="kpi-loading">Loading metrics...</div>');

    $('body').append($activeBackdrop);
    requestAnimationFrame(() => $activeBackdrop.addClass('show'));

    await _loadAndRender($modal, instanceId, instanceLabel);

    // Auto-refresh every 30s while modal is open
    _refreshInterval = setInterval(async () => {
        if ($activeBackdrop) {
            await _loadAndRender($modal, instanceId, instanceLabel, true);
        }
    }, 30000);
}

/**
 * Close the active KPI modal and clean up.
 */
export function closeKpiModal() {
    if (_refreshInterval) {
        clearInterval(_refreshInterval);
        _refreshInterval = null;
    }
    _destroyCharts();
    _detachDebugStream();

    if (!$activeBackdrop) return;
    $activeBackdrop.removeClass('show');
    setTimeout(() => {
        if ($activeBackdrop) {
            $activeBackdrop.remove();
            $activeBackdrop = null;
        }
    }, 300);
}


/* =============================================================================
   Debug Terminal — live event stream for the open KPI modal
   ============================================================================= */

let _debugStream = null;
let _debugPaused = false;

function _appendDebugLine(html) {
    if (_debugPaused) return;
    const log = document.getElementById('kpi-debug-log');
    if (!log) return;
    const wasAtBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 40;
    const div = document.createElement('div');
    div.className = 'kpi-debug-line';
    div.innerHTML = html;
    log.appendChild(div);
    // Cap at 1000 lines so a long-open modal doesn't grow unbounded.
    while (log.children.length > 1000) {
        log.removeChild(log.firstChild);
    }
    if (wasAtBottom) log.scrollTop = log.scrollHeight;
}

function _formatDebugTime(d) {
    return d.toLocaleTimeString([], { hour12: false })
        + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function _attachDebugStream(instanceId, metrics) {
    _detachDebugStream();
    _debugPaused = false;

    // Build a canonical-id → device-row map from the just-rendered metrics
    // so log lines can hyperlink to the device's hub edit page.
    const devById = {};
    for (const [cid, stats] of Object.entries(metrics.device_stats || {})) {
        devById[String(cid)] = stats;
    }

    const $log = $('#kpi-debug-log').empty();
    const $status = $('#kpi-debug-status').text('Connecting…');

    // Reuse the existing per-instance SSE stream that the e2e modal also
    // consumes. It broadcasts both webhook device-events and (for the
    // currently-running e2e scenario, if any) test step events.
    try {
        _debugStream = new EventSource(
            `/api/e2e/events/stream?instance_id=${instanceId}`
        );
    } catch (e) {
        $status.text(`SSE error: ${e.message}`);
        return;
    }

    _debugStream.onopen = () => $status.text('Connected');
    _debugStream.onerror = () => $status.text('Disconnected — will retry');

    _debugStream.onmessage = (ev) => {
        let data;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        const t = _formatDebugTime(new Date());

        // Event payloads vary; try to render the common shapes nicely.
        const type = data.type || 'event';
        let body = '';
        if (type === 'device_event') {
            const cid = String(data.canonical_id ?? data.device_id ?? '');
            const dev = devById[cid] || {};
            const hubIp = data.hub_ip || dev.hub_ip || '';
            const hubitatId = String(data.hubitat_id ?? dev.hubitat_id ?? '');
            const name = utils.escapeHtml(
                data.device_name || dev.device_name || `#${cid}`
            );
            const linked = (hubIp && hubitatId)
                ? `<a class="kpi-device-link" href="http://${hubIp}/device/edit/${utils.escapeHtml(hubitatId)}" target="_blank" rel="noopener">${name}</a>`
                : name;
            const evName = utils.escapeHtml(data.event_name || '');
            const evValue = utils.escapeHtml(String(data.event_value ?? ''));
            const meta = [];
            if (cid) meta.push(`canon #${utils.escapeHtml(cid)}`);
            if (hubitatId) meta.push(`hubitat #${utils.escapeHtml(hubitatId)}`);
            if (hubIp) meta.push(`hub ${utils.escapeHtml(hubIp)}`);
            body = `${linked} <span class="kpi-debug-meta">[${meta.join(' · ')}]</span> ${evName} = <b>${evValue}</b>`;
        } else {
            body = utils.escapeHtml(JSON.stringify(data));
        }

        _appendDebugLine(`<span class="kpi-debug-time">${t}</span> <span class="kpi-debug-type kpi-debug-type-${utils.escapeHtml(type)}">${utils.escapeHtml(type)}</span> ${body}`);
    };

    // Wire the Pause / Clear buttons (idempotent — re-bind on each refresh).
    $(document).off('click.kpi-debug').on('click.kpi-debug', '.kpi-debug-pause', function () {
        const $b = $(this);
        _debugPaused = !_debugPaused;
        $b.attr('data-paused', _debugPaused).text(_debugPaused ? 'Resume' : 'Pause');
    });
    $(document).off('click.kpi-debug-clear').on('click.kpi-debug-clear', '.kpi-debug-clear', function () {
        const log = document.getElementById('kpi-debug-log');
        if (log) log.innerHTML = '';
    });
}

function _detachDebugStream() {
    if (_debugStream) {
        try { _debugStream.close(); } catch (_) {}
        _debugStream = null;
    }
    $(document).off('click.kpi-debug click.kpi-debug-clear');
}

/* =============================================================================
   Modal Structure
   ============================================================================= */

/**
 * Create the modal backdrop and container.
 * @returns {jQuery} Backdrop element
 */
function _createBackdrop() {
    const $backdrop = $('<div class="kpi-backdrop">')
        .on('click', function (e) {
            if (e.target === this) closeKpiModal();
        });

    const $close = $('<button class="kpi-close">&times;</button>')
        .on('click', closeKpiModal);

    const $modal = $('<div class="kpi-modal">').append($close);
    $backdrop.append($modal);
    return $backdrop;
}

/* =============================================================================
   Data Loading & Rendering
   ============================================================================= */

/**
 * Fetch metrics and render all KPI sections.
 *
 * @param {jQuery} $modal - Modal container
 * @param {number} instanceId - Instance ID
 * @param {string} instanceLabel - Display label
 * @param {boolean} isRefresh - If true, preserve scroll position
 */
async function _loadAndRender($modal, instanceId, instanceLabel, isRefresh = false) {
    const scrollTop = isRefresh ? $modal.scrollTop() : 0;

    try {
        const metrics = await api.get(`/instances/${instanceId}/metrics?hours=24`);

        _destroyCharts();

        const html = `
            <div class="kpi-header">
                <div class="kpi-header-left">
                    <h2>${utils.escapeHtml(instanceLabel)}</h2>
                    <div class="kpi-subtitle">Instance #${instanceId} — Last 24 hours</div>
                </div>
                <div class="kpi-header-right">
                    <div class="kpi-status-badge ${metrics.is_paused ? 'paused' : metrics.is_running ? 'running' : 'stopped'}">
                        ${metrics.is_paused ? 'PAUSED' : metrics.is_running ? 'RUNNING' : 'STOPPED'}
                    </div>
                </div>
            </div>

            <div class="kpi-overview">
                ${_renderOverviewCards(metrics)}
            </div>

            <div class="kpi-charts-row">
                <div class="kpi-chart-container kpi-chart-wide">
                    <h3>Activity Timeline</h3>
                    <canvas id="kpi-hourly-chart"></canvas>
                </div>
                <div class="kpi-chart-container kpi-chart-narrow">
                    <h3>Event Types</h3>
                    <canvas id="kpi-type-chart"></canvas>
                </div>
            </div>

            <div class="kpi-section">
                <h3>Device Activity</h3>
                ${_renderDeviceTable(metrics)}
            </div>

            <div class="kpi-section kpi-debug-section">
                <div class="kpi-debug-header">
                    <h3>Debug Terminal</h3>
                    <div class="kpi-debug-controls">
                        <button class="btn btn-secondary btn-small kpi-debug-pause" data-paused="false">Pause</button>
                        <button class="btn btn-secondary btn-small kpi-debug-clear">Clear</button>
                        <span class="kpi-debug-status" id="kpi-debug-status">Connecting…</span>
                    </div>
                </div>
                <div class="kpi-debug-log" id="kpi-debug-log"></div>
            </div>

            <div class="kpi-section">
                <h3>Configuration</h3>
                ${_renderConfigSummary(metrics)}
            </div>
        `;

        $modal.find('.kpi-close').length
            ? $modal.children(':not(.kpi-close)').remove() && $modal.append(html)
            : $modal.html('<button class="kpi-close">&times;</button>' + html);

        $modal.find('.kpi-close').off('click').on('click', closeKpiModal);

        // Render charts after DOM is ready
        requestAnimationFrame(() => {
            _renderHourlyChart(metrics);
            _renderTypeChart(metrics);
            _attachDebugStream(instanceId, metrics);
        });

        if (isRefresh) {
            $modal.scrollTop(scrollTop);
        }

    } catch (error) {
        console.error('Failed to load KPI metrics:', error);
        if (!isRefresh) {
            $modal.html(
                '<button class="kpi-close">&times;</button>'
                + `<div class="kpi-error">Failed to load metrics: ${error.message}</div>`
            );
            $modal.find('.kpi-close').on('click', closeKpiModal);
        }
    }
}

/* =============================================================================
   Overview Cards
   ============================================================================= */

/**
 * Render the top-level KPI summary cards.
 * @param {object} metrics - Metrics data from API
 * @returns {string} HTML
 */
function _renderOverviewCards(metrics) {
    const cards = [
        {
            label: 'Total Events',
            value: metrics.total_events.toLocaleString(),
            detail: `in last ${metrics.window_hours}h`,
            icon: '&#x1F4CA;'
        },
        {
            label: 'Devices',
            value: metrics.device_count,
            detail: _deviceBreakdown(metrics.device_selections),
            icon: '&#x1F4F1;'
        },
        {
            label: 'Last Activity',
            value: metrics.last_activity_at
                ? _timeAgo(metrics.last_activity_at)
                : 'Never',
            detail: metrics.last_activity_at
                ? new Date(metrics.last_activity_at).toLocaleTimeString()
                : '',
            icon: '&#x23F1;'
        },
        {
            label: 'Errors',
            value: metrics.error_count || 0,
            detail: metrics.last_error
                ? utils.escapeHtml(metrics.last_error.substring(0, 50))
                : 'None',
            icon: '&#x26A0;',
            warn: (metrics.error_count || 0) > 0
        },
        {
            label: 'Uptime',
            value: metrics.created_at
                ? _timeSince(metrics.created_at)
                : 'N/A',
            detail: metrics.created_at
                ? 'Since ' + new Date(metrics.created_at).toLocaleDateString()
                : '',
            icon: '&#x2B06;'
        },
        {
            label: 'Events/Hour',
            value: metrics.total_events > 0
                ? (metrics.total_events / metrics.window_hours).toFixed(1)
                : '0',
            detail: 'average rate',
            icon: '&#x26A1;'
        }
    ];

    return cards.map(c => `
        <div class="kpi-card ${c.warn ? 'kpi-card-warn' : ''}">
            <div class="kpi-card-icon">${c.icon}</div>
            <div class="kpi-card-body">
                <div class="kpi-card-value">${c.value}</div>
                <div class="kpi-card-label">${c.label}</div>
                <div class="kpi-card-detail">${c.detail}</div>
            </div>
        </div>
    `).join('');
}

/**
 * Build a short device category breakdown string.
 * @param {object} selections - device_selections JSONB
 * @returns {string}
 */
function _deviceBreakdown(selections) {
    if (!selections) return '';
    return Object.entries(selections)
        .map(([cat, ids]) => `${ids.length} ${cat.replace(/_/g, ' ')}`)
        .join(', ');
}

/* =============================================================================
   Charts
   ============================================================================= */

/**
 * Render the hourly activity bar chart.
 * @param {object} metrics - Metrics data
 */
function _renderHourlyChart(metrics) {
    const canvas = document.getElementById('kpi-hourly-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    const labels = metrics.hourly_events.map(h => {
        const d = new Date(h.hour);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    });
    const data = metrics.hourly_events.map(h => h.count);

    const chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Events',
                data,
                backgroundColor: 'rgba(74, 159, 216, 0.6)',
                borderColor: 'rgba(74, 159, 216, 1)',
                borderWidth: 1,
                borderRadius: 3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => {
                            const idx = items[0].dataIndex;
                            return new Date(metrics.hourly_events[idx].hour)
                                .toLocaleString([], {
                                    month: 'short', day: 'numeric',
                                    hour: '2-digit', minute: '2-digit'
                                });
                        }
                    }
                }
            },
            scales: {
                x: {
                    ticks: {
                        color: 'rgba(255,255,255,0.5)',
                        maxRotation: 45,
                        maxTicksLimit: 12
                    },
                    grid: { color: 'rgba(255,255,255,0.05)' }
                },
                y: {
                    beginAtZero: true,
                    ticks: {
                        color: 'rgba(255,255,255,0.5)',
                        stepSize: 1
                    },
                    grid: { color: 'rgba(255,255,255,0.05)' }
                }
            }
        }
    });
    _charts.push(chart);
}

/**
 * Render the event type doughnut chart.
 * @param {object} metrics - Metrics data
 */
function _renderTypeChart(metrics) {
    const canvas = document.getElementById('kpi-type-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    const entries = Object.entries(metrics.type_counts || {});
    if (entries.length === 0) return;

    // Sort by count descending
    entries.sort((a, b) => b[1] - a[1]);

    const palette = [
        '#4A9FD8', '#E89B3C', '#22c55e', '#ef4444',
        '#a855f7', '#ec4899', '#14b8a6', '#f59e0b',
        '#6366f1', '#84cc16'
    ];

    const chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: entries.map(e => e[0]),
            datasets: [{
                data: entries.map(e => e[1]),
                backgroundColor: entries.map((_, i) => palette[i % palette.length]),
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '55%',
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: 'rgba(255,255,255,0.7)',
                        padding: 12,
                        usePointStyle: true,
                        pointStyleWidth: 10
                    }
                }
            }
        }
    });
    _charts.push(chart);
}

/**
 * Destroy all active Chart.js instances.
 */
function _destroyCharts() {
    for (const chart of _charts) {
        chart.destroy();
    }
    _charts = [];
}

/* =============================================================================
   Device Activity Table
   ============================================================================= */

/**
 * Render a sortable device activity table.
 * @param {object} metrics - Metrics data
 * @returns {string} HTML
 */
function _renderDeviceTable(metrics) {
    const devices = Object.entries(metrics.device_stats || {});

    if (devices.length === 0) {
        return '<div class="kpi-empty">No device activity in this period.</div>';
    }

    // Sort by event count descending
    devices.sort((a, b) => b[1].event_count - a[1].event_count);

    const rows = devices.map(([devId, stats]) => {
        const typeBreakdown = Object.entries(stats.type_breakdown || {})
            .map(([t, d]) => `<span class="kpi-type-chip">${utils.escapeHtml(t)}: ${d.count}</span>`)
            .join(' ');

        const lastTime = stats.last_event_at
            ? (() => {
                // HH:MM:SS.mmm — millisecond precision matters for
                // ordering closely-spaced (e.g. mesh-mirror) events.
                const _d = new Date(stats.last_event_at);
                return _d.toLocaleTimeString([], { hour12: false })
                    + '.' + String(_d.getMilliseconds()).padStart(3, '0');
            })()
            : 'N/A';

        // Backend (Phase 5+) enriches each device_stats entry with the
        // hub_ip, hub_name, and Hubitat per-hub id from the canonical
        // `devices` table joined with `hub_config`. Render the device
        // name as a hyperlink to its edit page on the OWNING hub, and
        // expose both ids in the row so the user can tell at a glance
        // which is canonical (#) and which is the per-hub Hubitat id.
        const name = utils.escapeHtml(stats.device_name || '');
        const hubIp = stats.hub_ip || '';
        const hubName = stats.hub_name || '';
        const hubitatId = stats.hubitat_id != null ? String(stats.hubitat_id) : '';

        const nameHtml = (hubIp && hubitatId)
            ? `<a class="kpi-device-link" href="http://${hubIp}/device/edit/${utils.escapeHtml(hubitatId)}" target="_blank" rel="noopener" title="Open on hub ${utils.escapeHtml(hubName || hubIp)}">${name}</a>`
            : name;

        // Canonical id is always shown. Hubitat id and hub are shown when
        // we have them (post-Phase-5 backend enrichment).
        const idsHtml = hubitatId
            ? `<div class="kpi-device-id">canon <span class="kpi-id-canon">#${utils.escapeHtml(devId)}</span> · `
              + `${utils.escapeHtml(hubName || 'hub')} <span class="kpi-id-hubitat">#${utils.escapeHtml(hubitatId)}</span></div>`
            : `<div class="kpi-device-id">canon <span class="kpi-id-canon">#${utils.escapeHtml(devId)}</span></div>`;

        return `
            <tr>
                <td class="kpi-td-device">
                    <div class="kpi-device-name">${nameHtml}</div>
                    ${idsHtml}
                </td>
                <td class="kpi-td-count">${stats.event_count}</td>
                <td class="kpi-td-last">
                    <div>${lastTime}</div>
                    <div class="kpi-last-detail">
                        ${utils.escapeHtml(stats.last_event_type)} = ${utils.escapeHtml(stats.last_event_value)}
                    </div>
                </td>
                <td class="kpi-td-breakdown">${typeBreakdown}</td>
            </tr>
        `;
    }).join('');

    return `
        <div class="kpi-table-scroll">
            <table class="kpi-table">
                <thead>
                    <tr>
                        <th>Device</th>
                        <th>Events</th>
                        <th>Last Event</th>
                        <th>Breakdown</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

/* =============================================================================
   Configuration Summary
   ============================================================================= */

/**
 * Render instance configuration summary.
 * @param {object} metrics - Metrics data
 * @returns {string} HTML
 */
function _renderConfigSummary(metrics) {
    const settings = metrics.settings || {};
    const selections = metrics.device_selections || {};

    const settingsRows = Object.entries(settings)
        .map(([key, val]) => `
            <tr>
                <td class="kpi-config-key">${utils.escapeHtml(key)}</td>
                <td class="kpi-config-val">${utils.escapeHtml(String(val))}</td>
            </tr>
        `).join('');

    const deviceRows = Object.entries(selections)
        .map(([cat, ids]) => `
            <tr>
                <td class="kpi-config-key">${utils.escapeHtml(cat.replace(/_/g, ' '))}</td>
                <td class="kpi-config-val">${ids.length} device${ids.length !== 1 ? 's' : ''} (IDs: ${ids.join(', ')})</td>
            </tr>
        `).join('');

    return `
        <div class="kpi-config-grid">
            <div class="kpi-config-section">
                <h4>Settings</h4>
                <table class="kpi-config-table">
                    <tbody>${settingsRows || '<tr><td colspan="2" class="kpi-empty">No settings</td></tr>'}</tbody>
                </table>
            </div>
            <div class="kpi-config-section">
                <h4>Device Selections</h4>
                <table class="kpi-config-table">
                    <tbody>${deviceRows || '<tr><td colspan="2" class="kpi-empty">No devices</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    `;
}

/* =============================================================================
   Utility Helpers
   ============================================================================= */

/**
 * Convert a timestamp to "X ago" format.
 * @param {string} isoStr - ISO timestamp
 * @returns {string}
 */
function _timeAgo(isoStr) {
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

/**
 * Convert a creation date to a duration string.
 * @param {string} isoStr - ISO timestamp
 * @returns {string}
 */
function _timeSince(isoStr) {
    const diff = Date.now() - new Date(isoStr).getTime();
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));
    if (days === 0) return 'Today';
    if (days === 1) return '1 day';
    if (days < 30) return `${days} days`;
    const months = Math.floor(days / 30);
    return `${months} month${months > 1 ? 's' : ''}`;
}
