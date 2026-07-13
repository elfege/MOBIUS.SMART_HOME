/**
 * admin/components/InstanceCard.tsx — one automation instance.
 *
 * Presentation + a single action for the proto: pause/resume. Errors are
 * surfaced honestly — `error_count` with the last error text, in danger red
 * PLUS the "⚠"-free textual prefix "errors:" (never hue alone, and never the
 * banned warning triangle, which renders yellow-orange — CVD canon PIN.1).
 * All lifecycle derivation comes from instanceState() in admin-types; this
 * component never re-derives it.
 */

import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import { instanceState, type InstanceRow } from '../core/admin-types';
import { StatusPill } from './StatusPill';

interface Props {
  row: InstanceRow;
  busy: boolean;
  onPause: (row: InstanceRow) => void;
  onResume: (row: InstanceRow) => void;
}

export function InstanceCard({ row, busy, onPause, onResume }: Props) {
  const state = instanceState(row);
  const canAct = state !== 'disabled';
  const actionLabel = state === 'paused' ? 'Resume' : 'Pause';
  const onPress = () => (state === 'paused' ? onResume(row) : onPause(row));

  return (
    <View style={styles.card}>
      <View style={styles.rowTop}>
        <Text style={styles.label} numberOfLines={1}>
          {row.label}
        </Text>
        <StatusPill state={state} />
      </View>

      {row.is_paused && row.pause_reason ? (
        <Text style={styles.meta} numberOfLines={1}>
          reason: {row.pause_reason}
        </Text>
      ) : null}

      {row.error_count > 0 ? (
        <Text style={styles.error} numberOfLines={2}>
          errors: {row.error_count}
          {row.last_error ? ` — ${row.last_error}` : ''}
        </Text>
      ) : null}

      {canAct ? (
        <Pressable
          style={[styles.btn, busy ? styles.btnBusy : null]}
          disabled={busy}
          onPress={onPress}
        >
          <Text style={styles.btnText}>{busy ? '…' : actionLabel}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.tile,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    padding: space.md,
    gap: space.sm,
    minWidth: 260,
    flexGrow: 1,
    flexBasis: 260,
  },
  rowTop: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.sm,
  },
  label: { color: colors.text, fontSize: 15, fontWeight: '600', flexShrink: 1 },
  meta: { color: colors.textFaint, fontSize: 12 },
  error: { color: colors.danger, fontSize: 12 },
  btn: {
    backgroundColor: colors.bg,
    borderRadius: radius.chip,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.borderActive,
    paddingVertical: 8,
    alignItems: 'center',
    marginTop: space.xs,
  },
  btnBusy: { opacity: 0.5 },
  btnText: { color: colors.accent, fontWeight: '700', fontSize: 13 },
});
