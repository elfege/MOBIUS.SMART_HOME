/**
 * admin/components/fields/OpaqueField.tsx — arrays/objects/unknown-typed
 * settings, shown READ-ONLY as compact JSON. Honest by design: complex shapes
 * (mode lists, per-mode timeout maps) are never silently hidden, and never
 * editable through a lossy text box — dedicated editors for them are a later
 * milestone. The "read-only" label says exactly what this is.
 */

import { StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../../shared/tokens';
import type { FieldSpec } from '../../core/settings-schema';

export function OpaqueField({ spec }: { spec: FieldSpec }) {
  let rendered: string;
  try {
    rendered = JSON.stringify(spec.value);
  } catch {
    rendered = String(spec.value);
  }
  return (
    <View style={styles.block}>
      <Text style={styles.title}>
        {spec.title} <Text style={styles.ro}>read-only</Text>
      </Text>
      <View style={styles.box}>
        <Text style={styles.json} numberOfLines={3}>
          {rendered}
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  block: { paddingVertical: space.sm, gap: space.xs },
  title: { color: colors.text, fontSize: 14 },
  ro: { color: colors.textFaint, fontSize: 11 },
  box: {
    backgroundColor: colors.bg,
    borderRadius: radius.chip,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    padding: space.sm,
  },
  json: { color: colors.textDim, fontSize: 12, fontFamily: 'monospace' },
});
