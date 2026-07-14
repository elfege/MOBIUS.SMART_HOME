/**
 * tiles/components/DeviceTile.tsx — ONE tile, rendered from the server-resolved
 * tile_type + primary_value (no client-side capability logic).
 *
 * CVD: "active" is signalled THREE redundant ways — a brighter blue-shifted
 * surface (luminance), a filled vs hollow shape (●/○), and the value text —
 * never by hue alone. Colours come only from shared/tokens (no yellow/green).
 */
import { memo } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import type { Tile } from '../core/panel-types';

const ACTIVE_VALUES = new Set([
  'on', 'open', 'unlocked', 'active', 'wet', 'present', 'detected',
]);

/** Is this tile in its "on/active" visual state? */
function isActive(t: Tile): boolean {
  return ACTIVE_VALUES.has((t.primary_value ?? '').toLowerCase());
}

/** Can the operator tap this tile to toggle it? Display-only sensors cannot. */
function canToggle(t: Tile): boolean {
  return t.is_actionable
    && (t.tile_type === 'switch' || t.tile_type === 'dimmer' || t.tile_type === 'color');
}

/** Does this tile open the dimmer/RGB modal on long-press? */
function isDimmable(t: Tile): boolean {
  return t.tile_type === 'dimmer' || t.tile_type === 'color';
}

/** Short state line under the label. */
function stateLine(t: Tile): string {
  const v = t.primary_value ?? '—';
  switch (t.tile_type) {
    case 'dimmer':
    case 'color': {
      // Level% ONLY when the switch is on AND level>0 (the "bunk-bed rule":
      // a bulb reporting level=98 while switch=off must read OFF, and
      // switch=on + level=0 also reads OFF). Verbatim from the original.
      const on = (t.primary_value ?? 'off').toLowerCase() === 'on';
      const lvl = Number(t.attributes['level'] ?? 0);
      return on && lvl > 0 ? `${lvl}%` : 'OFF';
    }
    case 'thermostat':
      return `${t.attributes['temperature'] ?? v}°`;
    default:
      return v;
  }
}

export const DeviceTile = memo(function DeviceTile({
  tile,
  onToggle,
  onOpen,
}: {
  tile: Tile;
  onToggle: (t: Tile) => void;
  /** Long-press handler — opens the dimmer/RGB modal for dimmable tiles. */
  onOpen: (t: Tile) => void;
}) {
  const active = isActive(tile);
  const body = (
    <View style={[styles.tile, active && styles.tileActive]}>
      <Text style={[styles.label, active && styles.textOnActive]} numberOfLines={2}>
        {tile.label}
      </Text>
      <Text style={[styles.state, active && styles.textOnActive]} numberOfLines={1}>
        {stateLine(tile)}
      </Text>
      <View style={styles.footer}>
        <Text style={[styles.kind, active && styles.kindOnActive]}>{tile.tile_type}</Text>
        {tile.is_actionable ? (
          // Redundant NON-colour state signal: filled = on, hollow = off.
          <Text style={[styles.shape, active && styles.textOnActive]}>
            {active ? '●' : '○'}
          </Text>
        ) : null}
      </View>
    </View>
  );
  if (!canToggle(tile)) return body;
  // Dimmable tiles: short-tap toggles, long-press opens the dimmer/RGB modal
  // (500ms, matching the original). Non-dimmable switch tiles: tap only.
  return (
    <Pressable
      onPress={() => onToggle(tile)}
      onLongPress={isDimmable(tile) ? () => onOpen(tile) : undefined}
      delayLongPress={500}
    >
      {body}
    </Pressable>
  );
});

const styles = StyleSheet.create({
  tile: {
    width: 160,
    minHeight: 110,
    borderRadius: radius.tile,
    backgroundColor: colors.surface,
    padding: 14,
    justifyContent: 'space-between',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
  },
  tileActive: {
    backgroundColor: colors.surfaceActive,
    borderColor: colors.borderActive,
    borderWidth: 1.5,
  },
  label: { color: colors.text, fontSize: 15, fontWeight: '600' },
  state: { color: colors.textDim, fontSize: 13, marginTop: space.sm },
  footer: {
    marginTop: space.sm,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  kind: {
    color: colors.textFaint,
    fontSize: 10,
    textTransform: 'uppercase',
    letterSpacing: 1,
  },
  shape: { color: colors.accent, fontSize: 12 },
  textOnActive: { color: colors.textOnActive },
  kindOnActive: { color: colors.textDim },
});
