/**
 * admin/components/fields/EnumField.tsx — an enum setting as a chip row.
 * Selected chip = brighter blue-shifted surface (luminance) + accent border +
 * bold text — three redundant signals, never hue alone (CVD).
 */

import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../../shared/tokens';
import type { FieldSpec } from '../../core/settings-schema';

interface Props {
  spec: FieldSpec;
  value: string;
  onChange: (key: string, value: string) => void;
}

export function EnumField({ spec, value, onChange }: Props) {
  return (
    <View style={styles.block}>
      <Text style={styles.title}>{spec.title}</Text>
      <View style={styles.chips}>
        {(spec.enumOptions ?? []).map((opt) => {
          const selected = opt === value;
          return (
            <Pressable
              key={opt}
              style={[styles.chip, selected ? styles.chipSelected : null]}
              onPress={() => onChange(spec.key, opt)}
            >
              <Text
                style={[styles.chipText, selected ? styles.chipTextSelected : null]}
              >
                {opt}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  block: { paddingVertical: space.sm, gap: space.sm },
  title: { color: colors.text, fontSize: 14 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  chip: {
    backgroundColor: colors.bg,
    borderRadius: radius.chip,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  chipSelected: {
    backgroundColor: colors.surfaceActive,
    borderColor: colors.borderActive,
  },
  chipText: { color: colors.textDim, fontSize: 13 },
  chipTextSelected: { color: colors.textOnActive, fontWeight: '700' },
});
