/**
 * Device Refresh Modal
 *
 * Tiny reusable modal that prompts for a device id and fires a single
 * POST to /api/devices/refresh?device_id=<n>. Fire-and-forget:
 * dismisses immediately on submit, the actual refresh runs in the
 * background and surfaces success/failure via a transient toast.
 *
 *   openDeviceRefreshModal()            → blank input (defaults to "0 = all")
 *   openDeviceRefreshModal(146)         → prefilled with 146 (single device)
 *   openDeviceRefreshModal(146, "Fan Bathroom") → prefilled + shows the label
 *                                         so the user can confirm before submit
 *
 * Accepts EITHER the canonical id (the #146 from chips/cards) or the
 * Hubitat per-hub id (e.g. 2781 — the number you see in the Hubitat web
 * UI). Backend resolves either path; modal just forwards what was typed.
 *
 * 0 or empty input → refresh all devices (slow; the operator's notice in
 * the modal text warns them).
 *
 * Styled inline (no separate CSS file) — the modal is small enough and
 * lives only in transient lifecycle, so the cost of a shared stylesheet
 * isn't justified.
 */

import { api } from '../main.js';

/**
 * Open the refresh modal. Returns the backdrop element so the caller can
 * inspect / close programmatically; usually ignored.
 *
 * @param {number|string|null} prefillId  Optional id to prefill.
 * @param {string|null}        labelHint  Optional human-readable label for confirmation.
 */
export function openDeviceRefreshModal(prefillId = null, labelHint = null) {
    // Clean up any previous instance — only one refresh modal at a time.
    document.querySelectorAll('.drm-backdrop').forEach((n) => n.remove());

    const backdrop = document.createElement('div');
    backdrop.className = 'drm-backdrop';
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(0,0,0,0.55);
        display: flex; align-items: center; justify-content: center;
    `;

    const card = document.createElement('div');
    card.className = 'drm-card';
    card.style.cssText = `
        background: var(--color-bg, #1d2026);
        color: var(--color-text, #e6e8eb);
        border: 1px solid var(--color-border, #2c303a);
        border-radius: 8px;
        padding: 1.25rem 1.4rem;
        width: min(420px, 92vw);
        box-shadow: 0 12px 36px rgba(0,0,0,0.45);
        font-family: inherit;
    `;

    const prefillStr = (prefillId === null || prefillId === undefined) ? '' : String(prefillId);
    const hint = labelHint
        ? `<div style="font-size:0.85rem; opacity:0.7; margin: 0.15rem 0 0.6rem;">${escapeHtml(labelHint)}</div>`
        : '';

    card.innerHTML = `
        <h3 style="margin:0 0 0.4rem; font-size:1.05rem; font-weight:600;">
            Refresh Device Cache
        </h3>
        ${hint}
        <p style="margin:0 0 0.85rem; font-size:0.88rem; line-height:1.45; opacity:0.85;">
            Enter the device's number — either the canonical id (the
            <code>#N</code> on chips and cards) or the Hubitat per-hub id
            (the number you see in the hub's web UI). The backend tries
            canonical first, then per-hub.<br>
            <span style="opacity:0.75;">
                <b>0</b> or empty = refresh every device on every hub
                (may take several seconds; fire-and-forget).
            </span>
        </p>
        <input type="text"
               inputmode="numeric" pattern="[0-9]*"
               class="drm-input"
               placeholder="0 = all devices"
               value="${escapeHtml(prefillStr)}"
               style="
                    width: 100%; box-sizing: border-box;
                    padding: 0.55rem 0.7rem;
                    background: rgba(255,255,255,0.06);
                    color: inherit;
                    border: 1px solid var(--color-border, #2c303a);
                    border-radius: 4px;
                    font-size: 1rem;
                    margin-bottom: 0.9rem;
               ">
        <div style="display:flex; gap:0.6rem; justify-content:flex-end;">
            <button class="drm-cancel" style="
                padding: 0.45rem 0.95rem;
                background: transparent;
                color: inherit;
                border: 1px solid var(--color-border, #3a3f4b);
                border-radius: 4px;
                cursor: pointer; font: inherit;
            ">Cancel</button>
            <button class="drm-submit" style="
                padding: 0.45rem 0.95rem;
                background: var(--color-primary, #7aa2f7);
                color: #0c0e12;
                border: 0; border-radius: 4px;
                cursor: pointer; font: inherit; font-weight: 600;
            ">Refresh</button>
        </div>
    `;

    backdrop.appendChild(card);
    document.body.appendChild(backdrop);

    const input  = card.querySelector('.drm-input');
    const submit = card.querySelector('.drm-submit');
    const cancel = card.querySelector('.drm-cancel');

    // Autofocus the input — and select prefilled content so a quick
    // re-type replaces it without an extra click.
    setTimeout(() => { input.focus(); if (prefillStr) input.select(); }, 30);

    function close() { backdrop.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(); }
        else if (e.key === 'Enter' && document.activeElement === input) {
            e.preventDefault(); fire();
        }
    }
    document.addEventListener('keydown', onKey);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    cancel.addEventListener('click', close);
    submit.addEventListener('click', fire);

    function fire() {
        const raw = (input.value || '').trim();
        // Strip a leading '#' so chips' visual format (#146) works directly.
        const cleaned = raw.replace(/^#/, '');
        const numeric = cleaned === '' ? '0' : cleaned;

        // Fire-and-forget: dispatch the POST, dismiss the modal NOW.
        // The promise resolves in the background; surface result via toast.
        _toast(numeric === '0'
            ? 'Refreshing all devices… (slow)'
            : `Refreshing device #${numeric}…`);
        close();

        api.post(`/devices/refresh?device_id=${encodeURIComponent(numeric)}`, {})
            .then((res) => {
                if (res && res.ok) {
                    const r = res.resolved || {};
                    const desc = numeric === '0'
                        ? `Refreshed all (${res.total_native ?? '?'} devices)`
                        : `Refreshed ${r.label || `#${r.canonical_id ?? numeric}`}` +
                          (res.caps_count != null ? ` · ${res.caps_count} caps` : '');
                    _toast(desc, { ok: true });
                } else {
                    _toast((res && res.reason) || 'Refresh failed (no detail)', { ok: false });
                }
            })
            .catch((err) => {
                _toast(`Refresh request failed: ${err && err.message || err}`, { ok: false });
            });
    }

    return backdrop;
}

/* ------------------------------------------------------------------ */
/* Internal helpers                                                    */
/* ------------------------------------------------------------------ */

/**
 * Very small toast notification. Stacks at bottom-right and auto-dismisses
 * after 5s. Intentionally not extracted to a global module — keeping the
 * device-refresh feature self-contained — but trivial to lift if other
 * features want the same shape.
 *
 * @param {string} msg
 * @param {{ok?: boolean}} [opts]  ok=true→accent, ok=false→error tint, default neutral.
 */
function _toast(msg, opts = {}) {
    let host = document.getElementById('drm-toast-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'drm-toast-host';
        host.style.cssText = `
            position: fixed; right: 1rem; bottom: 1rem; z-index: 10001;
            display: flex; flex-direction: column; gap: 0.4rem;
            pointer-events: none;
        `;
        document.body.appendChild(host);
    }
    const tone = (opts.ok === true)
        ? 'background: rgba(64,160,90,0.18); border-color: rgba(64,160,90,0.6);'
        : (opts.ok === false)
            ? 'background: rgba(180,70,70,0.22); border-color: rgba(180,70,70,0.65);'
            : 'background: rgba(255,255,255,0.10); border-color: rgba(255,255,255,0.25);';

    const t = document.createElement('div');
    t.style.cssText = `
        ${tone}
        color: var(--color-text, #e6e8eb);
        border: 1px solid;
        padding: 0.55rem 0.85rem;
        border-radius: 4px;
        font-size: 0.88rem;
        max-width: 360px;
        pointer-events: auto;
        box-shadow: 0 4px 12px rgba(0,0,0,0.35);
    `;
    t.textContent = msg;
    host.appendChild(t);
    setTimeout(() => t.remove(), 5000);
}

/**
 * Minimal HTML escape for the few interpolated values in our template.
 * The modal is operator-internal but defensive against any value with
 * special chars (a device label could in theory carry one).
 */
function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/* ------------------------------------------------------------------ */
/* Reusable refresh-button factory                                     */
/* ------------------------------------------------------------------ */

/**
 * Build a small refresh-icon button that opens the modal on click. Use
 * from any device-listing surface — drop the returned <button> next to
 * the panel header / section title.
 *
 *   header.appendChild(makeDeviceRefreshButton({ title: 'Refresh device cache' }));
 *   modal.appendChild(makeDeviceRefreshButton({ prefillId: 146, labelHint: 'Fan Bathroom' }));
 *
 * @param {{prefillId?:number|string, labelHint?:string, title?:string}} [opts]
 * @returns {HTMLButtonElement}
 */
export function makeDeviceRefreshButton(opts = {}) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'drm-trigger';
    btn.title = opts.title || 'Refresh device cache from Hubitat';
    btn.setAttribute('aria-label', btn.title);
    // ↻ Unicode anticlockwise open circle arrow — same glyph the
    // dashboard already uses elsewhere for refresh affordances.
    btn.textContent = '↻';
    btn.style.cssText = `
        background: transparent;
        color: inherit;
        border: 1px solid var(--color-border, #3a3f4b);
        border-radius: 4px;
        padding: 0.2rem 0.5rem;
        font: inherit;
        font-size: 0.95rem;
        cursor: pointer;
        line-height: 1;
        opacity: 0.85;
    `;
    btn.addEventListener('mouseenter', () => { btn.style.opacity = '1.0'; });
    btn.addEventListener('mouseleave', () => { btn.style.opacity = '0.85'; });
    btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        openDeviceRefreshModal(opts.prefillId ?? null, opts.labelHint ?? null);
    });
    return btn;
}
