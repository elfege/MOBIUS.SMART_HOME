/**
 * Sonos Driver Controller
 * =======================
 * Renders one card per Sonos speaker (room/coordinator) with command controls:
 * TTS announce (text + voice), setVolume, restoreVolume, persist/lock volume,
 * play mp3 (URL), stop. All backed by /api/sonos/* (services/sonos, local UPnP).
 *
 * Plain ES6 module + fetch (matches dashboard-controller conventions). Speaker
 * state (saved/persisted volume) is DB-backed server-side; this is just the UI.
 */

const $ = (sel, root = document) => root.querySelector(sel);

async function jpost(url, body) {
    const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* ignore */ }
    return { ok: r.ok, data };
}

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let VOICE_OPTIONS = '';   // cached <option> html for the voice <select>s
let DEFAULT_VOICE = '';

async function loadVoices() {
    try {
        const r = await fetch('/api/sonos/voices');
        const d = await r.json();
        DEFAULT_VOICE = d.default || '';
        VOICE_OPTIONS = (d.voices || [])
            .map(v => `<option value="${esc(v.id)}"${v.id === DEFAULT_VOICE ? ' selected' : ''}>${esc(v.name)}</option>`)
            .join('');
    } catch (_) {
        VOICE_OPTIONS = '<option value="">(voices unavailable)</option>';
    }
}

function speakerCard(sp) {
    const room = esc(sp.room || sp.ip);
    const vol = sp.volume == null ? 30 : sp.volume;
    const state = esc(sp.state || (sp.error ? 'error' : '—'));
    const locked = sp.persist_volume ? 'checked' : '';
    const lockLevel = sp.persisted_level == null ? vol : sp.persisted_level;
    const playing = esc((sp.current_uri || '').split('/').pop() || '');
    return `
    <div class="instance-card sonos-card" data-room="${room}" style="display:flex;flex-direction:column;gap:.6rem;">
        <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
            <h3 style="margin:0;">🔊 ${room}</h3>
            <span class="app-type-badge">${state}</span>
        </div>
        <div style="font-size:.8rem;opacity:.7;">vol ${vol}${playing ? ` · ${playing}` : ''}${sp.error ? ` · ${esc(sp.error)}` : ''}</div>

        <label style="font-size:.8rem;">Volume: <span class="vol-val">${vol}</span>
            <input type="range" class="vol-slider" min="0" max="100" value="${vol}" style="width:100%;">
        </label>
        <div style="display:flex;gap:.4rem;flex-wrap:wrap;">
            <button class="btn btn-secondary btn-set-vol">Set volume</button>
            <button class="btn btn-secondary btn-restore-vol">Restore</button>
        </div>
        <label style="font-size:.8rem;display:flex;align-items:center;gap:.4rem;">
            <input type="checkbox" class="chk-persist" ${locked}>
            Lock volume at <input type="number" class="persist-level" min="0" max="100" value="${lockLevel}" style="width:4rem;">
            <span title="Re-assert this level if another system changes it" style="cursor:help;opacity:.6;">?</span>
        </label>

        <hr style="border:none;border-top:1px solid var(--color-border);margin:.2rem 0;">
        <label style="font-size:.8rem;">Announce (TTS):
            <input type="text" class="tts-text" placeholder="e.g. Dinner is ready" style="width:100%;">
        </label>
        <div style="display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;">
            <select class="tts-voice" style="flex:1;min-width:8rem;">${VOICE_OPTIONS}</select>
            <button class="btn btn-primary btn-announce">Speak</button>
        </div>

        <label style="font-size:.8rem;">Play mp3 URL:
            <input type="text" class="play-url" placeholder="http://…/clip.mp3" style="width:100%;">
        </label>
        <div style="display:flex;gap:.4rem;flex-wrap:wrap;">
            <button class="btn btn-secondary btn-play">Play</button>
            <button class="btn btn-secondary btn-stop">Stop</button>
        </div>
        <div class="sonos-card-msg" style="font-size:.75rem;min-height:1em;opacity:.8;"></div>
    </div>`;
}

async function render() {
    const status = $('#sonos-status');
    const grid = $('#sonos-speakers');
    status.textContent = 'Loading speakers…';
    try {
        const r = await fetch('/api/sonos/state');
        const d = await r.json();
        const list = d.speakers || [];
        if (!list.length) { status.textContent = 'No Sonos speakers found.'; grid.innerHTML = ''; return; }
        status.textContent = `${list.length} speaker${list.length > 1 ? 's' : ''}`;
        grid.innerHTML = list.map(speakerCard).join('');
    } catch (e) {
        status.textContent = 'Failed to load speakers: ' + e;
    }
}

function cardOf(el) { return el.closest('.sonos-card'); }
function roomOf(el) { return cardOf(el).dataset.room; }
function msg(el, text, ok = true) {
    const m = $('.sonos-card-msg', cardOf(el));
    m.textContent = text; m.style.color = ok ? 'var(--color-primary)' : 'crimson';
}

function wire() {
    const grid = $('#sonos-speakers');

    grid.addEventListener('input', (e) => {
        if (e.target.classList.contains('vol-slider')) {
            $('.vol-val', cardOf(e.target)).textContent = e.target.value;
        }
    });

    grid.addEventListener('click', async (e) => {
        const t = e.target;
        if (!t.classList || !cardOf(t)) return;
        const room = roomOf(t);

        if (t.classList.contains('btn-set-vol')) {
            const v = +$('.vol-slider', cardOf(t)).value;
            const { ok } = await jpost('/api/sonos/volume', { room, volume: v });
            msg(t, ok ? `volume → ${v}` : 'set volume failed', ok);
        } else if (t.classList.contains('btn-restore-vol')) {
            const { ok, data } = await jpost('/api/sonos/restore-volume', { room });
            msg(t, ok && data.ok ? `restored → ${data.volume}` : (data.error || 'no saved volume'), ok && data.ok);
        } else if (t.classList.contains('btn-announce')) {
            const text = $('.tts-text', cardOf(t)).value.trim();
            const voice = $('.tts-voice', cardOf(t)).value;
            if (!text) { msg(t, 'enter text first', false); return; }
            const { ok } = await jpost('/api/sonos/announce', { room, text, voice });
            msg(t, ok ? 'announcing…' : 'announce failed', ok);
        } else if (t.classList.contains('btn-play')) {
            const url = $('.play-url', cardOf(t)).value.trim();
            if (!url) { msg(t, 'enter an mp3 URL', false); return; }
            const { ok } = await jpost('/api/sonos/play', { room, url });
            msg(t, ok ? 'playing…' : 'play failed', ok);
        } else if (t.classList.contains('btn-stop')) {
            const { ok } = await jpost('/api/sonos/stop', { room });
            msg(t, ok ? 'stopped' : 'stop failed', ok);
        }
    });

    grid.addEventListener('change', async (e) => {
        const t = e.target;
        if (t.classList.contains('chk-persist')) {
            const room = roomOf(t);
            const enabled = t.checked;
            const level = +$('.persist-level', cardOf(t)).value;
            const { ok } = await jpost('/api/sonos/persist-volume', { room, enabled, level });
            msg(t, ok ? (enabled ? `volume locked @ ${level}` : 'lock off') : 'lock failed', ok);
        }
    });

    $('#btn-refresh-sonos')?.addEventListener('click', render);
}

(async function init() {
    await loadVoices();
    await render();
    wire();
})();
