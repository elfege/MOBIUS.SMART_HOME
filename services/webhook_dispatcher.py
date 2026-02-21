#!/usr/bin/env python3
"""
Webhook Dispatcher Service

Lightweight service that receives webhooks from Hubitat on a dedicated port
and forwards to multiple applications (0_TILES, 0_SMART_HOME, etc.).

This allows a single Maker API webhook URL to fan out to multiple consumers.

Usage:
    python webhook_dispatcher.py

Environment Variables:
    WEBHOOK_PORT: Port to listen on (default: 5050)
    WEBHOOK_TARGETS: Comma-separated list of URLs to forward to

Example:
    WEBHOOK_PORT=5050
    WEBHOOK_TARGETS=http://localhost:80,http://localhost:5001/api/webhook/event
"""

import os
import json
import logging
from flask import Flask, request, jsonify
import requests
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('webhook-dispatcher')

app = Flask(__name__)

# Thread pool for async forwarding
executor = ThreadPoolExecutor(max_workers=10)

def get_targets():
    """Get list of webhook target URLs from environment."""
    targets_str = os.environ.get('WEBHOOK_TARGETS', '')
    if not targets_str:
        return []
    return [url.strip() for url in targets_str.split(',') if url.strip()]


def forward_to_target(url: str, data: dict, headers: dict):
    """Forward webhook to a single target."""
    try:
        response = requests.post(
            url,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=5
        )
        logger.debug(f"Forwarded to {url}: {response.status_code}")
        return True
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout forwarding to {url}")
        return False
    except requests.exceptions.ConnectionError:
        logger.warning(f"Connection error forwarding to {url}")
        return False
    except Exception as e:
        logger.error(f"Error forwarding to {url}: {e}")
        return False


@app.route('/api/webhook/event', methods=['POST'])
def handle_event():
    """
    Receive device event webhook and forward to all targets.

    Hubitat Maker API POSTs here. We forward to all configured targets.
    """
    data = request.get_json(silent=True) or {}
    source_ip = request.remote_addr

    # Debug: log raw payload to see Hubitat format
    logger.info(f"Raw payload from {source_ip}: {data}")

    # Hubitat may nest data in 'content' object
    if 'content' in data:
        data = data['content']

    device_id = data.get('deviceId', 'unknown')
    event_name = data.get('name', 'unknown')
    event_value = data.get('value', '')

    logger.info(f"Event from {source_ip}: device={device_id}, {event_name}={event_value}")

    targets = get_targets()

    if not targets:
        logger.warning("No webhook targets configured!")
        return jsonify({'status': 'warning', 'message': 'No targets configured'}), 200

    # Forward to all targets asynchronously
    futures = []
    for target_url in targets:
        future = executor.submit(forward_to_target, target_url, data, dict(request.headers))
        futures.append((target_url, future))

    # Collect results (don't wait too long)
    results = {}
    for target_url, future in futures:
        try:
            results[target_url] = future.result(timeout=5)
        except Exception:
            results[target_url] = False

    success_count = sum(1 for v in results.values() if v)

    return jsonify({
        'status': 'ok',
        'forwarded_to': success_count,
        'total_targets': len(targets)
    })


@app.route('/api/webhook/mode', methods=['POST'])
def handle_mode():
    """
    Receive mode change webhook and forward to all targets.
    """
    data = request.get_json(silent=True) or {}
    source_ip = request.remote_addr

    mode_name = data.get('value', 'unknown')
    logger.info(f"Mode change from {source_ip}: {mode_name}")

    targets = get_targets()

    # Forward to mode endpoints (append /mode if target doesn't have it)
    for target_url in targets:
        if '/api/webhook/event' in target_url:
            mode_url = target_url.replace('/api/webhook/event', '/api/webhook/mode')
        else:
            mode_url = target_url.rstrip('/') + '/mode'
        executor.submit(forward_to_target, mode_url, data, dict(request.headers))

    return jsonify({'status': 'ok'})


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    targets = get_targets()
    return jsonify({
        'status': 'ok',
        'targets_configured': len(targets),
        'targets': targets
    })


@app.route('/', methods=['GET'])
def index():
    """Simple status page."""
    targets = get_targets()
    return f"""
    <html>
    <head><title>Webhook Dispatcher</title></head>
    <body>
        <h1>Webhook Dispatcher</h1>
        <p>Listening for Hubitat webhooks and forwarding to:</p>
        <ul>
            {''.join(f'<li>{t}</li>' for t in targets) or '<li>No targets configured</li>'}
        </ul>
        <p>Endpoints:</p>
        <ul>
            <li>POST /api/webhook/event - Device events</li>
            <li>POST /api/webhook/mode - Mode changes</li>
            <li>GET /health - Health check</li>
        </ul>
    </body>
    </html>
    """


if __name__ == '__main__':
    port = int(os.environ.get('WEBHOOK_PORT', 5050))

    targets = get_targets()
    logger.info(f"Starting webhook dispatcher on port {port}")
    logger.info(f"Configured targets: {targets}")

    app.run(host='0.0.0.0', port=port, debug=False)
