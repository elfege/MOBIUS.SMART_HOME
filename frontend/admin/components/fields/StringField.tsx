/**
 * admin/components/fields/StringField.tsx — a free-text string setting.
 * (Enum-constrained strings render as EnumField chips instead — the kind
 * split happens in settings-schema.ts, never here.)
 */

import { StyleSheet, Text, TextInput, View } from 'react-native';

import { colors, radius, space } from '../../../shared/tokens';
import type { FieldSpec } from '../../core/settings-schema';

interface Props {
  spec: FieldSpec;
  value: string;
  onChange: (key: string, value: string) => void;
}

export function StringField({ spec, value, onChange }: Props) {
  return (
    <View style={styles.row}>
      <Text style={styles.title}>{spec.title}</Text>
      <TextInput
        style={styles.input}
        value={value}
        onChangeText={(t) => onChange(spec.key, t)}
        placeholderTextColor={colors.textFaint}
        autoCapitalize="none"
      />
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
  input: {
    backgroundColor: colors.bg,
    color: colors.text,
    borderRadius: radius.chip,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: 10,
    paddingVertical: 6,
    minWidth: 160,
    fontSize: 14,
  },
});
