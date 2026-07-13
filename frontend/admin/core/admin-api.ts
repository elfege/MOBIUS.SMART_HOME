/**
 * admin/core/admin-api.ts — the admin app's typed endpoints on the shared
 * transport. This is the admin DATA LAYER, kept in the admin lane (not
 * shared/) so it is not gated on a cross-app ack — the exact mirror of
 * tiles/core/panel-api.ts.
 *
 * Talks to the instance-management API (/api/instances, /api/app-types) that
 * the existing Jinja/jQuery admin UI uses today — same-origin, served behind
 * nginx. shared/transport injects a Bearer token when one is stored; today
 * these endpoints are LAN-open, so unauthenticated same-origin calls work.
 */

import { Transport } from '../../shared/transport';
import type { AppType, InstanceRow } from './admin-types';

export class AdminApi {
  private readonly t: Transport;
  constructor(baseUrl = '') {
    this.t = new Transport(baseUrl);
  }

  /** All automation instances, newest first (server orders by created_at). */
  instances(): Promise<InstanceRow[]> {
    return this.t.get<InstanceRow[]>('/api/instances');
  }

  /** The app-type catalog (blueprint id -> display name etc.). */
  appTypes(): Promise<AppType[]> {
    return this.t.get<AppType[]>('/api/app-types');
  }

  /** One instance, fresh from the server (used by save verification). */
  instance(instanceId: number): Promise<InstanceRow> {
    return this.t.get<InstanceRow>(`/api/instances/${instanceId}`);
  }

  /** Update an instance's settings. The server MERGES the given keys into the
   *  existing settings JSONB (instance_manager.update_instance), kills the
   *  running instance first and restarts it from the new DB state — so send
   *  only the CHANGED keys. */
  update(
    instanceId: number,
    settings: Record<string, unknown>,
  ): Promise<{ message: string }> {
    return this.t.put(`/api/instances/${instanceId}`, { settings });
  }

  /** Pause an instance. duration_seconds=0 means INDEFINITE (universal pause
   *  contract). The backend converts seconds -> ceil minutes internally. */
  pause(instanceId: number, durationSeconds = 0): Promise<{ message: string }> {
    return this.t.post(`/api/instances/${instanceId}/pause`, {
      duration_seconds: durationSeconds,
    });
  }

  /** Resume a paused instance. */
  resume(instanceId: number): Promise<{ message: string }> {
    return this.t.post(`/api/instances/${instanceId}/resume`);
  }
}
