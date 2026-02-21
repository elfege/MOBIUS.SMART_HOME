/**
 * Instance Controller
 *
 * Manages instance creation wizard and editing.
 */

import { api, utils } from '../main.js';

export class InstanceWizardController {
    constructor() {
        this.currentStep = 1;
        this.appType = null;
        this.appTypeSchema = null;
        this.selectedDevices = {};
        this.settings = {};
    }

    /**
     * Initialize the wizard
     */
    async init() {
        await this.loadAppTypes();
    }

    /**
     * Load available app types
     */
    async loadAppTypes() {
        try {
            const types = await api.get('/app-types');
            this.renderAppTypes(types);
        } catch (error) {
            console.error('Failed to load app types:', error);
            document.getElementById('app-types-container').innerHTML =
                '<p class="error-message">Failed to load app types</p>';
        }
    }

    /**
     * Render app type selection cards
     * @param {Array} types - App types from API
     */
    renderAppTypes(types) {
        const container = document.getElementById('app-types-container');

        if (types.length === 0) {
            container.innerHTML = '<p>No automation types available</p>';
            return;
        }

        container.innerHTML = types.map(type => `
            <div class="app-type-card" data-type="${type.type_name}" onclick="wizard.selectAppType('${type.type_name}')">
                <h4>${utils.escapeHtml(type.display_name)}</h4>
                <p>${utils.escapeHtml(type.description || '')}</p>
            </div>
        `).join('');
    }

    /**
     * Select an app type
     * @param {string} typeName - App type identifier
     */
    async selectAppType(typeName) {
        // Update UI selection
        document.querySelectorAll('.app-type-card').forEach(card => {
            card.classList.toggle('selected', card.dataset.type === typeName);
        });

        this.appType = typeName;

        // Load schema
        try {
            this.appTypeSchema = await api.get(`/app-types/${typeName}/schema`);
            this.nextStep();
        } catch (error) {
            utils.notify(`Failed to load app configuration: ${error.message}`, 'error');
        }
    }

    /**
     * Go to next step
     */
    nextStep() {
        if (this.currentStep === 1 && !this.appType) {
            utils.notify('Please select an automation type', 'error');
            return;
        }

        if (this.currentStep === 2 && !this.validateDevices()) {
            return;
        }

        this.currentStep++;
        this.showStep(this.currentStep);
    }

    /**
     * Go to previous step
     */
    prevStep() {
        if (this.currentStep > 1) {
            this.currentStep--;
            this.showStep(this.currentStep);
        }
    }

    /**
     * Show a wizard step
     * @param {number} step - Step number (1-4)
     */
    showStep(step) {
        // Update step indicators
        document.querySelectorAll('.wizard-steps .step').forEach((el, i) => {
            el.classList.remove('active', 'completed');
            if (i + 1 < step) el.classList.add('completed');
            if (i + 1 === step) el.classList.add('active');
        });

        // Show/hide step content
        for (let i = 1; i <= 4; i++) {
            const stepEl = document.getElementById(`step-${i}`);
            if (stepEl) {
                stepEl.style.display = i === step ? 'block' : 'none';
            }
        }

        // Initialize step content
        if (step === 2) {
            this.renderDevicePickers();
        } else if (step === 3) {
            this.renderSettingsForm();
        } else if (step === 4) {
            this.renderSummary();
        }
    }

    /**
     * Render device pickers for step 2
     */
    async renderDevicePickers() {
        const container = document.getElementById('device-categories-container');
        const categories = this.appTypeSchema.device_categories || [];

        container.innerHTML = '<p class="loading-placeholder">Loading devices...</p>';

        // Render each category
        let html = '';
        for (const category of categories) {
            const devices = await this.loadDevices(category.capability);
            html += this.renderDeviceCategory(category, devices);
        }

        container.innerHTML = html;

        // Bind checkbox handlers
        container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', (e) => this.handleDeviceSelection(e));
        });
    }

    /**
     * Load devices by capability
     * @param {string} capability - Device capability
     * @returns {Array} Devices
     */
    async loadDevices(capability) {
        try {
            return await api.get(`/devices?capability=${capability}`);
        } catch (error) {
            console.error(`Failed to load ${capability} devices:`, error);
            return [];
        }
    }

    /**
     * Render a device category picker
     * @param {object} category - Category definition
     * @param {Array} devices - Available devices
     * @returns {string} HTML string
     */
    renderDeviceCategory(category, devices) {
        const deviceItems = devices.map(device => `
            <label class="device-item">
                <input type="checkbox"
                       name="${category.key}"
                       value="${device.id}"
                       ${category.multiple ? '' : 'data-single="true"'}>
                <span>${utils.escapeHtml(device.label || device.name)}</span>
            </label>
        `).join('');

        return `
            <div class="device-category" data-key="${category.key}">
                <h4>${utils.escapeHtml(category.label)}${category.required ? ' *' : ''}</h4>
                <p class="help-text">${utils.escapeHtml(category.description || '')}</p>
                <div class="device-list">
                    ${deviceItems || '<p>No devices available</p>'}
                </div>
            </div>
        `;
    }

    /**
     * Handle device selection change
     * @param {Event} e - Change event
     */
    handleDeviceSelection(e) {
        const checkbox = e.target;
        const category = checkbox.name;
        const deviceId = checkbox.value;
        const isSingle = checkbox.dataset.single === 'true';

        // Initialize category if needed
        if (!this.selectedDevices[category]) {
            this.selectedDevices[category] = [];
        }

        if (checkbox.checked) {
            // For single-select, uncheck others
            if (isSingle) {
                document.querySelectorAll(`input[name="${category}"]`).forEach(cb => {
                    if (cb !== checkbox) cb.checked = false;
                });
                this.selectedDevices[category] = [deviceId];
            } else {
                this.selectedDevices[category].push(deviceId);
            }
        } else {
            this.selectedDevices[category] = this.selectedDevices[category].filter(
                id => id !== deviceId
            );
        }
    }

    /**
     * Validate device selections
     * @returns {boolean} Whether validation passed
     */
    validateDevices() {
        const categories = this.appTypeSchema.device_categories || [];

        for (const category of categories) {
            if (category.required) {
                const selected = this.selectedDevices[category.key] || [];
                if (selected.length === 0) {
                    utils.notify(`Please select at least one ${category.label}`, 'error');
                    return false;
                }
            }
        }

        return true;
    }

    /**
     * Render settings form for step 3
     */
    renderSettingsForm() {
        const container = document.getElementById('settings-form-container');
        const schema = this.appTypeSchema.settings_schema || {};
        const properties = schema.properties || {};

        let html = '';
        for (const [key, prop] of Object.entries(properties)) {
            html += this.renderSettingField(key, prop);
        }

        container.innerHTML = html || '<p>No additional settings required.</p>';

        // Initialize with defaults
        for (const [key, prop] of Object.entries(properties)) {
            if (prop.default !== undefined) {
                this.settings[key] = prop.default;
            }
        }

        // Bind change handlers
        container.querySelectorAll('input, select').forEach(el => {
            el.addEventListener('change', (e) => this.handleSettingChange(e));
        });
    }

    /**
     * Render a settings field
     * @param {string} key - Setting key
     * @param {object} prop - Schema property
     * @returns {string} HTML string
     */
    renderSettingField(key, prop) {
        const title = prop.title || key;
        const description = prop.description || '';
        const defaultVal = prop.default;

        let input = '';

        if (prop.type === 'boolean') {
            input = `
                <div class="checkbox-group">
                    <input type="checkbox" id="${key}" name="${key}" ${defaultVal ? 'checked' : ''}>
                    <label for="${key}">${utils.escapeHtml(title)}</label>
                </div>
            `;
        } else if (prop.enum) {
            const options = prop.enum.map(opt =>
                `<option value="${opt}" ${opt === defaultVal ? 'selected' : ''}>${opt}</option>`
            ).join('');
            input = `
                <label for="${key}">${utils.escapeHtml(title)}</label>
                <select id="${key}" name="${key}">${options}</select>
            `;
        } else if (prop.type === 'integer' || prop.type === 'number') {
            input = `
                <label for="${key}">${utils.escapeHtml(title)}</label>
                <input type="number" id="${key}" name="${key}"
                       value="${defaultVal || ''}"
                       ${prop.minimum !== undefined ? `min="${prop.minimum}"` : ''}
                       ${prop.maximum !== undefined ? `max="${prop.maximum}"` : ''}>
            `;
        } else {
            input = `
                <label for="${key}">${utils.escapeHtml(title)}</label>
                <input type="text" id="${key}" name="${key}" value="${defaultVal || ''}">
            `;
        }

        return `
            <div class="form-group">
                ${input}
                ${description ? `<p class="help-text">${utils.escapeHtml(description)}</p>` : ''}
            </div>
        `;
    }

    /**
     * Handle settings change
     * @param {Event} e - Change event
     */
    handleSettingChange(e) {
        const el = e.target;
        const key = el.name;

        if (el.type === 'checkbox') {
            this.settings[key] = el.checked;
        } else if (el.type === 'number') {
            this.settings[key] = parseInt(el.value, 10);
        } else {
            this.settings[key] = el.value;
        }
    }

    /**
     * Render summary for step 4
     */
    renderSummary() {
        const container = document.getElementById('wizard-summary');

        const deviceCount = Object.values(this.selectedDevices)
            .reduce((sum, arr) => sum + arr.length, 0);

        const settingsCount = Object.keys(this.settings)
            .filter(k => this.settings[k] !== undefined).length;

        container.innerHTML = `
            <div class="summary-item">
                <span class="label">Type</span>
                <span class="value">${utils.escapeHtml(this.appTypeSchema.display_name)}</span>
            </div>
            <div class="summary-item">
                <span class="label">Devices</span>
                <span class="value">${deviceCount} selected</span>
            </div>
            <div class="summary-item">
                <span class="label">Settings</span>
                <span class="value">${settingsCount} configured</span>
            </div>
        `;
    }

    /**
     * Create the instance
     */
    async create() {
        const label = document.getElementById('instance-label').value.trim();

        if (!label) {
            utils.notify('Please enter a name for your automation', 'error');
            return;
        }

        const payload = {
            app_type: this.appType,
            label: label,
            device_selections: this.selectedDevices,
            settings: this.settings
        };

        try {
            const result = await api.post('/instances', payload);
            utils.notify('Automation created successfully!');
            window.location.href = '/';
        } catch (error) {
            utils.notify(`Failed to create automation: ${error.message}`, 'error');
        }
    }
}
