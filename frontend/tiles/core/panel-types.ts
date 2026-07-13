/**
 * tiles/core/panel-types.ts — the RESOLVED panel shapes from GET /api/panel/devices.
 *
 * Tiles-specific (not shared/): only the tiles app renders these. Everything the
 * renderer needs is precomputed SERVER-SIDE from the panel_* tables (migration
 * 014) — tile_type, section, primary value — so the client renders verbatim and
 * carries no capability/room logic of its own.
 */

export type AttrMap = Record<string, string>;

/** One resolved tile from GET /api/panel/devices. */
export interface Tile {
  /** Canonical device id — the ONLY key the client sends back on a command. */
  id: number;
  label: string;
  device_type: string | null;
  capabilities: string[];
  attributes: AttrMap;
  protocol: string | null;
  /** Renderer key, resolved server-side (color|dimmer|switch|thermostat|...). */
  tile_type: string;
  /** False = display-only sensor: render no control surface. */
  is_actionable: boolean;
  primary_attribute: string | null;
  primary_value: string | null;
  section_slug: string;
  sort_order: number;
  is_favorite: boolean;
  is_hidden: boolean;
}

/** One resolved section (room/group), ordered server-side. */
export interface Section {
  slug: string;
  name: string;
  icon: string | null;
  sort_order: number;
}

/** The whole resolved roster. */
export interface PanelRoster {
  profile: string;
  sections: Section[];
  tiles: Tile[];
}
