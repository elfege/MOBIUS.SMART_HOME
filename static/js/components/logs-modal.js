/**
 * Live Logs modal — navbar "Logs" button (operator directive 2026-07-12).
 *
 * The PATTERN of Hubitat's Logs page (the mature reference): a live streaming
 * log with clickable per-source name chips, level filters and timestamps —
 * restyled to the MOBIUS dark theme and the colorblind-safe palette
 * (red/blue/white/black families, shape-first level badges [E][W][I][D];
 * NEVER green/yellow/orange). Pattern only — none of Hubitat's trade dress.
 *
 * Data: polls GET /api/logs/tail?after=<cursor> every 1.5s while open
 * (backend = root-logger ring buffer, services/log_stream.py) and keeps a
 * client-side array capped at MAX_CLIENT_ENTRIES. ALL filtering (sources,
 * levels, text) is client-side, so toggling a filter re-renders instantly
 * from the local buffer — no cursor reset, no refetch.
 *
 * Filter surfaces, per the operator's spec — BOTH kept in sync:
 *   1. a chips strip of sources (click chip = toggle that source), and
 *   2. a "Sources ▾" dropdown with the same list as checkboxes.
 * Sources are logger names: per-instance apps ({AppClass}.{label}),
 * services (services.*), app.py — the running apps/drivers/processes.
 */

const POLL_MS = 1500;
const MAX_CLIENT_ENTRIES = 5000;

// CVD-safe source palette: blues / reds / lavenders / grays only.
const SRC_PALETTE = ['#89b4fa', '#f38ba8', '#cdd6f4', '#74a8fa', '#eba0ac',
                     '#b4befe', '#7aa2f7', '#f2cdcd', '#a6adc8', '#5c7cfa'];
const LEVELS = ['ERROR', 'WARNING', 'INFO', 'DEBUG'];
const LEVEL_BADGE = { ERROR: 'E', CRITICAL: 'E', WARNING: 'W', INFO: 'I', DEBUG: 'D' };

let built = false;          // DOM injected?
let timer = null;           // poll timer while open
let cursor = 0;             // /api/logs/tail cursor
let entries = [];           // client buffer (capped)
let knownSources = [];      // [{src, count}] from /api/logs/sources
let excluded = new Set();   // sources toggled OFF (default: everything ON)
let levelsOn = new Set(LEVELS);
let query = '';             // text filter
let paused = false;

function srcColor(src) {
    let h = 0;
    for (let i = 0; i < src.length; i++) h = ((h << 5) - h + src.charCodeAt(i)) | 0;
    return SRC_PALETTE[Math.abs(h) % SRC_PALETTE.length];
}

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtTs(epoch) {
    const d = new Date(epoch * 1000);
    const p = (n, w = 2) => String(n).padStart(w, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function buildDom() {
    if (built) return;
    built = true;
    $('body').append(`
<div id="logs-modal" class="logsm-overlay" style="display:none;">
  <div class="logsm-panel" role="dialog" aria-label="Live backend logs">
    <div class="logsm-head">
      <strong>Live Logs</strong>
      <span class="logsm-state" id="logsm-state">connecting…</span>
      <span class="logsm-spacer"></span>
      <div class="logsm-dd">
        <button id="logsm-dd-btn" class="logsm-btn" title="Filter by source — running apps, drivers and services (same list as the chips)">Sources ▾</button>
        <div id="logsm-dd-panel" class="logsm-dd-panel" style="display:none;"></div>
      </div>
      <span class="logsm-levels" id="logsm-levels" title="Level filters (shape-first: E=error W=warning I=info D=debug)"></span>
      <input id="logsm-q" class="logsm-q" type="search" placeholder="filter text…"
             title="Substring filter over source + message">
      <button id="logsm-pause" class="logsm-btn" title="Freeze the display (polling continues)">Pause</button>
      <button id="logsm-clear" class="logsm-btn" title="Clear the display buffer">Clear</button>
      <button id="logsm-close" class="logsm-btn logsm-x" aria-label="Close">✕</button>
    </div>
    <div class="logsm-chips" id="logsm-chips"
         title="Click a source to toggle it. Sources are logger names — one per running app instance / driver / service."></div>
    <pre class="logsm-stream" id="logsm-stream"></pre>
  </div>
</div>`);

    // ----- wiring ------------------------------------------------------------
    $('#logsm-close').on('click', close);
    $('#logs-modal').on('mousedown', function (e) { if (e.target === this) close(); });
    $(document).on('keydown.logsModal', function (e) {
        if (e.key === 'Escape' && $('#logs-modal').is(':visible')) close();
    });
    $('#logsm-pause').on('click', function () {
        paused = !paused;
        $(this).text(paused ? 'Resume' : 'Pause').toggleClass('active', paused);
        if (!paused) render();
    });
    $('#logsm-clear').on('click', function () { entries = []; render(); });
    $('#logsm-q').on('input', function () { query = $(this).val().toLowerCase(); render(); });

    // Level checkboxes (shape-first badges, CVD-safe colors via CSS)
    const $lv = $('#logsm-levels');
    LEVELS.forEach(function (lv) {
        $lv.append(`<label class="logsm-lv logsm-lv-${lv.toLowerCase()}">
            <input type="checkbox" data-level="${lv}" checked>
            <span class="logsm-badge logsm-badge-${lv.toLowerCase()}">${LEVEL_BADGE[lv]}</span></label>`);
    });
    $lv.on('change', 'input', function () {
        const lv = $(this).data('level');
        if (this.checked) levelsOn.add(lv); else levelsOn.delete(lv);
        render();
    });

    // Sources dropdown open/close
    $('#logsm-dd-btn').on('click', function (e) {
        e.stopPropagation();
        $('#logsm-dd-panel').toggle();
    });
    $(document).on('click.logsModalDd', function (e) {
        if (!$(e.target).closest('.logsm-dd').length) $('#logsm-dd-panel').hide();
    });
}

/** Rebuild BOTH filter surfaces (chips strip + dropdown checkboxes) from
 *  knownSources, preserving the excluded set. */
function renderSources() {
    const $chips = $('#logsm-chips').empty();
    const $dd = $('#logsm-dd-panel').empty();
    $dd.append(`<div class="logsm-dd-tools">
        <button class="logsm-btn" id="logsm-src-all">All</button>
        <button class="logsm-btn" id="logsm-src-none">None</button></div>`);
    knownSources.forEach(function (s) {
        const off = excluded.has(s.src);
        $chips.append(`<span class="logsm-chip${off ? ' off' : ''}" data-src="${esc(s.src)}"
            style="--chip:${srcColor(s.src)}">${esc(s.src)} <em>${s.count}</em></span>`);
        $dd.append(`<label class="logsm-dd-row"><input type="checkbox" data-src="${esc(s.src)}"
            ${off ? '' : 'checked'}> <span style="color:${srcColor(s.src)}">${esc(s.src)}</span>
            <em>${s.count}</em></label>`);
    });
    function toggle(src, on) {
        if (on) excluded.delete(src); else excluded.add(src);
        renderSources();   // keep both surfaces in sync
        render();
    }
    $chips.off('click').on('click', '.logsm-chip', function () {
        const src = $(this).data('src');
        toggle(src, excluded.has(src));
    });
    $dd.off('change click')
       .on('change', 'input', function () { toggle($(this).data('src'), this.checked); })
       .on('click', '#logsm-src-all', function () { excluded.clear(); renderSources(); render(); })
       .on('click', '#logsm-src-none', function () {
           knownSources.forEach(s => excluded.add(s.src)); renderSources(); render();
       });
}

function passes(e) {
    const lv = e.level === 'CRITICAL' ? 'ERROR' : e.level;
    if (!levelsOn.has(lv)) return false;
    if (excluded.has(e.src)) return false;
    if (query && !(e.src.toLowerCase().includes(query) || e.msg.toLowerCase().includes(query))) return false;
    return true;
}

function lineHtml(e) {
    const lv = (e.level === 'CRITICAL' ? 'ERROR' : e.level).toLowerCase();
    return `<span class="logsm-line logsm-l-${lv}">${fmtTs(e.ts)} `
         + `<span class="logsm-badge logsm-badge-${lv}">${LEVEL_BADGE[e.level] || 'I'}</span> `
         + `<span class="logsm-src" style="color:${srcColor(e.src)}">${esc(e.src)}</span> `
         + `${esc(e.msg)}</span>\n`;
}

/** Full re-render from the client buffer (filter change / clear / resume). */
function render() {
    if (paused) return;
    const el = document.getElementById('logsm-stream');
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    el.innerHTML = entries.filter(passes).map(lineHtml).join('');
    if (atBottom) el.scrollTop = el.scrollHeight;
}

/** Incremental append (normal poll tick). */
function append(newEntries) {
    if (!newEntries.length) return;
    entries = entries.concat(newEntries);
    if (entries.length > MAX_CLIENT_ENTRIES) entries = entries.slice(-MAX_CLIENT_ENTRIES);
    if (paused) return;
    const el = document.getElementById('logsm-stream');
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    const html = newEntries.filter(passes).map(lineHtml).join('');
    if (html) el.insertAdjacentHTML('beforeend', html);
    if (atBottom) el.scrollTop = el.scrollHeight;
}

let srcRefreshCountdown = 0;

function poll() {
    $.getJSON(`/api/logs/tail?after=${cursor}&limit=800`)
        .done(function (d) {
            append(d.entries || []);
            cursor = d.cursor ?? cursor;
            $('#logsm-state').text(`live · ${entries.length} buffered`);
        })
        .fail(function () { $('#logsm-state').text('backend unreachable — retrying'); });
    // Refresh the source list every ~10 ticks (new instances appear as they log).
    if (--srcRefreshCountdown <= 0) {
        srcRefreshCountdown = 10;
        $.getJSON('/api/logs/sources').done(function (d) {
            const next = d.sources || [];
            if (JSON.stringify(next) !== JSON.stringify(knownSources)) {
                knownSources = next;
                renderSources();
            }
        });
    }
}

function open() {
    buildDom();
    $('#logs-modal').show();
    if (!timer) {
        srcRefreshCountdown = 0;   // force an immediate sources fetch
        poll();
        timer = setInterval(poll, POLL_MS);
    }
}

function close() {
    $('#logs-modal').hide();
    if (timer) { clearInterval(timer); timer = null; }  // no polling while closed
}

// Navbar wiring — the button lives in base.html on every page.
$(function () {
    $('#nav-logs').on('click', function (e) { e.preventDefault(); open(); });
});
