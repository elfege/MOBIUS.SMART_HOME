/**
 * Hubs controller — load/edit/save/delete hub_config rows.
 *
 * Endpoints:
 *   GET    /api/hubs           → list
 *   PATCH  /api/hubs/{id}      → partial update + cache invalidation
 *   POST   /api/hubs           → create
 *   DELETE /api/hubs/{id}      → delete (refused if devices reference it)
 */

import { openDeviceRefreshModal } from '../components/device-refresh-modal.js';

const $ = window.jQuery || window.$;

async function fetchJSON(url, opts = {}) {
    const res = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
    });
    if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body}`);
    }
    if (res.status === 204) return null;
    return res.json();
}

function readForm($form) {
    const data = {};
    $form.find('input').each(function () {
        const $i = $(this);
        const name = $i.attr('name');
        if (!name) return;
        if ($i.attr('type') === 'checkbox') {
            data[name] = $i.is(':checked');
        } else if ($i.attr('type') === 'number') {
            const v = $i.val();
            // Empty number input → null (not 0, not '') so PostgREST leaves
            // the column as NULL.
            data[name] = (v === '' || v === null) ? null : parseInt(v, 10);
        } else if ($i.attr('type') === 'password') {
            const v = $i.val();
            // Empty password field → don't submit at all (preserves existing
            // value in DB). User who wants to CLEAR password sends NULL via
            // a small UX affordance later; for now leaving blank is no-op.
            if (v !== '') data[name] = v;
        } else {
            const v = $i.val();
            // Empty string → null for optional plaintext fields so we don't
            // overwrite a valid value with empty.
            if (v !== '' || name === 'hub_name' || name === 'hub_ip'
                || name === 'maker_api_app_number'
                || name === 'maker_api_token_env') {
                data[name] = v;
            }
        }
    });
    return data;
}

function fillForm($form, hub) {
    $form.attr('data-hub-id', hub.id ?? '');
    $form.find('input[name=hub_name]').val(hub.hub_name ?? '');
    $form.find('input[name=hub_ip]').val(hub.hub_ip ?? '');
    $form.find('input[name=maker_api_app_number]').val(hub.maker_api_app_number ?? '');
    $form.find('input[name=maker_api_token_env]').val(hub.maker_api_token_env ?? '');
    $form.find('input[name=is_primary]').prop('checked', !!hub.is_primary);
    $form.find('input[name=is_enabled]').prop('checked', hub.is_enabled !== false);
    $form.find('input[name=admin_username]').val(hub.admin_username ?? '');
    // NEVER prefill password — keep server-side value hidden. Blank field
    // means "no change" (per readForm logic above).
    $form.find('input[name=admin_password]').val('').attr(
        'placeholder',
        hub.admin_password ? '(unchanged — type to update)' : '(unauthenticated)'
    );
    $form.find('input[name=admin_creds_index]').val(hub.admin_creds_index ?? '');
}

function setStatus($form, msg, isError) {
    $form.find('.hub-card-status')
        .text(msg)
        .css('color', isError ? '#e57373' : '#7ec27e');
}

/**
 * Populate the admin-API contract-drift health line on a hub card from a
 * hub_health row (see services/hub_contract_watch.py). Shows firmware version
 * and a green/red/amber badge for the runmethod command path.
 */
function applyHealth($form, health) {
    const $fw = $form.find('.hub-health-firmware');
    const $badge = $form.find('.hub-health-badge');
    const $checked = $form.find('.hub-health-checked');

    if (!health) {
        $fw.text('');
        $badge.text('').css({ background: '', color: '' });
        $checked.text('');
        return;
    }

    $fw.text(health.platform_version ? `Firmware ${health.platform_version}` : 'Firmware —');

    const ok = health.command_path_ok;
    const contract = health.command_path_contract;
    let label, bg, fg;
    if (ok === true && contract === 'form') {
        label = 'Commands OK (legacy form contract)'; bg = '#5a4a1e'; fg = '#f0d97a';
    } else if (ok === true) {
        label = 'Commands OK'; bg = '#1e4a2e'; fg = '#7ec27e';
    } else if (ok === false) {
        label = `Commands BROKEN (HTTP path rejected)`; bg = '#5a1e1e'; fg = '#e57373';
    } else {
        label = 'Command path not yet checked'; bg = '#333'; fg = '#aaa';
    }
    $badge.text(label).css({ background: bg, color: fg });
    if (ok === false && health.command_path_error) {
        $badge.attr('title', health.command_path_error);
    } else {
        $badge.removeAttr('title');
    }

    if (health.command_path_checked_at) {
        const d = new Date(health.command_path_checked_at);
        $checked.text(`checked ${d.toLocaleString()}`);
    } else {
        $checked.text('');
    }
}

function renderHub(hub, health) {
    const tpl = document.getElementById('hub-card-template');
    const node = tpl.content.firstElementChild.cloneNode(true);
    const $form = $(node);
    fillForm($form, hub);
    applyHealth($form, health);

    $form.on('submit', async function (e) {
        e.preventDefault();
        const id = $form.attr('data-hub-id');
        const body = readForm($form);
        try {
            setStatus($form, 'Saving…');
            if (id) {
                await fetchJSON(`/api/hubs/${id}`, {
                    method: 'PATCH',
                    body: JSON.stringify(body),
                });
                setStatus($form, 'Saved');
            } else {
                const created = await fetchJSON('/api/hubs', {
                    method: 'POST',
                    body: JSON.stringify(body),
                });
                const row = Array.isArray(created) ? created[0] : created;
                if (row?.id) $form.attr('data-hub-id', row.id);
                setStatus($form, 'Created');
            }
        } catch (err) {
            setStatus($form, err.message, true);
        }
    });

    $form.find('.btn-delete').on('click', async function () {
        const id = $form.attr('data-hub-id');
        if (!id) {
            $form.remove();
            return;
        }
        if (!confirm(`Delete hub "${$form.find('input[name=hub_name]').val()}"?`)) return;
        try {
            setStatus($form, 'Deleting…');
            await fetchJSON(`/api/hubs/${id}`, { method: 'DELETE' });
            $form.remove();
        } catch (err) {
            setStatus($form, err.message, true);
        }
    });

    $form.find('.btn-reboot').on('click', async function () {
        const ip = $form.find('input[name=hub_ip]').val();
        const name = $form.find('input[name=hub_name]').val() || ip;
        if (!ip) return;
        if (!confirm(
            `Reboot hub "${name}" (${ip})?\n\n` +
            `The hub goes OFFLINE for ~2-3 minutes and all its automations pause. ` +
            `Use this to try reviving a dead Matter bridge / eventsocket.`
        )) return;
        try {
            setStatus($form, 'Rebooting…');
            const res = await fetchJSON(`/api/hubs/${ip}/reboot`, { method: 'POST' });
            setStatus($form, res.reboot_initiated
                ? 'Reboot initiated — hub offline ~2-3 min'
                : 'Reboot request sent');
        } catch (err) {
            setStatus($form, 'Reboot failed: ' + err.message, true);
        }
    });

    return node;
}

async function loadAll() {
    const $list = $('#hubs-list').empty();
    const $status = $('#hubs-status');
    try {
        $status.text('Loading…');
        const hubs = await fetchJSON('/api/hubs');
        // Merge per-hub admin-API contract-drift health (best-effort; the
        // hub list still renders if the health endpoint is unavailable).
        let healthById = {};
        try {
            const health = await fetchJSON('/api/hubs/health');
            for (const row of health) healthById[row.hub_id] = row;
        } catch (e) {
            // non-fatal — cards just render without the health line
        }
        $status.text(`${hubs.length} hub${hubs.length === 1 ? '' : 's'} configured`);
        for (const h of hubs) {
            $list.append(renderHub(h, healthById[h.id]));
        }
    } catch (err) {
        $status.text(`Failed to load: ${err.message}`).css('color', '#e57373');
    }
}

$(function () {
    loadAll();
    $('#btn-add-hub').on('click', function () {
        $('#hubs-list').prepend(renderHub({
            hub_name: '', hub_ip: '', maker_api_app_number: '',
            maker_api_token_env: '', is_primary: false, is_enabled: true,
        }));
    });
    $('#btn-reboot-all-hubs').on('click', async function () {
        if (!confirm(
            'Reboot ALL enabled hubs?\n\n' +
            'Every hub goes OFFLINE for ~2-3 minutes and all automations pause. ' +
            'Use this when the Matter bridges / eventsockets are dead across the board.'
        )) return;
        const $btn = $(this).prop('disabled', true).text('⟳ Rebooting all…');
        try {
            const res = await fetchJSON('/api/hubs/reboot-all', { method: 'POST' });
            const ok = (res.results || []).filter(r => r.reboot_initiated).length;
            $btn.text(`⟳ Rebooted ${ok}/${res.count} — offline ~2-3 min`);
        } catch (err) {
            $btn.text('⟳ Reboot all failed').prop('disabled', false);
        }
    });
    // ↻ device-cache refresh — pops the modal (operator types device # or
    // 0 for all). Lives next to the hubs panel because hub-side driver
    // changes are the canonical reason you'd want to invalidate one row.
    $('#btn-refresh-device-cache').on('click', function () {
        openDeviceRefreshModal();
    });
});
