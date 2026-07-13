/**
 * admin/core/store.ts — the admin client state (zustand).
 *
 * Holds the instance list (keyed by id, ordered as the server returned them),
 * the app-type catalog (keyed by id, for grouping/labels), and a load status.
 * Fed by the paired instances+app-types fetch; refreshed by polling for the
 * proto. Optimistic pause/resume flips is_paused immediately, then a refresh
 * reconciles with the server — same reconcile-by-poll pattern as the tiles
 * store, so a failed command can never leave the UI lying about state
 * (RULE 9.1.4: a dropped connection is NOT a failed write, and equally a
 * locally-flipped flag is NOT a committed one).
 */

import { create } from 'zustand';

import type { AppType, InstanceRow } from './admin-types';

export type AdminStatus = 'loading' | 'ready' | 'error';

interface AdminState {
  instances: Record<number, InstanceRow>;
  order: number[]; // instance ids in server order (created_at desc)
  appTypes: Record<number, AppType>;
  status: AdminStatus;
  /** ids with a pause/resume request in flight (disables their button). */
  busy: Record<number, boolean>;

  setData: (instances: InstanceRow[], appTypes: AppType[]) => void;
  setStatus: (s: AdminStatus) => void;
  /** Optimistically flip an instance's paused flag (before refresh confirms). */
  setPaused: (id: number, paused: boolean) => void;
  setBusy: (id: number, busy: boolean) => void;
  /** Replace one instance row with fresh server truth (post-save refetch). */
  setInstance: (row: InstanceRow) => void;
}

export const useAdminStore = create<AdminState>((set) => ({
  instances: {},
  order: [],
  appTypes: {},
  status: 'loading',
  busy: {},

  setData: (instances, appTypes) =>
    set(() => {
      const byId: Record<number, InstanceRow> = {};
      const order: number[] = [];
      for (const row of instances) {
        byId[row.id] = row;
        order.push(row.id);
      }
      const types: Record<number, AppType> = {};
      for (const t of appTypes) {
        types[t.id] = t;
      }
      return { instances: byId, order, appTypes: types, status: 'ready' as const };
    }),

  setStatus: (status) => set({ status }),

  setPaused: (id, paused) =>
    set((state) => {
      const existing = state.instances[id];
      if (!existing) return state;
      return {
        instances: {
          ...state.instances,
          [id]: { ...existing, is_paused: paused },
        },
      };
    }),

  setBusy: (id, busy) =>
    set((state) => ({ busy: { ...state.busy, [id]: busy } })),

  setInstance: (row) =>
    set((state) => ({
      instances: { ...state.instances, [row.id]: row },
    })),
}));
