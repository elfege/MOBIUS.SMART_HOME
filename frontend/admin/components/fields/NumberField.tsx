/**
 * admin/components/fields/NumberField.tsx — an integer/number setting as a
 * validated numeric input. Invalid input = danger text stating the constraint
 * PLUS a thick border (shape signal, not colour alone — CVD). The parent
 * receives the raw text and the validity verdict; it must not save while any
 * field is invalid.
 */

import { StyleSheet, Text, TextInput, View } from 'react-native';

import { colors, radius, space } from '../../../shared/tokens';
import { validateNumber, type FieldSpec } from '../../core/settings-schema';

interface Props {
  spec: FieldSpec;
  /** Raw text as typed (parent owns it so dirty-tracking sees every change). */
  text: string;
  onChangeText: (key: string, text: string, error: string | null) => void;
}

export function NumberField({ spec, text, onChangeText }: Props) {
  const error = validateNumber(spec, text);
  const range =
    spec.minimum !== null || spec.maximum !== null
      ? ` (${spec.minimum ?? '…'}–${spec.maximum ?? '…'})`
      : '';
  return (
    <View style={styles.row}>
      <Text style={styles.title}>
        {spec.title}
        <Text style={styles.range}>{range}</Text>
      </Text>
      <View style={styles.right}>
        <TextInput
          style={[styles.input, error ? styles.inputInvalid : null]}
          value={text}
          onChangeText={(t) => onChangeText(spec.key, t, validateNumber(spec, t))}
          keyboardType="numeric"
          placeholderTextColor={colors.textFaint}
        />
        {error ? <Text style={styles.error}>{error}</Text> : null}
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
  range: { color: colors.textFaint, fontSize: 12 },
  right: { alignItems: 'flex-end', gap: 2 },
  input: {
    backgroundColor: colors.bg,
    color: colors.text,
    borderRadius: radius.chip,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: 10,
    paddingVertical: 6,
    minWidth: 90,
    textAlign: 'right',
    fontSize: 14,
  },
  inputInvalid: { borderColor: colors.danger, borderWidth: 2 },
  error: { color: colors.danger, fontSize: 11 },
});
