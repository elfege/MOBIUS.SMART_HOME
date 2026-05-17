/**
 * Instance Controller
 *
 * Manages instance creation wizard and editing.
 * In edit mode, loads existing instance data and pre-populates all steps.
 */

import { api, utils } from '../main.js';
import { openDeviceTileModal } from '../components/device-tile-modal.js';

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
        this._applyHeaderForMode();
        if (this.isEditMode) {
            await this.loadExistingInstance();
            // In edit mode, skip the "Choose Type" step entirely — the type is
            // immutable for an existing instance. Hide the pill (via CSS class)
            // and jump straight to step 2.
            document.querySelector('.wizard-steps')?.classList.add('edit-mode');
            this.goToStep(2);
        } else {
            await this.loadAppTypes();
        }
        this._refreshStepPills();
    }

    /**
     * Adjust header text + Save button label depending on add vs edit mode.
     * Lets the same template serve both flows without two URLs.
     */
    _applyHeaderForMode() {
        const title = document.getElementById('wizard-title');
        const step4Title = document.getElementById('step-4-title');
        const saveBtn = document.getElementById('wizard-save-btn');
        if (this.isEditMode) {
            if (title) title.textContent = 'Edit Automation';
            if (step4Title) step4Title.textContent = 'Save Changes';
            if (saveBtn) saveBtn.textContent = 'Save';
        } else {
            if (title) title.textContent = 'New Automation';
            if (step4Title) step4Title.textContent = 'Name Your Automation';
            if (saveBtn) saveBtn.textContent = 'Create Automation';
        }
    }

    /**
     * Mark step pills as clickable. ALL steps except the current one are
     * navigable — forward jumps are gated by validation inside goToStep()
     * (which walks nextStep one step at a time and bails on validation
     * failure), backward jumps are always allowed. The current step gets
     * the .active class for visual emphasis but is not clickable (no point
     * in clicking the step you're already on).
     */
    _refreshStepPills() {
        document.querySelectorAll('.wizard-steps .step').forEach((el) => {
            const step = parseInt(el.dataset.step, 10);
            const isCurrent = step === this.currentStep;
            el.classList.toggle('clickable', !isCurrent);
            el.setAttribute('aria-disabled', isCurrent ? 'true' : 'false');
        });
    }

    /**
     * Load existing instance data for edit mode
     */
    async loadExistingInstance() {
        try {
            this.existingInstance = await api.get(`/instances/${this.instanceId}`);

            // Kill the running instance immediately on edit entry.
            // It will be restarted on save (with new data) or on
            // cancel/navigation away (with current DB data).
            await api.post(`/instances/${this.instanceId}/stop`);
            this._instanceStopped = true;

            // Guard: if the user leaves the page without saving, restart
            // the instance from its current DB state.
            this._beforeUnloadHandler = () => {
                if (this._instanceStopped) {
                    // Fire-and-forget with keepalive so the request survives
                    // page unload (sendBeacon can't set Content-Type: json)
                    fetch(`/api/instances/${this.instanceId}/start`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        keepalive: true
                    });
                }
            };
            window.addEventListener('beforeunload', this._beforeUnloadHandler);

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
     * Restart the instance from DB and clean up the beforeunload guard.
     * Called when the user cancels editing without saving.
     */
    async _restartInstance() {
        if (!this._instanceStopped) return;
        try {
            await api.post(`/instances/${this.instanceId}/start`);
        } catch (e) {
            console.error('Failed to restart instance:', e);
        }
        this._instanceStopped = false;
        if (this._beforeUnloadHandler) {
            window.removeEventListener('beforeunload', this._beforeUnloadHandler);
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
        this._refreshStepPills();
    }

    /**
     * Go to previous step. Edit mode floor is step 2 (no type-pick).
     */
    prevStep() {
        const floor = this.isEditMode ? 2 : 1;
        if (this.currentStep > floor) {
            this.currentStep--;
            this.showStep(this.currentStep);
            this._refreshStepPills();
        }
    }

    /**
     * Jump directly to a step (via pill click). Forward jumps validate the
     * intervening steps; backward jumps are always allowed.
     */
    goToStep(target) {
        if (target < 1 || target > 4) return;
        // Disallow jumping to step 1 in edit mode (type is immutable).
        if (this.isEditMode && target === 1) return;
        if (target > this.currentStep) {
            // Forward jump: walk steps to validate each intermediate gate.
            while (this.currentStep < target) {
                const before = this.currentStep;
                this.nextStep();
                if (this.currentStep === before) return; // validation blocked
            }
        } else if (target < this.currentStep) {
            this.currentStep = target;
            this.showStep(this.currentStep);
            this._refreshStepPills();
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

        // Cross-category fallback: every device in the canonical `devices`
        // table by id. Used when a saved selection references a device the
        // current category's capability filter wouldn't return (so the
        // chip can still render the label, never a bare numeric id).
        this._allDevicesById = {};

        // Perf 2026-05-17: ONE bulk call to /api/devices/by-categories instead
        // of N sequential per-category calls. The endpoint reads the canonical
        // `devices` table (no Hubitat HTTP), so this is ~10ms total vs
        // ~700ms × N categories previously.
        const capabilities = categories.map(c => c.capability).filter(Boolean);
        let grouped = {};
        try {
            if (capabilities.length) {
                grouped = await api.get(
                    `/devices/by-categories?categories=${encodeURIComponent(capabilities.join(','))}`
                );
            }
        } catch (err) {
            console.error('Bulk devices load failed, falling back to per-category', err);
            grouped = {};
        }

        // Populate fallback dict from the bulk response itself (every device
        // we just loaded is fair game for chip labels).
        for (const cap of Object.keys(grouped)) {
            for (const d of (grouped[cap] || [])) {
                this._allDevicesById[String(d.id)] = d;
            }
        }

        // Render each category from the bulk response. Fallback to per-category
        // call if the bulk call somehow missed (defensive).
        let html = '';
        for (const category of categories) {
            let devices = grouped[category.capability] || [];
            if (!devices.length) {
                devices = await this.loadDevices(category.capability);
            }
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

        // Delegated click handler: clicking a device tag name opens tile modal
        $(container).off('click.dtm').on('click.dtm', '.device-tag-name', function (e) {
            e.stopPropagation();
            const deviceId = $(this).data('device-id');
            const deviceName = $(this).data('device-name');
            if (deviceId) {
                openDeviceTileModal(String(deviceId), deviceName);
            }
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
                    // First try the current category's device list (fast path).
                    let dev = devices.find(d => String(d.id) === String(id));
                    // Fallback: any device by canonical id, regardless of
                    // capability — handles saved selections that reference
                    // a device the category's capability filter wouldn't
                    // return (or that's been miscategorised by a prior bug).
                    if (!dev && this._allDevicesById) {
                        dev = this._allDevicesById[String(id)];
                    }
                    const label = dev ? (dev.label || dev.name) : null;
                    // Show "Label · #id" so the canonical id is always
                    // visible. If no label, render "(unknown #id)" so it's
                    // immediately obvious the row is broken.
                    const display = label
                        ? `${label} · #${id}`
                        : `(unknown #${id})`;
                    const cls = label ? 'device-tag' : 'device-tag device-tag-orphan';

                    // The chip's label part is a link to the device's edit
                    // page on its OWN hub — multi-hub aware. Hub IP comes
                    // from the canonical devices row (joined with hub_config
                    // server-side), hubitat_id is the per-hub Hubitat id.
                    // Falls back to a plain span when we don't know the hub.
                    let nameNode;
                    if (dev && dev.hub_ip && dev.hubitat_id != null) {
                        const href = `http://${dev.hub_ip}/device/edit/${dev.hubitat_id}`;
                        nameNode =
                            `<a class="device-tag-name" `
                            + `href="${href}" target="_blank" rel="noopener" `
                            + `data-device-id="${id}" data-device-name="${utils.escapeHtml(label || '')}" `
                            + `title="Open ${utils.escapeHtml(label || '')} on hub ${dev.hub_ip}" `
                            + `onclick="event.stopPropagation()">`
                            + `${utils.escapeHtml(display)}</a>`;
                    } else {
                        nameNode =
                            `<span class="device-tag-name" `
                            + `data-device-id="${id}" `
                            + `data-device-name="${utils.escapeHtml(label || '')}">`
                            + `${utils.escapeHtml(display)}</span>`;
                    }
                    return `<span class="${cls}">${nameNode}<span class="device-tag-remove" onclick="event.stopPropagation(); wizard.removeDevice('${categoryKey}', '${id}')">&times;</span></span>`;
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

            // Mutual exclusion: keep_off ↔ keep_on
            // A device cannot be in both lists simultaneously
            const conflictMap = {
                'keep_off_switches': 'keep_on_switches',
                'keep_on_switches': 'keep_off_switches'
            };
            const conflictCategory = conflictMap[category];
            if (conflictCategory && this.selectedDevices[conflictCategory]) {
                const wasIn = this.selectedDevices[conflictCategory].includes(deviceId);
                if (wasIn) {
                    this.selectedDevices[conflictCategory] =
                        this.selectedDevices[conflictCategory].filter(id => id !== deviceId);
                    // Uncheck the checkbox in the other category
                    const otherCb = document.querySelector(
                        `input[name="${conflictCategory}"][value="${deviceId}"]`
                    );
                    if (otherCb) otherCb.checked = false;
                    this._updateCategoryTags(conflictCategory);
                }
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
            keys: ['noMotionTime', 'timeUnit', 'timeWithMode', 'modeTimeouts']
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
            id: 'keep',
            title: 'Always Off / Always On',
            description: 'Mode restrictions for keep-off and keep-on enforcement',
            keys: ['keepOffModes', 'keepOnModes']
        },
        {
            id: 'restrictions',
            title: 'Restrictions',
            description: 'Mode-based exclusion (app pauses in selected modes)',
            keys: ['exclusionModes']
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

        // Populate mode checkboxes for array-type settings
        this._populateModeCheckboxes(container);

        // Bind timeWithMode toggle to show/hide per-mode timeouts
        this._bindModeTimeoutToggle(container);
    }

    /**
     * Fetch Hubitat modes and populate dropdown-checkbox selectors.
     * @param {HTMLElement} container - Settings form container
     */
    async _populateModeCheckboxes(container) {
        const dropdowns = container.querySelectorAll('.mode-dropdown');
        if (dropdowns.length === 0) return;

        let modes = [];
        try {
            const resp = await $.get('/api/modes');
            modes = resp || [];
        } catch (e) {
            console.error('Failed to fetch modes:', e);
            dropdowns.forEach(dd => {
                dd.querySelector('.mode-dropdown-menu').innerHTML =
                    '<span class="error-text">Failed to load modes</span>';
            });
            return;
        }

        dropdowns.forEach(dd => {
            const key = dd.dataset.modeKey;
            const selected = this.settings[key] || [];
            const menu = dd.querySelector('.mode-dropdown-menu');
            const toggle = dd.querySelector('.mode-dropdown-toggle');
            const labelSpan = dd.querySelector('.mode-dropdown-label');

            const checkboxes = modes.map(mode => {
                const name = mode.name || mode;
                const checked = selected.includes(name) ? 'checked' : '';
                const badge = mode.active ? ' <span class="mode-active-badge">(current)</span>' : '';
                return `
                    <label class="mode-dropdown-item">
                        <input type="checkbox" value="${utils.escapeHtml(name)}" ${checked}>
                        ${utils.escapeHtml(name)}${badge}
                    </label>
                `;
            }).join('');

            menu.innerHTML = checkboxes;

            // Update label text showing selection count
            const emptyLabel = dd.dataset.emptyLabel || 'All modes';
            const updateLabel = () => {
                const checked = Array.from(menu.querySelectorAll('input:checked'));
                if (checked.length === 0) {
                    labelSpan.textContent = emptyLabel;
                } else {
                    labelSpan.textContent = checked.map(c => c.value).join(', ');
                }
                this.settings[key] = checked.map(c => c.value);
            };
            updateLabel();

            // Toggle dropdown open/close
            toggle.addEventListener('click', (e) => {
                e.preventDefault();
                const isOpen = menu.style.display !== 'none';
                // Close all other dropdowns first
                container.querySelectorAll('.mode-dropdown-menu').forEach(m => {
                    m.style.display = 'none';
                });
                menu.style.display = isOpen ? 'none' : 'block';
            });

            // Checkbox change updates settings
            menu.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                cb.addEventListener('change', updateLabel);
            });
        });

        // Close dropdowns on click outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.mode-dropdown')) {
                container.querySelectorAll('.mode-dropdown-menu').forEach(m => {
                    m.style.display = 'none';
                });
            }
        });
    }

    /**
     * Bind the timeWithMode checkbox to show/hide per-mode timeout inputs.
     * @param {HTMLElement} container - Settings form container
     */
    _bindModeTimeoutToggle(container) {
        const twmCheckbox = container.querySelector('[name="timeWithMode"]');
        const mtContainer = container.querySelector('#mode-timeouts-container');
        if (!twmCheckbox || !mtContainer) return;

        // Show/hide based on current value
        const show = !!this.settings.timeWithMode;
        mtContainer.style.display = show ? '' : 'none';
        if (show) {
            this._populateModeTimeouts(mtContainer);
        }

        // Toggle handler
        twmCheckbox.addEventListener('change', () => {
            const enabled = twmCheckbox.checked;
            mtContainer.style.display = enabled ? '' : 'none';
            if (enabled) {
                this._populateModeTimeouts(mtContainer);
            }
        });
    }

    /**
     * Fetch modes and render per-mode timeout inputs.
     * Shows ALL modes with a number input next to each.
     * Empty input = use default timeout.
     * @param {HTMLElement} container - The #mode-timeouts-container element
     */
    async _populateModeTimeouts(container) {
        const listEl = container.querySelector('#mode-timeouts-list');
        if (!listEl) return;

        let modes = [];
        try {
            modes = await $.get('/api/modes');
        } catch (e) {
            console.error('Failed to fetch modes for timeouts:', e);
            listEl.innerHTML = '<span class="error-text">Failed to load modes</span>';
            return;
        }

        const modeTimeouts = this.settings.modeTimeouts || {};
        const defaultTimeout = this.settings.noMotionTime || 5;

        listEl.innerHTML = modes.map(mode => {
            const name = mode.name || mode;
            const value = modeTimeouts[name] !== undefined ? modeTimeouts[name] : '';
            const badge = mode.active
                ? ' <span class="mode-active-badge">(current)</span>'
                : '';
            return `
                <div class="mode-timeout-row">
                    <span class="mode-timeout-label">
                        ${utils.escapeHtml(name)}${badge}
                    </span>
                    <input type="number"
                           class="mode-timeout-input"
                           data-mode="${utils.escapeHtml(name)}"
                           value="${value}"
                           placeholder="${defaultTimeout}"
                           min="1">
                </div>
            `;
        }).join('');

        // Bind change handlers
        listEl.querySelectorAll('.mode-timeout-input').forEach(input => {
            input.addEventListener('change', () => {
                if (!this.settings.modeTimeouts) {
                    this.settings.modeTimeouts = {};
                }
                const modeName = input.dataset.mode;
                const val = parseInt(input.value, 10);
                if (isNaN(val) || input.value.trim() === '') {
                    delete this.settings.modeTimeouts[modeName];
                } else {
                    this.settings.modeTimeouts[modeName] = val;
                }
            });
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
        } else if (prop.type === 'object' && key === 'modeTimeouts') {
            // Per-mode timeout widget (shown/hidden by timeWithMode toggle)
            input = `
                <div id="mode-timeouts-container" style="display:none;">
                    <label>${utils.escapeHtml(title)}</label>
                    <p class="help-text">${utils.escapeHtml(description)}</p>
                    <div id="mode-timeouts-list">
                        <span class="help-text">Loading modes...</span>
                    </div>
                </div>
            `;
            // Skip the outer form-group description since we embed it
            return `<div class="form-group">${input}</div>`;
        } else if (prop.type === 'array' && prop.items && prop.items.type === 'string') {
            // Dropdown with checkboxes for multi-select
            // For exclusionModes, empty = "None" (no exclusion).
            // For keepOff/keepOn modes, empty = "All modes".
            const emptyLabel = key === 'exclusionModes' ? 'None' : 'All modes';
            input = `
                <label>${utils.escapeHtml(title)}</label>
                <div class="mode-dropdown" data-mode-key="${key}" data-empty-label="${emptyLabel}">
                    <button type="button" class="mode-dropdown-toggle">
                        <span class="mode-dropdown-label">${emptyLabel}</span>
                        <span class="mode-dropdown-arrow">&#9662;</span>
                    </button>
                    <div class="mode-dropdown-menu" style="display:none;">
                        <span class="help-text">Loading modes...</span>
                    </div>
                </div>
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
            // Backend update_instance kills + restarts the instance.
            // Clear the beforeunload guard so it doesn't also restart.
            this._instanceStopped = false;
            if (this._beforeUnloadHandler) {
                window.removeEventListener('beforeunload', this._beforeUnloadHandler);
            }

            await api.put(`/instances/${this.instanceId}`, payload);
            utils.notify('Automation updated successfully!');
            window.location.href = '/';
        } catch (error) {
            // Save failed — re-arm the guard so the instance restarts on page leave
            this._instanceStopped = true;
            if (this._beforeUnloadHandler) {
                window.addEventListener('beforeunload', this._beforeUnloadHandler);
            }
            utils.notify(`Failed to update automation: ${error.message}`, 'error');
        }
    }
}
