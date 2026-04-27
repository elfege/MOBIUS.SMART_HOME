/**
 * Hubs controller — load/edit/save/delete hub_config rows.
 *
 * Endpoints:
 *   GET    /api/hubs           → list
 *   PATCH  /api/hubs/{id}      → partial update + cache invalidation
 *   POST   /api/hubs           → create
 *   DELETE /api/hubs/{id}      → delete (refused if devices reference it)
 */

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
        } else {
            data[name] = $i.val();
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
}

function setStatus($form, msg, isError) {
    $form.find('.hub-card-status')
        .text(msg)
        .css('color', isError ? '#e57373' : '#7ec27e');
}

function renderHub(hub) {
    const tpl = document.getElementById('hub-card-template');
    const node = tpl.content.firstElementChild.cloneNode(true);
    const $form = $(node);
    fillForm($form, hub);

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

    return node;
}

async function loadAll() {
    const $list = $('#hubs-list').empty();
    const $status = $('#hubs-status');
    try {
        $status.text('Loading…');
        const hubs = await fetchJSON('/api/hubs');
        $status.text(`${hubs.length} hub${hubs.length === 1 ? '' : 's'} configured`);
        for (const h of hubs) {
            $list.append(renderHub(h));
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
});
