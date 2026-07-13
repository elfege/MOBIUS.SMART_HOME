/**
 * shared/tokens.ts — the ONE source of colour, radius, and spacing for BOTH RN
 * apps (frontend/tiles + frontend/admin). Owned by the assistant per the
 * parallel-agent methodology; the tiles app consumes it.
 *
 * CVD-CRITICAL — CVD-safe accessibility, and this is the file where that
 * constraint is enforced for the whole UI. The load-bearing rule is NOT a
 * banned-colour list; it is:
 *
 *   Never let HUE ALONE carry a state distinction. Lean on LUMINANCE contrast
 *   plus shape/label. A single colour in isolation, with good luminance contrast
 *   and a redundant shape/text signal, is fine regardless of hue.
 *
 * The salvaged TILES seed distinguished an "active" tile purely by turning it
 * YELLOW (#f5d76e) — hue-only, and a hue a red/green-deficient viewer cannot
 * separate. That dies here. "Active" is now carried by a brighter, blue-shifted
 * SURFACE (luminance) + a high-luminance blue border + dark-on-light text, and
 * the tile ALSO states its value in text ("on"/"72%"), so the signal survives
 * with zero reliance on hue discrimination.
 */

export const colors = {
  // Base surfaces (dark theme).
  bg: '#0d0d12',
  surface: '#1c1c22',
  border: '#2c2c34',

  // "Active/on" state — LUMINANCE + blue accent, never yellow.
  surfaceActive: '#2a3f66', // brighter, blue-shifted: reads "on" by lightness
  borderActive: '#89b4fa',  // high-luminance blue accent ring

  // Text.
  text: '#ffffff',
  textDim: '#c9c9d1',
  textFaint: '#6e6e78',
  textOnActive: '#ffffff', // white on the lighter blue active surface = high contrast

  // Accents — CVD-safe. Blue is the primary signal colour; red is used ONLY
  // alongside a shape/label, never as the sole differentiator.
  accent: '#89b4fa',       // vetted high-luminance blue on dark bg
  danger: '#ff6b6b',

  // Connection status — carried by blue/red PLUS a distinct shape in the UI
  // (filled vs hollow), never hue alone.
  live: '#89b4fa',
  lost: '#ff6b6b',
  connecting: '#8e8e99',
} as const;

export const radius = { tile: 18, chip: 14, modal: 20 } as const;
export const space = { xs: 4, sm: 8, md: 12, lg: 20 } as const;

export type ColorToken = keyof typeof colors;
