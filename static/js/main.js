/**
 * MOBIUS.HOME Main JavaScript
 *
 * Entry point for the frontend application.
 * Uses ES6 modules with jQuery for DOM manipulation.
 */

// Global API client
export const api = {
    /**
     * Make API request
     * @param {string} endpoint - API endpoint
     * @param {object} options - Fetch options
     * @returns {Promise<any>} Response data
     */
    async request(endpoint, options = {}) {
        const url = `/api${endpoint}`;

        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json'
            }
        };

        const response = await fetch(url, { ...defaultOptions, ...options });

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `HTTP ${response.status}`);
        }

        return response.json();
    },

    // Convenience methods
    get(endpoint) {
        return this.request(endpoint);
    },

    post(endpoint, data) {
        return this.request(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },

    put(endpoint, data) {
        return this.request(endpoint, {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    },

    delete(endpoint) {
        return this.request(endpoint, { method: 'DELETE' });
    }
};

// Global utility functions
export const utils = {
    /**
     * Format date for display
     * @param {string} dateStr - ISO date string
     * @returns {string} Formatted date
     */
    formatDate(dateStr) {
        if (!dateStr) return 'N/A';
        return new Date(dateStr).toLocaleString();
    },

    /**
     * Escape HTML to prevent XSS
     * @param {string} str - String to escape
     * @returns {string} Escaped string
     */
    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    },

    /**
     * Show notification toast
     * @param {string} message - Message to show
     * @param {string} type - 'success', 'error', 'warning'
     */
    notify(message, type = 'success') {
        // Simple alert for now, could be replaced with toast library
        if (type === 'error') {
            console.error(message);
            alert(`Error: ${message}`);
        } else {
            console.log(message);
        }
    },

    /**
     * Copy text to clipboard with visual feedback on the trigger element.
     *
     * Uses the modern Clipboard API with a fallback for older browsers.
     * Briefly changes the trigger's text to "Copied!" then restores it.
     *
     * @param {string} text - The text to copy
     * @param {HTMLElement} [triggerEl] - Button/element that triggered the copy (for feedback)
     * @param {string} [originalLabel] - Label to restore after feedback (default: element's current text)
     */
    async copyToClipboard(text, triggerEl, originalLabel) {
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            // Fallback: hidden textarea
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }

        // Visual feedback on trigger element
        if (triggerEl) {
            const saved = originalLabel || triggerEl.textContent;
            triggerEl.textContent = 'Copied!';
            triggerEl.classList.add('copy-flash');
            setTimeout(() => {
                triggerEl.textContent = saved;
                triggerEl.classList.remove('copy-flash');
            }, 1200);
        }
    }
};

// Initialize on DOM ready
$(document).ready(function() {
    console.log('MOBIUS.HOME initialized');
});
