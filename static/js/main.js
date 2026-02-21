/**
 * 0_SMART_HOME Main JavaScript
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
    }
};

// Initialize on DOM ready
$(document).ready(function() {
    console.log('0_SMART_HOME initialized');
});
