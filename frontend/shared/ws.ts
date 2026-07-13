/**
 * shared/ws.ts — the GENERIC WebSocket client both apps build on, replacing the
 * TILES seed's SSE (MOBIUS.HOME broadcasts over a WebSocket, not SSE).
 *
 * Connects to /ws/dashboard (services/dashboard_broadcaster.py) and surfaces
 * each device_event frame plus a connection status. Web uses the built-in
 * WebSocket (present in react-native-web and RN).
 *
 * ROUTING CAVEAT — why the tiles PROTO does not use this yet: the broadcast keys
 * events by the Hubitat per-hub `device_id`, which is ambiguous across a
 * multi-hub fleet, while the panel roster keys tiles by the canonical id. Wiring
 * this correctly needs the backend to also emit the canonical id (a coordinated
 * backend change, my lane). Until then the tiles app polls the roster. This
 * client is kept ready for admin + the eventual tiles live-update wiring.
 */

import type { DeviceEventFrame } from './types';

export type StreamStatus = 'connecting' | 'live' | 'lost';

export interface WsHandlers {
  onEvent: (ev: DeviceEventFrame) => void;
  onStatus?: (status: StreamStatus) => void;
}

/** Build the ws:// or wss:// URL for /ws/dashboard from the current origin
 *  (or an injected baseUrl for native). */
function dashboardUrl(baseUrl: string): string {
  if (baseUrl) {
    return `${baseUrl.replace(/^http/, 'ws')}/ws/dashboard`;
  }
  // Same-origin web build.
  const loc = typeof location !== 'undefined' ? location : null;
  const scheme = loc && loc.protocol === 'https:' ? 'wss' : 'ws';
  const host = loc ? loc.host : 'localhost';
  return `${scheme}://${host}/ws/dashboard`;
}

/** Subscribe to the dashboard broadcast. Returns an unsubscribe fn. */
export function subscribeDashboard(
  baseUrl: string,
  handlers: WsHandlers,
): () => void {
  handlers.onStatus?.('connecting');
  const ws = new WebSocket(dashboardUrl(baseUrl));

  ws.onopen = () => handlers.onStatus?.('live');
  ws.onclose = () => handlers.onStatus?.('lost');
  ws.onerror = () => handlers.onStatus?.('lost');
  ws.onmessage = (msg: MessageEvent) => {
    let data: unknown;
    try {
      data = JSON.parse(String(msg.data));
    } catch {
      return; // malformed frame — ignore, never crash the stream
    }
    if (
      data && typeof data === 'object' &&
      (data as { type?: unknown }).type === 'device_event'
    ) {
      handlers.onEvent(data as DeviceEventFrame);
    }
  };

  return () => ws.close();
}
