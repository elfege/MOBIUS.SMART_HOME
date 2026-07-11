/**
 * restart-app.js — trigger a full host-side application restart (start.sh)
 * via POST /api/restart, then show a blocking overlay that polls /api/health
 * until the stack is back and reloads the page.
 *
 * Backed by the trigger-file + systemd-watcher flow (canonical STANDARD
 * RESTART.1-4, mirrors NVR + TILES): the container can't run start.sh itself,
 * so POST /api/restart writes a tmpfs trigger the host watcher picks up. A full
 * restart (docker compose up + warm-up) takes ~30-60s.
 *
 * Reused by:
 *   - the navbar "⟳ Restart" link (templates/base.html)
 *   - deletion flows that enforce a restart after removing a hub / instance
 *     (call with {confirmed:true} to skip the generic prompt — the caller has
 *     already asked its own tailored question).
 */

const OVERLAY_ID = 'app-restart-overlay';

/**
 * Show (or update) the full-screen restart overlay with a spinner + message.
 * Idempotent — reuses the node across calls so the message can be updated
 * as the restart progresses.
 */
function showOverlay(message) {
    let el = document.getElementById(OVERLAY_ID);
    if (!el) {
        el = document.createElement('div');
        el.id = OVERLAY_ID;
        el.style.cssText = [
            'position:fixed', 'inset:0', 'z-index:99999',
            'display:flex', 'flex-direction:column',
            'align-items:center', 'justify-content:center',
            'gap:1.2rem', 'background:rgba(10,12,16,0.92)',
            'color:#e8eaed', 'font-family:system-ui,sans-serif',
            'text-align:center', 'padding:2rem',
        ].join(';');
        el.innerHTML = `
            <div style="width:48px;height:48px;border:4px solid #444;border-top-color:#4a9eff;border-radius:50%;animation:app-restart-spin 0.9s linear infinite;"></div>
            <div id="${OVERLAY_ID}-msg" style="font-size:1.1rem;max-width:34rem;line-height:1.5;"></div>
            <style>@keyframes app-restart-spin{to{transform:rotate(360deg)}}</style>`;
        document.body.appendChild(el);
    }
    el.querySelector(`#${OVERLAY_ID}-msg`).textContent = message;
    el.style.display = 'flex';
}

function hideOverlay() {
    const el = document.getElementById(OVERLAY_ID);
    if (el) el.style.display = 'none';
}

/**
 * Poll /api/health until the app has gone DOWN and then come back UP, then
 * reload. Requiring a confirmed down-then-up transition avoids a false
 * "already back" reload if the poll races the container teardown.
 */
async function waitForHealthThenReload() {
    const start = Date.now();
    const DEADLINE_MS = 180_000;   // 3 min, then stop hanging and tell the user
    let downSeen = false;
    while (Date.now() - start < DEADLINE_MS) {
        await new Promise(r => setTimeout(r, 3000));
        let ok = false;
        try {
            ok = (await fetch('/api/health', { cache: 'no-store' })).ok;
        } catch (e) {
            ok = false;
        }
        if (!ok) {
            downSeen = true;
            showOverlay('Restarting — the application went down. Waiting for it to come back…');
        } else if (downSeen) {
            showOverlay('Back online — reloading…');
            await new Promise(r => setTimeout(r, 800));
            window.location.reload();
            return;
        }
    }
    showOverlay('Restart is taking longer than expected. Reload the page manually once the app is back.');
}

/**
 * Trigger a full application restart.
 *
 * @param {string} reason  Logged server-side (shows up in app logs).
 * @param {{confirmed?: boolean}} opts  confirmed=true skips the generic
 *        confirm() prompt (caller already asked its own question).
 * @returns {Promise<boolean>} true if a restart was initiated.
 */
export async function triggerAppRestart(reason = 'UI requested restart', { confirmed = false } = {}) {
    if (!confirmed) {
        const ok = confirm(
            'Restart the MOBIUS.SMART_HOME application?\n\n' +
            'The whole stack (app, DB, PostgREST, nginx, Matter) restarts via ' +
            'start.sh. The UI is unavailable for ~30-60s and reconnects itself.'
        );
        if (!ok) return false;
    }
    showOverlay('Requesting restart…');
    try {
        const res = await fetch('/api/restart', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason }),
        });
        if (res.status === 503) {
            hideOverlay();
            alert(
                'Restart watcher not installed yet.\n\n' +
                'Run  ./start.sh  once on the host (<HOST>) to install the ' +
                'restart watcher + trigger mount, then this button will work.'
            );
            return false;
        }
        if (!res.ok) {
            throw new Error(`${res.status}: ${await res.text()}`);
        }
    } catch (e) {
        hideOverlay();
        alert('Restart request failed: ' + e.message);
        return false;
    }
    showOverlay('Restart initiated — waiting for the app to go down and come back…');
    waitForHealthThenReload();
    return true;
}
