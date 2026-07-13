/**
 * admin/components/fields/BooleanField.tsx — a boolean setting as a switch row.
 * The RN Switch already carries state by thumb POSITION (shape), and we add the
 * on/off word next to it — never colour alone (CVD).
 */

import { StyleSheet, Switch, Text, View } from 'react-native';

import { colors, space } from '../../../shared/tokens';
import type { FieldSpec } from '../../core/settings-schema';

interface Props {
  spec: FieldSpec;
  value: boolean;
  onChange: (key: string, value: boolean) => void;
}

export function BooleanField({ spec, value, onChange }: Props) {
  return (
    <View style={styles.row}>
      <Text style={styles.title}>{spec.title}</Text>
      <View style={styles.right}>
        <Text style={styles.stateWord}>{value ? 'on' : 'off'}</Text>
        <Switch
          value={value}
          onValueChange={(v) => onChange(spec.key, v)}
          trackColor={{ false: colors.border, true: colors.surfaceActive }}
          thumbColor={value ? colors.accent : colors.textFaint}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: space.sm,
    gap: space.md,
  },
  title: { color: colors.text, fontSize: 14, flexShrink: 1 },
  right: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  stateWord: { color: colors.textFaint, fontSize: 12, width: 24, textAlign: 'right' },
});
