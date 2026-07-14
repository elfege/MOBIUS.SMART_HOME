/**
 * tiles/core/panel-api.ts — the tiles app's typed endpoints on the shared
 * transport. This is the tiles DATA LAYER, kept in the tiles lane (not shared/)
 * so it is not gated on a cross-app ack.
 *
 * Talks to /api/panel/* (apps/tiles_api) with the enrolled-device Bearer token
 * that shared/transport injects. Command dispatch delegates, server-side, to the
 * shared device commander (Matter-first-then-Hubitat fallback, Matter-only by
 * construction) — the client just names the device + command.
 */

import { Transport } from '../../shared/transport';
import type { PanelRoster } from './panel-types';

export class PanelApi {
  private readonly t: Transport;
  constructor(baseUrl = '') {
    this.t = new Transport(baseUrl);
  }

  /** The resolved roster: ordered sections + resolved tiles. */
  roster(profile = 'default'): Promise<PanelRoster> {
    return this.t.get<PanelRoster>(
      `/api/panel/devices?profile=${encodeURIComponent(profile)}`,
    );
  }

  /** Trusted-LAN auto-enrollment: mint + return this tablet's own panel token
   *  (the wall-tablet zero-touch path). Throws (403) off-LAN. */
  bootstrap(): Promise<{ token: string; id: number; scopes: string[] }> {
    return this.t.post('/api/panel/session/bootstrap');
  }

  /** Confirm this enrolled panel's identity + scopes (used to validate a token). */
  whoami(): Promise<{ id: number; name: string; kind: string; scopes: string[] }> {
    return this.t.get('/api/panel/whoami');
  }

  /** Send a device command (requires panel:command). value is optional:
   *  a bare scalar (e.g. setLevel -> 75) OR a map (e.g. setColor ->
   *  {hue,saturation,level}, Hubitat 0-100 scale). The backend wraps it into
   *  the commander's arg list; the Matter client translates the map natively. */
  command(
    deviceId: number,
    command: string,
    value?: string | number | Record<string, number>,
  ): Promise<{ message: string; verified: boolean; status: string }> {
    return this.t.post(`/api/panel/devices/${deviceId}/command`, {
      command,
      ...(value !== undefined ? { value } : {}),
    });
  }
}
