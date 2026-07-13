/**
 * tiles/core/store.ts — the tiles client state (zustand).
 *
 * Holds the resolved roster (sections + tiles keyed by canonical id) plus an
 * auth/loading status. Fed by the roster fetch; refreshed by polling for the
 * proto (see the ws routing caveat in shared/ws.ts). Optimistic toggle updates
 * a tile's primary_value immediately, then a refresh reconciles with the server.
 */

import { create } from 'zustand';

import type { Section, Tile } from './panel-types';

export type RosterStatus = 'loading' | 'ready' | 'unauthorized' | 'error';

interface TilesState {
  sections: Section[];
  tiles: Record<number, Tile>;
  order: number[]; // tile ids in server order (sort_order, then label)
  status: RosterStatus;

  setRoster: (sections: Section[], tiles: Tile[]) => void;
  setStatus: (s: RosterStatus) => void;
  /** Optimistically set a tile's primary value (before the refresh confirms). */
  setPrimaryValue: (id: number, value: string) => void;
}

export const useTilesStore = create<TilesState>((set) => ({
  sections: [],
  tiles: {},
  order: [],
  status: 'loading',

  setRoster: (sections, tiles) =>
    set(() => {
      const byId: Record<number, Tile> = {};
      const order: number[] = [];
      for (const t of tiles) {
        byId[t.id] = t;
        order.push(t.id);
      }
      return { sections, tiles: byId, order, status: 'ready' as const };
    }),

  setStatus: (status) => set({ status }),

  setPrimaryValue: (id, value) =>
    set((state) => {
      const existing = state.tiles[id];
      if (!existing) return state;
      return {
        tiles: {
          ...state.tiles,
          [id]: {
            ...existing,
            primary_value: value,
            attributes: existing.primary_attribute
              ? { ...existing.attributes, [existing.primary_attribute]: value }
              : existing.attributes,
          },
        },
      };
    }),
}));
