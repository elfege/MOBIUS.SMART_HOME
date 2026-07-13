/**
 * admin/components/StatusPill.tsx — the instance lifecycle pill.
 *
 * CVD-CRITICAL: state is NEVER carried by hue alone (shared/tokens.ts rule).
 * Each state gets THREE redundant signals — a distinct GLYPH shape, the state
 * NAME in text, and a luminance-contrasted surface:
 *   running  -> "● running"  filled dot, active (bright blue-shifted) surface
 *   paused   -> "◌ paused"   hollow dot, plain surface
 *   disabled -> "■ disabled" square, faint text on plain surface
 * A red/green-deficient viewer separates all three with zero hue information.
 */

import { StyleSheet, Text, View } from 'react-native';

import { colors, radius } from '../../shared/tokens';
import type { InstanceState } from '../core/admin-types';

const GLYPH: Record<InstanceState, string> = {
  running: '●', // ● filled circle
  paused: '◌', // ◌ dotted/hollow circle
  disabled: '■', // ■ filled square
};

export function StatusPill({ state }: { state: InstanceState }) {
  const active = state === 'running';
  return (
    <View style={[styles.pill, active ? styles.pillActive : null]}>
      <Text style={[styles.text, active ? styles.textActive : null, state === 'disabled' ? styles.textFaint : null]}>
        {GLYPH[state]} {state}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  pill: {
    backgroundColor: colors.surface,
    borderRadius: radius.chip,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    paddingHorizontal: 10,
    paddingVertical: 4,
    alignSelf: 'flex-start',
  },
  pillActive: {
    backgroundColor: colors.surfaceActive,
    borderColor: colors.borderActive,
  },
  text: { color: colors.textDim, fontSize: 12, fontWeight: '600' },
  textActive: { color: colors.textOnActive },
  textFaint: { color: colors.textFaint },
});
