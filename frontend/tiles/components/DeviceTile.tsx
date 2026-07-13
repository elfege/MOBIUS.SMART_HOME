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

/** Short state line under the label. */
function stateLine(t: Tile): string {
  const v = t.primary_value ?? '—';
  switch (t.tile_type) {
    case 'dimmer':
      return v === 'on' ? `${t.attributes['level'] ?? '?'}%` : 'off';
    case 'thermostat':
      return `${t.attributes['temperature'] ?? v}°`;
    default:
      return v;
  }
}

export const DeviceTile = memo(function DeviceTile({
  tile,
  onToggle,
}: {
  tile: Tile;
  onToggle: (t: Tile) => void;
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
  return canToggle(tile) ? <Pressable onPress={() => onToggle(tile)}>{body}</Pressable> : body;
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
