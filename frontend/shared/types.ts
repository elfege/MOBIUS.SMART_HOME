/**
 * shared/types.ts — generic domain primitives shared by BOTH RN apps.
 *
 * PURE TypeScript, no react/react-native imports. Keep this SMALL and truly
 * cross-app: backend-contract shapes both tiles and admin consume. App-specific
 * shapes (the resolved panel Tile/Section, admin's instance shapes) live in each
 * app's own core/, not here.
 */

/** A device attribute-change frame as the /ws/dashboard broadcast emits it
 *  (services/dashboard_broadcaster.py). NOTE: `device_id` is the Hubitat per-hub
 *  id, which is ambiguous across a multi-hub fleet — routing it to a canonical
 *  panel tile needs the backend to also emit the canonical id, so the tiles proto
 *  polls the roster instead of consuming this (see shared/ws.ts). */
export interface DeviceEventFrame {
  type: 'device_event';
  device_id: string;
  device_name?: string;
  event_name: string;
  event_value: string;
}

/** A location mode (GET /api/modes). */
export interface Mode {
  id: number;
  name: string;
  active: boolean;
}
