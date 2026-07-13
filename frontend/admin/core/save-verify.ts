/**
 * admin/core/save-verify.ts — settings save with COMMIT VERIFICATION.
 *
 * RULE 9.1.4 (the 12:04 incident, encoded): a dropped connection is NOT a
 * failed write. On 2026-07-13 the operator's phone save committed (HTTP 200 in
 * the DB) but the in-flight response died in a container restart; the old UI
 * reported "Failed to update" on a COMMITTED write and he concluded the app
 * was broken. This module makes that lie impossible in the RN admin app:
 *
 *   PUT succeeds            -> saved.
 *   PUT fails with HTTP 4xx/5xx -> genuinely rejected; report failure.
 *   PUT fails as NETWORK/abort  -> UNKNOWN — the write may have committed.
 *       Re-fetch the instance and compare the edited keys against the server
 *       state: all match -> the write landed, report saved (recovered);
 *       mismatch/unreachable -> report honestly as unverified, never as a
 *       definite failure.
 */

import { TransportError } from '../../shared/transport';
import type { AdminApi } from './admin-api';
import type { InstanceRow } from './admin-types';

export type SaveOutcome =
  | { result: 'saved'; recovered: boolean; row: InstanceRow | null }
  | { result: 'rejected'; status: number }
  | { result: 'unverified'; detail: string };

/** Deep-enough equality for JSONB setting values (primitives, arrays, plain
 *  objects — the only shapes settings hold). */
function valueEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Save `edits` onto an instance (server merges), verifying commit on a
 *  network-level failure before ever reporting a loss. */
export async function saveSettings(
  api: AdminApi,
  instanceId: number,
  edits: Record<string, unknown>,
): Promise<SaveOutcome> {
  try {
    await api.update(instanceId, edits);
    // Clean success — refresh the row so the caller renders server truth.
    const row = await api.instance(instanceId).catch(() => null);
    return { result: 'saved', recovered: false, row };
  } catch (e) {
    if (e instanceof TransportError && !e.isNetwork) {
      // Real HTTP rejection — the server said no.
      return { result: 'rejected', status: e.status };
    }
    // Network/abort: the write may have committed. Verify before claiming loss.
    try {
      const row = await api.instance(instanceId);
      const allLanded = Object.entries(edits).every(([k, v]) =>
        valueEqual(row.settings[k], v),
      );
      if (allLanded) {
        return { result: 'saved', recovered: true, row };
      }
      return {
        result: 'unverified',
        detail: 'connection dropped and the server does not show the change — retry',
      };
    } catch {
      return {
        result: 'unverified',
        detail: 'connection dropped; could not re-check the server — it may have saved',
      };
    }
  }
}
