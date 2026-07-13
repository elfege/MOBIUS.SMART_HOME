/**
 * admin/core/nav.ts — hand-rolled navigation state (zustand).
 *
 * Deliberately NOT react-navigation: dependencies are frozen while
 * frontend/node_modules is the temporary TILES symlink (the npm-workspaces
 * step is a coordinated change). Two views suffice for now; this grows into
 * a real navigator only if/when the workspaces step lands new deps.
 */

import { create } from 'zustand';

export type NavView =
  | { view: 'list' }
  | { view: 'detail'; instanceId: number };

interface NavState {
  current: NavView;
  openInstance: (instanceId: number) => void;
  backToList: () => void;
}

export const useNav = create<NavState>((set) => ({
  current: { view: 'list' },
  openInstance: (instanceId) => set({ current: { view: 'detail', instanceId } }),
  backToList: () => set({ current: { view: 'list' } }),
}));
