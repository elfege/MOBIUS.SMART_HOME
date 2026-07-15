/**
 * admin/components/fields/WeeklyWindowsField.tsx — the REAL weekly-windows
 * editor (operator 2026-07-15: the read-only JSON fallback "is not user
 * friendly at all"). Feature parity with the legacy wizard's weeklyWindows
 * widget: a "same windows every day" switch, one uniform list OR seven
 * per-day lists, HH:MM range rows with add/remove.
 *
 * Value shape (unchanged server contract):
 *   { uniform: boolean,
 *     uniformWindows: [{start:"HH:MM", end:"HH:MM"}, ...],
 *     days: { monday:[...], ..., sunday:[...] } }
 *
 * Commit discipline: every STRUCTURAL change (toggle, add, remove) commits
 * immediately via onChange. Time-text edits commit only while valid HH:MM;
 * invalid text stays local with a thick-border + message signal (shape +
 * text, not colour alone — CVD) and the last valid value stands, so Save can
 * never persist garbage.
 */

import { useState } from 'react';
import {
  Pressable,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';

import { colors, radius, space } from '../../../shared/tokens';
import type { FieldSpec } from '../../core/settings-schema';

const DOW = [
  'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
] as const;

interface TimeWindow {
  start: string;
  end: string;
}

interface WeeklyWindows {
  uniform: boolean;
  uniformWindows: TimeWindow[];
  days: Record<string, TimeWindow[]>;
}

interface Props {
  spec: FieldSpec;
  value: unknown;
  onChange: (key: string, value: unknown) => void;
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Coerce whatever the server sent into the full editable shape. */
function normalize(raw: unknown): WeeklyWindows {
  const v = (raw ?? {}) as Partial<WeeklyWindows>;
  const days: Record<string, TimeWindow[]> = {};
  for (const d of DOW) {
    const list = v.days?.[d];
    days[d] = Array.isArray(list) ? list.map((w) => ({ ...w })) : [];
  }
  return {
    uniform: v.uniform !== false,
    uniformWindows: Array.isArray(v.uniformWindows)
      ? v.uniformWindows.map((w) => ({ ...w }))
      : [],
    days,
  };
}

export function WeeklyWindowsField({ spec, value, onChange }: Props) {
  const model = normalize(value);
  /** Local invalid drafts: `${listKey}:${index}:${side}` -> text. Valid edits
   *  commit upward immediately and are removed from here. */
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const commit = (next: WeeklyWindows) => onChange(spec.key, next);

  const editTime = (
    listKey: string,
    index: number,
    side: 'start' | 'end',
    text: string,
  ) => {
    const draftId = `${listKey}:${index}:${side}`;
    if (HHMM.test(text)) {
      const next = normalize(model);
      const list =
        listKey === 'uniform' ? next.uniformWindows : (next.days[listKey] ??= []);
      const cur = list[index] ?? { start: '08:00', end: '20:00' };
      list[index] = { ...cur, [side]: text };
      setDrafts((prev) => {
        const { [draftId]: _gone, ...rest } = prev;
        return rest;
      });
      commit(next);
    } else {
      setDrafts((prev) => ({ ...prev, [draftId]: text }));
    }
  };

  const addWindow = (listKey: string) => {
    const next = normalize(model);
    const list =
      listKey === 'uniform' ? next.uniformWindows : (next.days[listKey] ??= []);
    list.push({ start: '08:00', end: '20:00' });
    commit(next);
  };

  const removeWindow = (listKey: string, index: number) => {
    const next = normalize(model);
    const list =
      listKey === 'uniform' ? next.uniformWindows : (next.days[listKey] ??= []);
    list.splice(index, 1);
    setDrafts({}); // indices shifted — drop any stale invalid drafts
    commit(next);
  };

  const renderList = (listKey: string, list: TimeWindow[]) => (
    <View style={styles.list}>
      {list.map((w, i) => {
        const sDraft = drafts[`${listKey}:${i}:start`];
        const eDraft = drafts[`${listKey}:${i}:end`];
        return (
          <View key={`${listKey}-${i}`} style={styles.rangeRow}>
            <TimeBox
              text={sDraft ?? w.start}
              invalid={sDraft !== undefined}
              onChangeText={(t) => editTime(listKey, i, 'start', t)}
            />
            <Text style={styles.arrow}>→</Text>
            <TimeBox
              text={eDraft ?? w.end}
              invalid={eDraft !== undefined}
              onChangeText={(t) => editTime(listKey, i, 'end', t)}
            />
            <Pressable
              onPress={() => removeWindow(listKey, i)}
              hitSlop={8}
              style={styles.removeBtn}
            >
              <Text style={styles.removeText}>✕</Text>
            </Pressable>
          </View>
        );
      })}
      {list.length === 0 ? (
        <Text style={styles.emptyHint}>no windows — TV never allowed</Text>
      ) : null}
      <Pressable style={styles.addBtn} onPress={() => addWindow(listKey)}>
        <Text style={styles.addText}>+ Add window</Text>
      </Pressable>
    </View>
  );

  return (
    <View style={styles.block}>
      <Text style={styles.title}>{spec.title}</Text>
      <View style={styles.uniformRow}>
        <Text style={styles.dimLabel}>Same windows every day</Text>
        <Switch
          value={model.uniform}
          onValueChange={(on) => commit({ ...normalize(model), uniform: on })}
          trackColor={{ false: colors.border, true: colors.accent }}
          thumbColor={colors.text}
        />
      </View>
      {model.uniform ? (
        renderList('uniform', model.uniformWindows)
      ) : (
        <View style={styles.daysWrap}>
          {DOW.map((d) => (
            <View key={d} style={styles.dayBlock}>
              <Text style={styles.dayName}>
                {d.charAt(0).toUpperCase() + d.slice(1)}
              </Text>
              {renderList(d, model.days[d] ?? [])}
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

function TimeBox({
  text,
  invalid,
  onChangeText,
}: {
  text: string;
  invalid: boolean;
  onChangeText: (t: string) => void;
}) {
  return (
    <View>
      <TextInput
        style={[styles.timeInput, invalid ? styles.timeInvalid : null]}
        value={text}
        onChangeText={onChangeText}
        placeholder="HH:MM"
        placeholderTextColor={colors.textFaint}
        maxLength={5}
        autoCapitalize="none"
        autoCorrect={false}
      />
      {invalid ? <Text style={styles.invalidMsg}>HH:MM</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  block: { paddingVertical: space.sm, gap: space.sm },
  title: { color: colors.text, fontSize: 14 },
  uniformRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  dimLabel: { color: colors.textDim, fontSize: 13 },
  list: { gap: space.sm },
  rangeRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  arrow: { color: colors.textFaint, fontSize: 14 },
  timeInput: {
    backgroundColor: colors.bg,
    borderRadius: radius.chip,
    borderWidth: 1,
    borderColor: colors.border,
    color: colors.text,
    paddingHorizontal: 12,
    paddingVertical: 6,
    minWidth: 72,
    textAlign: 'center',
    fontVariant: ['tabular-nums'],
  },
  timeInvalid: { borderWidth: 2, borderColor: colors.danger },
  invalidMsg: { color: colors.danger, fontSize: 10, textAlign: 'center' },
  removeBtn: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.chip,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  removeText: { color: colors.textDim, fontSize: 13 },
  addBtn: { alignSelf: 'flex-start', paddingVertical: 4 },
  addText: { color: colors.accent, fontSize: 13, fontWeight: '600' },
  daysWrap: { gap: space.md },
  dayBlock: {
    backgroundColor: colors.surface,
    borderRadius: radius.tile,
    padding: space.md,
    gap: space.sm,
  },
  dayName: { color: colors.text, fontSize: 13, fontWeight: '700' },
  emptyHint: { color: colors.textFaint, fontSize: 12, fontStyle: 'italic' },
});
