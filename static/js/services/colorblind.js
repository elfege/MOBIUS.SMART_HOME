/**
 * Colorblind palette service.
 *
 * Default mode: the project's saturated color set (matches main.css vars).
 * Colorblind mode: the Okabe-Ito CVD-safe palette (designed by Masataka Okabe
 * and Kei Ito specifically to be distinguishable under protanopia,
 * deuteranopia, and tritanopia). Eight base hues + five lightness variants
 * for charts with more than 8 categories.
 *
 * Refs:
 *   https://jfly.uni-koeln.de/color/   (Okabe-Ito original)
 *   https://www.nature.com/articles/nmeth.1618  (Wong, Nature Methods 2011)
 *
 * Toggle: body.classList.contains('colorblind'). Set early by an inline
 * script in base.html that reads localStorage 'colorblind_mode' (cached for
 * first-paint), then fetches /api/system_settings/colorblind_mode in the
 * background to refresh the cache.
 */

// Project default palette — saturated, NOT CVD-safe.
// Kept here so individual chart code doesn't have to maintain its own list.
const DEFAULT_PALETTE = [
    '#4A9FD8',  // bright blue
    '#E89B3C',  // orange
    '#22c55e',  // green
    '#ef4444',  // red
    '#a855f7',  // purple
    '#ec4899',  // pink
    '#14b8a6',  // teal
    '#f59e0b',  // amber
    '#6366f1',  // indigo
    '#84cc16',  // lime
    '#06b6d4',  // cyan
    '#f43f5e',  // rose
    '#8b5cf6',  // violet
];

// Okabe-Ito + lightness variants. CVD-safe across the three common
// dichromacies. Order matters — adjacent hues are deliberately distinct.
const COLORBLIND_PALETTE = [
    '#E69F00',  // orange
    '#56B4E9',  // sky blue
    '#009E73',  // bluish green
    '#F0E442',  // yellow
    '#0072B2',  // dark blue
    '#D55E00',  // vermilion
    '#CC79A7',  // reddish purple
    '#999999',  // neutral grey
    // Extensions — lighter variants of the base, ordered to keep
    // adjacent palette indices distinguishable.
    '#FFD58A',  // light orange
    '#9CD3F0',  // light sky blue
    '#7DCFB5',  // light bluish green
    '#A6B3D8',  // light slate
    '#F0A78A',  // light vermilion
];

/**
 * Get an N-element color palette appropriate for the current accessibility mode.
 * @param {number} count - How many distinct colors needed.
 * @returns {string[]} Hex color strings.
 */
export function getPalette(count = 8) {
    const cb = document.body.classList.contains('colorblind');
    const source = cb ? COLORBLIND_PALETTE : DEFAULT_PALETTE;
    // Cycle if caller asks for more colors than the palette has.
    const out = [];
    for (let i = 0; i < count; i++) {
        out.push(source[i % source.length]);
    }
    return out;
}

/**
 * Single accent color (for places that only need one).
 * @returns {string}
 */
export function getAccent() {
    return document.body.classList.contains('colorblind')
        ? COLORBLIND_PALETTE[1]  // sky blue, CVD-safe
        : '#4A9FD8';
}

/**
 * Apply colorblind mode based on localStorage cache (fast, first-paint)
 * AND kick off a background fetch of /api/system_settings/colorblind_mode
 * to refresh the cache. Idempotent — safe to call multiple times.
 *
 * Called by an inline script in base.html so the body class is set
 * before any chart renders.
 */
export async function initColorblindMode() {
    try {
        const cached = localStorage.getItem('colorblind_mode');
        if (cached === 'true') {
            document.body.classList.add('colorblind');
        }
        // Refresh from server in background. If different, update + reload
        // any open charts (they pick up new palette on next render).
        fetch('/api/system_settings/colorblind_mode')
            .then(r => r.ok ? r.json() : null)
            .then(row => {
                if (!row) return;
                const onServer = row.value === true || row.value === 'true';
                if (onServer !== (cached === 'true')) {
                    localStorage.setItem('colorblind_mode', String(onServer));
                    document.body.classList.toggle('colorblind', onServer);
                    // Notify anyone listening that the mode changed.
                    document.dispatchEvent(new CustomEvent('colorblind:changed', {
                        detail: { enabled: onServer },
                    }));
                }
            })
            .catch(() => {});
    } catch (e) {
        // localStorage disabled or fetch failed — just leave body class as-is.
    }
}
