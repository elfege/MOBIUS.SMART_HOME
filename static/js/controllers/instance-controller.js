/**
 * Instance Controller
 *
 * Manages instance creation wizard and editing.
 * In edit mode, loads existing instance data and pre-populates all steps.
 */

import { api, utils } from '../main.js';

export class InstanceWizardController {
    /**
     * @param {number|null} instanceId - Instance ID for edit mode, null for create
     */
    constructor(instanceId = null) {
        this.instanceId = instanceId;
        this.isEditMode = !!instanceId;
        this.currentStep = 1;
        this.appType = null;
        this.appTypeSchema = null;
        this.selectedDevices = {};
        this.settings = {};
        this.existingInstance = null;
    }

    /**
     * Initialize the wizard
     */
    async init() {
        if (this.isEditMode) {
            await this.loadExistingInstance();
        } else {
            await this.loadAppTypes();
        }
    }

    /**
     * Load existing instance data for edit mode
     */
    async loadExistingInstance() {
        try {
            this.existingInstance = await api.get(`/instances/${this.instanceId}`);

            // Resolve app type name from ID
            const types = await api.get('/app-types');
            const matchedType = types.find(t => t.id === this.existingInstance.app_type_id);
            if (!matchedType) {
                utils.notify('Unknown app type for this instance', 'error');
                return;
            }

            this.appType = matchedType.type_name;

            // Load schema
            this.appTypeSchema = await api.get(`/app-types/${this.appType}/schema`);

            // Restore saved state
            this.selectedDevices = this.existingInstance.device_selections || {};
            this.settings = this.existingInstance.settings || {};

            // Render step 1 with type locked
            this.renderAppTypes(types);

            // Pre-select and show locked type card
            document.querySelectorAll('.app-type-card').forEach(card => {
                card.classList.toggle('selected', card.dataset.type === this.appType);
                if (card.dataset.type !== this.appType) {
                    card.style.opacity = '0.4';
                    card.style.pointerEvents = 'none';
                }
            });

        } catch (error) {
            console.error('Failed to load instance:', error);
            utils.notify(`Failed to load instance: ${error.message}`, 'error');
        }
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
            // Pre-fill label in edit mode
            if (this.isEditMode && this.existingInstance) {
                document.getElementById('instance-label').value = this.existingInstance.label || '';
            }
        }
    }

    /**
     * Render device pickers for step 2.
     * Each category is a collapsible card: collapsed shows selected tags,
     * expanded shows search + full device list.
     */
    async renderDevicePickers() {
        const container = document.getElementById('device-categories-container');
        const categories = this.appTypeSchema.device_categories || [];

        container.innerHTML = '<p class="loading-placeholder">Loading devices...</p>';

        // Cache loaded devices per category for tag rendering
        this._devicesByCategory = {};

        // Render each category
        let html = '';
        for (const category of categories) {
            const devices = await this.loadDevices(category.capability);
            this._devicesByCategory[category.key] = devices;
            html += this.renderDeviceCategory(category, devices);
        }

        container.innerHTML = html;

        // Pre-check devices from existing selections
        if (this.selectedDevices) {
            for (const [catKey, deviceIds] of Object.entries(this.selectedDevices)) {
                for (const deviceId of deviceIds) {
                    const cb = container.querySelector(
                        `input[name="${catKey}"][value="${deviceId}"]`
                    );
                    if (cb) cb.checked = true;
                }
            }
        }

        // Bind checkbox handlers
        container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', (e) => this.handleDeviceSelection(e));
        });

        // Bind search/filter handlers
        container.querySelectorAll('.device-search').forEach(input => {
            input.addEventListener('input', (e) => this.filterDevices(e));
        });

        // Update selected tags for all categories
        for (const category of categories) {
            this._updateCategoryTags(category.key);
        }
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
     * Render a device category as a collapsible card.
     * Collapsed: shows selected device tags.
     * Expanded: shows search + full scrollable list.
     * @param {object} category - Category definition
     * @param {Array} devices - Available devices
     * @returns {string} HTML string
     */
    renderDeviceCategory(category, devices) {
        const selectedCount = (this.selectedDevices[category.key] || []).length;

        const deviceItems = devices.map(device => `
            <label class="device-item"
                   data-device-id="${device.id}"
                   data-search-text="${utils.escapeHtml((device.label || device.name).toLowerCase())}">
                <input type="checkbox"
                       name="${category.key}"
                       value="${device.id}"
                       ${category.multiple ? '' : 'data-single="true"'}>
                <span>${utils.escapeHtml(device.label || device.name)}</span>
            </label>
        `).join('');

        return `
            <div class="device-category-card" data-key="${category.key}">
                <div class="device-category-header" onclick="wizard.toggleCategory('${category.key}')">
                    <div class="device-category-title">
                        <h4>${utils.escapeHtml(category.label)}${category.required ? ' <span class="required-star">*</span>' : ''}</h4>
                        <span class="device-category-desc">${utils.escapeHtml(category.description || '')}</span>
                    </div>
                    <div class="device-category-meta">
                        <span class="device-count" id="count-${category.key}">${selectedCount} selected</span>
                        <span class="expand-icon" id="icon-${category.key}">&#9660;</span>
                    </div>
                </div>
                <div class="device-selected-tags" id="tags-${category.key}"></div>
                <div class="device-category-body" id="body-${category.key}" style="display:none;">
                    <input type="text"
                           class="device-search"
                           placeholder="Filter (comma-separated keywords)"
                           data-category="${category.key}">
                    <div class="device-list">
                        ${deviceItems || '<p class="help-text">No devices available</p>'}
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Toggle a device category card open/closed.
     * @param {string} categoryKey - Category key
     */
    toggleCategory(categoryKey) {
        const body = document.getElementById(`body-${categoryKey}`);
        const tags = document.getElementById(`tags-${categoryKey}`);
        const icon = document.getElementById(`icon-${categoryKey}`);
        if (!body) return;

        const isOpen = body.style.display !== 'none';
        body.style.display = isOpen ? 'none' : 'block';
        if (tags) tags.style.display = isOpen ? '' : 'none';
        if (icon) icon.innerHTML = isOpen ? '&#9660;' : '&#9650;';
    }

    /**
     * Update the selected-devices tag strip for a category.
     * @param {string} categoryKey - Category key
     */
    _updateCategoryTags(categoryKey) {
        const tagsEl = document.getElementById(`tags-${categoryKey}`);
        const countEl = document.getElementById(`count-${categoryKey}`);
        const selected = this.selectedDevices[categoryKey] || [];
        const devices = this._devicesByCategory[categoryKey] || [];

        // Update count badge
        if (countEl) {
            countEl.textContent = `${selected.length} selected`;
        }

        // Render tag chips with red X remove button
        if (tagsEl) {
            if (selected.length === 0) {
                tagsEl.innerHTML = '<span class="no-selection-hint">Click to select devices</span>';
            } else {
                tagsEl.innerHTML = selected.map(id => {
                    const dev = devices.find(d => String(d.id) === String(id));
                    const name = dev ? (dev.label || dev.name) : id;
                    return `<span class="device-tag">${utils.escapeHtml(name)}<span class="device-tag-remove" onclick="event.stopPropagation(); wizard.removeDevice('${categoryKey}', '${id}')">&times;</span></span>`;
                }).join('');
            }
        }
    }

    /**
     * Normalize a string for search: lowercase, strip accents/diacritics
     * @param {string} str - Input string
     * @returns {string} Normalized string
     */
    normalize(str) {
        return str.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    }

    /**
     * Filter device list items by comma-separated keywords.
     * A device matches if ALL keywords appear somewhere in its name.
     * @param {Event} e - Input event from search field
     */
    filterDevices(e) {
        const input = e.target;
        const categoryDiv = input.closest('.device-category-card');
        const items = categoryDiv.querySelectorAll('.device-item');
        const raw = input.value;

        // Split on commas, normalize each keyword, drop empties
        const keywords = raw.split(',')
            .map(k => this.normalize(k.trim()))
            .filter(k => k.length > 0);

        items.forEach(item => {
            if (keywords.length === 0) {
                item.style.display = '';
                return;
            }
            const text = this.normalize(item.dataset.searchText || '');
            // Show if every keyword is found in the device name
            const match = keywords.every(kw => text.includes(kw));
            item.style.display = match ? '' : 'none';
        });
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

        // Refresh tag strip
        this._updateCategoryTags(category);
    }

    /**
     * Remove a device from a category (called from tag X button).
     * No confirmation — immediate removal.
     * @param {string} categoryKey - Category key
     * @param {string} deviceId - Device ID to remove
     */
    removeDevice(categoryKey, deviceId) {
        if (!this.selectedDevices[categoryKey]) return;

        // Remove from selection
        this.selectedDevices[categoryKey] = this.selectedDevices[categoryKey].filter(
            id => id !== deviceId
        );

        // Uncheck the corresponding checkbox if the card body is open
        const cb = document.querySelector(
            `input[name="${categoryKey}"][value="${deviceId}"]`
        );
        if (cb) cb.checked = false;

        // Refresh tags
        this._updateCategoryTags(categoryKey);
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
     * Settings grouping: maps schema property keys to logical groups.
     * Each group renders as a separate card in the settings form.
     * Properties not listed here go into an "Other" group.
     */
    static SETTINGS_GROUPS = [
        {
            id: 'timing',
            title: 'Timing',
            description: 'Motion timeout configuration',
            keys: ['noMotionTime', 'timeUnit']
        },
        {
            id: 'dimming',
            title: 'Dimming & Color',
            description: 'Brightness and color settings',
            keys: ['useDim', 'defaultDimLevel', 'useColor', 'colorPreset', 'customColorTemperature']
        },
        {
            id: 'illuminance',
            title: 'Illuminance',
            description: 'Light-level based activation',
            keys: ['useIlluminance', 'illuminanceThreshold']
        },
        {
            id: 'pause',
            title: 'Pause Control',
            description: 'Button and pause duration settings',
            keys: ['buttonEventType', 'pauseDuration', 'pauseDurationUnit', 'pauseSwitchAction']
        },
        {
            id: 'advanced',
            title: 'Advanced',
            description: 'Memoization and fail-safe options',
            keys: ['memoize', 'considerActiveWhenFail']
        }
    ];

    /**
     * Render settings form for step 3, grouped into cards.
     */
    renderSettingsForm() {
        const container = document.getElementById('settings-form-container');
        const schema = this.appTypeSchema.settings_schema || {};
        const properties = schema.properties || {};

        if (Object.keys(properties).length === 0) {
            container.innerHTML = '<p>No additional settings required.</p>';
            return;
        }

        // Track which keys have been placed in a group
        const placed = new Set();
        let html = '';

        // Render each defined group
        for (const group of InstanceWizardController.SETTINGS_GROUPS) {
            // Only include keys that exist in this schema
            const groupKeys = group.keys.filter(k => k in properties);
            if (groupKeys.length === 0) continue;

            groupKeys.forEach(k => placed.add(k));

            const fieldsHtml = groupKeys.map(k => this.renderSettingField(k, properties[k])).join('');

            html += `
                <div class="settings-group" data-group="${group.id}">
                    <div class="settings-group-header">
                        <h4>${group.title}</h4>
                        <span class="settings-group-desc">${group.description}</span>
                    </div>
                    <div class="settings-group-body">
                        ${fieldsHtml}
                    </div>
                </div>
            `;
        }

        // Render any remaining un-grouped settings
        const ungrouped = Object.keys(properties).filter(k => !placed.has(k));
        if (ungrouped.length > 0) {
            const fieldsHtml = ungrouped.map(k => this.renderSettingField(k, properties[k])).join('');
            html += `
                <div class="settings-group" data-group="other">
                    <div class="settings-group-header">
                        <h4>Other</h4>
                    </div>
                    <div class="settings-group-body">
                        ${fieldsHtml}
                    </div>
                </div>
            `;
        }

        container.innerHTML = html;

        // Initialize settings: use existing values in edit mode, defaults otherwise
        for (const [key, prop] of Object.entries(properties)) {
            if (this.settings[key] === undefined && prop.default !== undefined) {
                this.settings[key] = prop.default;
            }
        }

        // Apply current values to form elements
        for (const [key, value] of Object.entries(this.settings)) {
            const el = container.querySelector(`[name="${key}"]`);
            if (!el) continue;
            if (el.type === 'checkbox') {
                el.checked = !!value;
            } else if (el.tagName === 'SELECT') {
                el.value = value;
            } else {
                el.value = value;
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
     * Create a new instance (create mode)
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

    /**
     * Save changes to existing instance (edit mode)
     */
    async save() {
        const label = document.getElementById('instance-label').value.trim();

        if (!label) {
            utils.notify('Please enter a name for your automation', 'error');
            return;
        }

        const payload = {
            label: label,
            device_selections: this.selectedDevices,
            settings: this.settings
        };

        try {
            await api.put(`/instances/${this.instanceId}`, payload);
            utils.notify('Automation updated successfully!');
            window.location.href = '/';
        } catch (error) {
            utils.notify(`Failed to update automation: ${error.message}`, 'error');
        }
    }
}
