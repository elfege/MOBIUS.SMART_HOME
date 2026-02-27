/**
 * Device Tile Modal
 *
 * Self-contained modal component that renders a HomeKit-style device tile
 * for quick control from the automation wizard. Supports toggle, dimmer slider,
 * and color picker. Uses optimistic state updates (no SSE).
 *
 * Visual design cherry-picked from 0_MOBIUS.TILES app.
 */

import { api, utils } from '../main.js';

/* =============================================================================
   Constants
   ============================================================================= */

/** Long-press threshold in ms to open dimmer panel */
const LONG_PRESS_MS = 500;

/** Debounce delay for setLevel commands during slider drag */
const SLIDER_DEBOUNCE_MS = 200;

/** Color presets (hue values 0-360, saturation 100%) */
const COLOR_PRESETS = [
    { name: 'Warm White', h: 30, s: 20, color: '#FFE4B5' },
    { name: 'Daylight',   h: 210, s: 10, color: '#F0F8FF' },
    { name: 'Red',        h: 0, s: 100, color: '#FF0000' },
    { name: 'Orange',     h: 30, s: 100, color: '#FF8C00' },
    { name: 'Yellow',     h: 60, s: 100, color: '#FFD700' },
    { name: 'Green',      h: 120, s: 100, color: '#00FF00' },
    { name: 'Cyan',       h: 180, s: 100, color: '#00FFFF' },
    { name: 'Blue',       h: 240, s: 100, color: '#0000FF' },
    { name: 'Purple',     h: 270, s: 100, color: '#8B00FF' },
    { name: 'Pink',       h: 330, s: 100, color: '#FF69B4' }
];

/* =============================================================================
   State
   ============================================================================= */

/** Currently open modal backdrop element (jQuery), or null */
let $activeBackdrop = null;

/** Debounce timer for setLevel commands */
let _sliderDebounceTimer = null;

/* =============================================================================
   Public API
   ============================================================================= */

/**
 * Opens a modal with a device tile for quick control.
 * Fetches device state, renders tile, attaches interaction handlers.
 *
 * @param {string} deviceId - Hubitat device ID
 * @param {string} deviceName - Display name for the tile
 */
export async function openDeviceTileModal(deviceId, deviceName) {
    // Prevent double-open
    if ($activeBackdrop) {
        closeModal();
    }

    // Build and show backdrop with loading state
    $activeBackdrop = _createBackdrop();
    const $modal = $activeBackdrop.find('.dtm-modal');
    $modal.html('<div class="dtm-loading">Loading device...</div>');

    $('body').append($activeBackdrop);

    // Trigger show animation on next frame
    requestAnimationFrame(() => {
        $activeBackdrop.addClass('show');
    });

    // Fetch device state
    try {
        const device = await api.get(`/devices/${deviceId}`);
        if (!device) {
            $modal.html('<div class="dtm-error">Device not found</div>');
            return;
        }

        // Determine device type from capabilities
        const deviceType = _determineDeviceType(device);

        // Extract current attribute values
        const attrs = _extractAttributes(device);

        // Render tile content
        const $tile = _renderTile(device, deviceType, attrs, deviceName);
        $modal.empty().append($tile);

        // Add hint text for dimmable devices
        if (deviceType === 'dimmer' || deviceType === 'color') {
            $modal.append('<div class="dtm-hint">Long-press for dimmer</div>');
        }

        // Attach interaction handlers
        _attachHandlers($tile, device, deviceType, attrs);

    } catch (error) {
        console.error('Failed to load device:', error);
        $modal.html(`<div class="dtm-error">Failed to load device: ${error.message}</div>`);
    }
}

/* =============================================================================
   Modal Structure
   ============================================================================= */

/**
 * Create the modal backdrop with container.
 * @returns {jQuery} Backdrop element
 */
function _createBackdrop() {
    const $backdrop = $('<div class="dtm-backdrop">')
        .on('click', function (e) {
            // Close only on backdrop click, not child clicks
            if (e.target === this) {
                closeModal();
            }
        });

    // Close button
    const $close = $('<button class="dtm-close">&times;</button>')
        .on('click', closeModal);

    const $modal = $('<div class="dtm-modal">')
        .append($close);

    $backdrop.append($modal);
    return $backdrop;
}

/**
 * Close the active modal.
 */
function closeModal() {
    if (!$activeBackdrop) return;

    $activeBackdrop.removeClass('show');

    // Remove after transition
    setTimeout(() => {
        if ($activeBackdrop) {
            $activeBackdrop.remove();
            $activeBackdrop = null;
        }
    }, 300);
}

/* =============================================================================
   Device Type Detection
   ============================================================================= */

/**
 * Determine device type from Hubitat capabilities.
 * @param {object} device - Hubitat device data
 * @returns {string} 'color' | 'dimmer' | 'switch'
 */
function _determineDeviceType(device) {
    const caps = (device.capabilities || []).map(c =>
        typeof c === 'string' ? c.toLowerCase() : (c.name || '').toLowerCase()
    );

    if (caps.includes('colorcontrol')) return 'color';
    if (caps.includes('switchlevel')) return 'dimmer';
    if (caps.includes('switch')) return 'switch';
    return 'switch';
}

/**
 * Extract attribute values from device data.
 * Hubitat returns attributes as an array of {name, currentValue, dataType}.
 * @param {object} device - Hubitat device data
 * @returns {object} Map of attribute name → current value
 */
function _extractAttributes(device) {
    const attrs = {};
    const attrList = device.attributes || [];

    for (const attr of attrList) {
        attrs[attr.name] = attr.currentValue;
    }

    return attrs;
}

/* =============================================================================
   Tile Rendering
   ============================================================================= */

/**
 * Render the device tile element.
 * @param {object} device - Hubitat device data
 * @param {string} deviceType - 'switch' | 'dimmer' | 'color'
 * @param {object} attrs - Extracted attributes
 * @param {string} displayName - Name to show on tile
 * @returns {jQuery} Tile element
 */
function _renderTile(device, deviceType, attrs, displayName) {
    const switchState = attrs.switch || 'off';
    const level = parseInt(attrs.level, 10) || 0;
    const isOn = switchState === 'on';

    const $tile = $('<div class="dtm-tile">')
        .data('device-id', device.id)
        .data('device-type', deviceType)
        .data('switch', switchState)
        .data('level', level);

    // Icon
    const iconChar = _getIconChar(device, isOn);
    const $icon = $('<div class="dtm-tile-icon">').html(iconChar);

    // State text
    const stateText = deviceType === 'switch'
        ? (isOn ? 'ON' : 'OFF')
        : `${isOn ? level : 0}%`;
    const $state = $('<div class="dtm-tile-state">').text(stateText);

    // Device name
    const $name = $('<div class="dtm-tile-name">').text(displayName);

    $tile.append($icon, $state, $name);

    // Apply on/off styling
    _applyTileState($tile, isOn);

    return $tile;
}

/**
 * Get icon character based on device name/type.
 * Uses Unicode symbols since Bootstrap Icons aren't available here.
 * @param {object} device - Device data
 * @param {boolean} isOn - Current state
 * @returns {string} Icon HTML/character
 */
function _getIconChar(device, isOn) {
    const name = (device.label || device.name || '').toLowerCase();

    // Fan
    if (name.includes('fan')) {
        return isOn ? '&#x1F4A8;' : '&#x1F4A8;'; // wind emoji
    }
    // Lock
    if (name.includes('lock')) {
        return isOn ? '&#x1F512;' : '&#x1F513;';
    }
    // Default: lightbulb for dimmers/lights, power for switches
    const caps = (device.capabilities || []).map(c =>
        typeof c === 'string' ? c.toLowerCase() : ''
    );

    if (caps.includes('switchlevel') || caps.includes('colorcontrol') ||
        name.includes('light') || name.includes('lamp') || name.includes('bulb')) {
        return isOn ? '&#x1F4A1;' : '&#x1F4A1;'; // lightbulb
    }

    // Generic switch
    return isOn ? '&#x26A1;' : '&#x2B58;'; // zap / circle
}

/**
 * Apply on/off CSS classes to tile.
 * @param {jQuery} $tile - Tile element
 * @param {boolean} isOn - State
 */
function _applyTileState($tile, isOn) {
    if (isOn) {
        $tile.addClass('dtm-tile-on').removeClass('dtm-tile-off');
    } else {
        $tile.addClass('dtm-tile-off').removeClass('dtm-tile-on');
    }
}

/* =============================================================================
   Interaction Handlers
   ============================================================================= */

/**
 * Attach tap (toggle) and long-press (dimmer) handlers to a tile.
 * @param {jQuery} $tile - Tile element
 * @param {object} device - Device data
 * @param {string} deviceType - Device type
 * @param {object} attrs - Current attributes
 */
function _attachHandlers($tile, device, deviceType, attrs) {
    let pressTimer = null;
    let didLongPress = false;
    let startX = 0;
    let startY = 0;

    const onPressStart = (e) => {
        didLongPress = false;
        const touch = e.touches ? e.touches[0] : e;
        startX = touch.clientX;
        startY = touch.clientY;

        // Only set up long-press for dimmable devices
        if (deviceType === 'dimmer' || deviceType === 'color') {
            pressTimer = setTimeout(() => {
                didLongPress = true;
                _openDimmerPanel(device, deviceType, attrs);
            }, LONG_PRESS_MS);
        }
    };

    const onPressEnd = (e) => {
        if (pressTimer) {
            clearTimeout(pressTimer);
            pressTimer = null;
        }

        if (didLongPress) {
            e.preventDefault();
            return;
        }

        // Check if finger moved too much (not a tap)
        const touch = e.changedTouches ? e.changedTouches[0] : e;
        const dx = Math.abs(touch.clientX - startX);
        const dy = Math.abs(touch.clientY - startY);
        if (dx > 10 || dy > 10) return;

        // Short tap: toggle
        _toggleDevice($tile, device, attrs);
    };

    const onPressCancel = () => {
        if (pressTimer) {
            clearTimeout(pressTimer);
            pressTimer = null;
        }
    };

    // Mouse events
    $tile.on('mousedown', onPressStart);
    $tile.on('mouseup', onPressEnd);
    $tile.on('mouseleave', onPressCancel);

    // Touch events
    $tile.on('touchstart', onPressStart);
    $tile.on('touchend', onPressEnd);
    $tile.on('touchcancel', onPressCancel);

    // Prevent context menu on long-press
    $tile.on('contextmenu', (e) => e.preventDefault());
}

/**
 * Toggle device on/off with optimistic update.
 * @param {jQuery} $tile - Tile element
 * @param {object} device - Device data
 * @param {object} attrs - Current attributes
 */
async function _toggleDevice($tile, device, attrs) {
    const currentState = $tile.data('switch');
    const newState = currentState === 'on' ? 'off' : 'on';
    const deviceType = $tile.data('device-type');

    // Optimistic UI update
    $tile.data('switch', newState);
    const isOn = newState === 'on';
    _applyTileState($tile, isOn);

    // Update state text
    if (deviceType === 'switch') {
        $tile.find('.dtm-tile-state').text(isOn ? 'ON' : 'OFF');
    } else {
        const level = $tile.data('level') || 0;
        $tile.find('.dtm-tile-state').text(`${isOn ? level : 0}%`);
    }

    // Update icon
    const iconChar = _getIconChar(device, isOn);
    $tile.find('.dtm-tile-icon').html(iconChar);

    // Send command
    try {
        await api.post(`/devices/${device.id}/command`, {
            command: newState === 'on' ? 'on' : 'off'
        });
    } catch (error) {
        console.error('Toggle failed:', error);

        // Revert on failure
        $tile.data('switch', currentState);
        const wasOn = currentState === 'on';
        _applyTileState($tile, wasOn);
        if (deviceType === 'switch') {
            $tile.find('.dtm-tile-state').text(wasOn ? 'ON' : 'OFF');
        } else {
            const level = $tile.data('level') || 0;
            $tile.find('.dtm-tile-state').text(`${wasOn ? level : 0}%`);
        }
        $tile.find('.dtm-tile-icon').html(_getIconChar(device, wasOn));

        utils.notify(`Failed to toggle device: ${error.message}`, 'error');
    }
}

/* =============================================================================
   Dimmer Slider Panel
   ============================================================================= */

/**
 * Open the dimmer slider panel inside the modal.
 * Replaces the tile content with a vertical slider.
 * @param {object} device - Device data
 * @param {string} deviceType - Device type
 * @param {object} attrs - Current attributes
 */
function _openDimmerPanel(device, deviceType, attrs) {
    if (!$activeBackdrop) return;

    const $modal = $activeBackdrop.find('.dtm-modal');
    const currentLevel = parseInt(attrs.level, 10) || 0;

    // Build dimmer panel
    const $panel = $('<div class="dtm-dimmer-panel">');

    // Header
    $panel.append(
        $('<div class="dtm-dimmer-header">').text(device.label || device.name)
    );

    // Slider track
    const $track = $('<div class="dtm-slider-track">');
    const $fill = $('<div class="dtm-slider-fill">').css('height', `${currentLevel}%`);
    const $pct = $('<div class="dtm-slider-pct">').text(`${currentLevel}`);
    $track.append($fill, $pct);

    // Buttons row
    const $controls = $('<div class="dtm-dimmer-controls">');
    const $minus = $('<button class="dtm-slider-btn">').html('&minus;');
    const $plus = $('<button class="dtm-slider-btn">').html('&plus;');

    // Color button (only for ColorControl devices)
    if (deviceType === 'color') {
        const $colorBtn = $('<button class="dtm-color-btn">')
            .attr('title', 'Color picker');
        $controls.append($minus, $colorBtn, $plus);

        // Color button click opens/toggles color panel
        $colorBtn.on('click', (e) => {
            e.stopPropagation();
            _toggleColorPanel($panel, device, attrs);
        });
    } else {
        $controls.append($minus, $plus);
    }

    // Slider container
    const $sliderContainer = $('<div class="dtm-slider-container">');
    $sliderContainer.append($plus, $track, $minus);

    $panel.append($sliderContainer, $controls);

    // Back button to return to tile view
    const $back = $('<button class="dtm-slider-btn" style="font-size:16px; width:auto; padding:8px 16px; border-radius:12px;">')
        .text('Back')
        .on('click', () => {
            // Re-render tile with updated state
            const newAttrs = { ...attrs, level: String(_currentSliderLevel) };
            if (_currentSliderLevel > 0) newAttrs.switch = 'on';
            const $tile = _renderTile(device, deviceType, newAttrs, device.label || device.name);
            $modal.empty()
                .append($('<button class="dtm-close">&times;</button>').on('click', closeModal))
                .append($tile);
            if (deviceType === 'dimmer' || deviceType === 'color') {
                $modal.append('<div class="dtm-hint">Long-press for dimmer</div>');
            }
            _attachHandlers($tile, device, deviceType, newAttrs);
        });
    $panel.append($back);

    // Track current level for the slider
    let _currentSliderLevel = currentLevel;

    /**
     * Update slider visuals and send debounced command.
     * @param {number} level - New level (0-99)
     */
    const updateSlider = (level) => {
        level = Math.max(0, Math.min(99, level));
        _currentSliderLevel = level;
        $fill.css('height', `${level}%`);
        $pct.text(`${level}`);

        // Also update attrs for when we go back to tile view
        attrs.level = String(level);
        if (level > 0) attrs.switch = 'on';

        // Debounced command
        if (_sliderDebounceTimer) clearTimeout(_sliderDebounceTimer);
        _sliderDebounceTimer = setTimeout(async () => {
            try {
                await api.post(`/devices/${device.id}/command`, {
                    command: 'setLevel',
                    args: [level]
                });
            } catch (error) {
                console.error('setLevel failed:', error);
            }
        }, SLIDER_DEBOUNCE_MS);
    };

    // Plus/minus button handlers
    $plus.on('click', (e) => {
        e.stopPropagation();
        updateSlider(_currentSliderLevel + 10);
    });
    $minus.on('click', (e) => {
        e.stopPropagation();
        updateSlider(Math.max(0, _currentSliderLevel - 10));
    });

    // Track drag (pointer events for unified mouse/touch)
    let isDragging = false;

    const getLevel = (clientY) => {
        const rect = $track[0].getBoundingClientRect();
        const y = clientY - rect.top;
        const pct = 1 - (y / rect.height);
        return Math.round(Math.max(0, Math.min(99, pct * 99)));
    };

    $track.on('pointerdown', (e) => {
        isDragging = true;
        $track[0].setPointerCapture(e.pointerId);
        updateSlider(getLevel(e.clientY));
    });

    $track.on('pointermove', (e) => {
        if (!isDragging) return;
        updateSlider(getLevel(e.clientY));
    });

    $track.on('pointerup pointercancel', () => {
        isDragging = false;
    });

    // Replace modal content with dimmer panel
    $modal.empty()
        .append($('<button class="dtm-close">&times;</button>').on('click', closeModal))
        .append($panel);
}

/* =============================================================================
   Color Picker
   ============================================================================= */

/**
 * Toggle the color picker panel inside the dimmer view.
 * @param {jQuery} $panel - Dimmer panel element
 * @param {object} device - Device data
 * @param {object} attrs - Current attributes
 */
function _toggleColorPanel($panel, device, attrs) {
    let $colorPanel = $panel.find('.dtm-color-panel');

    if ($colorPanel.length) {
        // Toggle visibility
        $colorPanel.toggleClass('show');
        return;
    }

    // Build color picker panel
    $colorPanel = $('<div class="dtm-color-panel">');

    // Header
    $colorPanel.append(
        $('<div class="dtm-color-panel-header">').text('Color')
    );

    // Color wheel canvas
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 160;
    canvas.className = 'dtm-color-wheel';
    _drawColorWheel(canvas);

    const $canvas = $(canvas);
    $colorPanel.append($canvas);

    // Preview swatch
    const $preview = $('<div class="dtm-color-preview">');
    $colorPanel.append($preview);

    // Preset chips
    const $presets = $('<div class="dtm-color-presets">');
    for (const preset of COLOR_PRESETS) {
        const $chip = $('<div class="dtm-color-chip">')
            .css('background-color', preset.color)
            .attr('title', preset.name)
            .on('click', () => {
                $preview.css('background-color', preset.color);
                _sendColor(device, preset.h, preset.s, 100);
            });
        $presets.append($chip);
    }
    $colorPanel.append($presets);

    // Canvas click handler
    let isPickingColor = false;

    const pickColor = (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = (e.clientX || e.touches[0].clientX) - rect.left;
        const y = (e.clientY || e.touches[0].clientY) - rect.top;
        const ctx = canvas.getContext('2d');
        const pixel = ctx.getImageData(x, y, 1, 1).data;

        if (pixel[3] === 0) return; // Transparent area (outside wheel)

        const hex = `rgb(${pixel[0]}, ${pixel[1]}, ${pixel[2]})`;
        $preview.css('background-color', hex);

        // Convert RGB to HSV for Hubitat
        const hsv = _rgbToHsv(pixel[0], pixel[1], pixel[2]);
        _sendColor(device, hsv.h, hsv.s, hsv.v);
    };

    $canvas.on('pointerdown', (e) => {
        isPickingColor = true;
        canvas.setPointerCapture(e.pointerId);
        pickColor(e);
    });
    $canvas.on('pointermove', (e) => {
        if (isPickingColor) pickColor(e);
    });
    $canvas.on('pointerup pointercancel', () => {
        isPickingColor = false;
    });

    $panel.append($colorPanel);

    // Show with animation
    requestAnimationFrame(() => {
        $colorPanel.addClass('show');
    });
}

/**
 * Draw an HSV color wheel on a canvas.
 * @param {HTMLCanvasElement} canvas - Target canvas
 */
function _drawColorWheel(canvas) {
    const ctx = canvas.getContext('2d');
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const radius = Math.min(cx, cy) - 2;

    // Draw pixel by pixel for accurate color wheel
    const imageData = ctx.createImageData(canvas.width, canvas.height);
    const data = imageData.data;

    for (let y = 0; y < canvas.height; y++) {
        for (let x = 0; x < canvas.width; x++) {
            const dx = x - cx;
            const dy = y - cy;
            const dist = Math.sqrt(dx * dx + dy * dy);

            if (dist <= radius) {
                const angle = Math.atan2(dy, dx);
                const hue = ((angle * 180 / Math.PI) + 360) % 360;
                const saturation = dist / radius;
                const rgb = _hsvToRgb(hue, saturation * 100, 100);

                const idx = (y * canvas.width + x) * 4;
                data[idx] = rgb.r;
                data[idx + 1] = rgb.g;
                data[idx + 2] = rgb.b;
                data[idx + 3] = 255;
            }
        }
    }

    ctx.putImageData(imageData, 0, 0);
}

/**
 * Send color command to device.
 * @param {object} device - Device data
 * @param {number} h - Hue (0-360)
 * @param {number} s - Saturation (0-100)
 * @param {number} v - Value/brightness (0-100)
 */
async function _sendColor(device, h, s, v) {
    try {
        // Hubitat setColor expects a map: [hue(0-100), saturation(0-100), level(0-100)]
        // Hubitat hue is 0-100 (not 0-360)
        const hubitatHue = Math.round((h / 360) * 100);
        await api.post(`/devices/${device.id}/command`, {
            command: 'setColor',
            args: [{ hue: hubitatHue, saturation: Math.round(s), level: Math.round(v) }]
        });
    } catch (error) {
        console.error('setColor failed:', error);
    }
}

/* =============================================================================
   Color Conversion Utilities
   ============================================================================= */

/**
 * Convert HSV to RGB.
 * @param {number} h - Hue (0-360)
 * @param {number} s - Saturation (0-100)
 * @param {number} v - Value (0-100)
 * @returns {{r: number, g: number, b: number}}
 */
function _hsvToRgb(h, s, v) {
    s /= 100;
    v /= 100;
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;

    let r, g, b;
    if (h < 60)       { r = c; g = x; b = 0; }
    else if (h < 120) { r = x; g = c; b = 0; }
    else if (h < 180) { r = 0; g = c; b = x; }
    else if (h < 240) { r = 0; g = x; b = c; }
    else if (h < 300) { r = x; g = 0; b = c; }
    else               { r = c; g = 0; b = x; }

    return {
        r: Math.round((r + m) * 255),
        g: Math.round((g + m) * 255),
        b: Math.round((b + m) * 255)
    };
}

/**
 * Convert RGB to HSV.
 * @param {number} r - Red (0-255)
 * @param {number} g - Green (0-255)
 * @param {number} b - Blue (0-255)
 * @returns {{h: number, s: number, v: number}}
 */
function _rgbToHsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const d = max - min;

    let h = 0;
    if (d !== 0) {
        if (max === r) h = 60 * (((g - b) / d) % 6);
        else if (max === g) h = 60 * (((b - r) / d) + 2);
        else h = 60 * (((r - g) / d) + 4);
    }
    if (h < 0) h += 360;

    const s = max === 0 ? 0 : (d / max) * 100;
    const v = max * 100;

    return { h: Math.round(h), s: Math.round(s), v: Math.round(v) };
}
