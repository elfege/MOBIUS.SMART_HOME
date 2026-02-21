/**
 * Dashboard Controller
 *
 * Manages the main dashboard view showing all automation instances.
 */

import { api, utils } from '../main.js';

export class DashboardController {
    /**
     * @param {string} containerId - ID of the container element
     */
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.instances = [];
        // Restore debug panel state from localStorage
        const saved = JSON.parse(localStorage.getItem('debugPanels') || '{}');
        this.openDebugPanels = new Set(saved.open || []);
        this.debugSizes = saved.sizes || {};
    }

    /**
     * Initialize the dashboard
     */
    async init() {
        await this.loadInstances();
        this.startAutoRefresh();
    }

    /**
     * Load all instances from API
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
    }

    /**
     * Render a single instance card
     * @param {object} inst - Instance data
     * @returns {string} HTML string
     */
    renderCard(inst) {
        const isPaused = inst.is_paused;
        const deviceCount = this.countDevices(inst.device_selections);

        return `
            <div class="instance-card ${isPaused ? 'paused' : ''}" data-id="${inst.id}">
                <div class="card-header">
                    <h3>${utils.escapeHtml(inst.label)}</h3>
                    <span class="app-type-badge">${this.getAppTypeName(inst.app_type_id)}</span>
                </div>
                <div class="card-body">
                    <span class="status-indicator ${isPaused ? 'paused' : 'active'}">
                        ${isPaused ? 'PAUSED' : 'ACTIVE'}
                    </span>
                    <div class="device-summary">
                        ${deviceCount} device${deviceCount !== 1 ? 's' : ''} configured
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
                    <button class="btn btn-danger btn-small" onclick="dashboard.deleteInstance(${inst.id})">
                        Delete
                    </button>
                </div>
                <div class="debug-panel" id="debug-${inst.id}" style="display:none;">
                    <div class="debug-toolbar">
                        <span class="debug-title">Event Log</span>
                        <button class="btn btn-secondary btn-small" onclick="dashboard.refreshDebug(${inst.id})">Refresh</button>
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
        // TODO: Fetch and cache app types
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
        // Make dashboard accessible globally for onclick handlers
        window.dashboard = this;
    }

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
                    duration_minutes: 60  // Default 1 hour
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

    /**
     * Toggle debug panel for an instance
     * @param {number} instanceId - Instance ID
     */
    /**
     * Persist debug panel state to localStorage
     */
    saveDebugState() {
        localStorage.setItem('debugPanels', JSON.stringify({
            open: [...this.openDebugPanels],
            sizes: this.debugSizes
        }));
    }

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

            // Scroll to bottom
            output.scrollTop = output.scrollHeight;
        } catch (error) {
            output.innerHTML = `<span class="debug-error">Error: ${error.message}</span>`;
        }
    }

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

    /**
     * Start auto-refresh
     */
    startAutoRefresh() {
        // Refresh every 30 seconds
        setInterval(() => this.loadInstances(), 30000);
    }
}
