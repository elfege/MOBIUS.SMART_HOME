/**
 * admin/components/SonosTestButton.tsx — "Test" next to the Sonos warning
 * speakers (operator 2026-07-15): fires the configured warning announcement
 * on the CURRENTLY selected speakers with the CURRENT (even unsaved) voice /
 * volume / message, via the existing POST /api/sonos/announce testing
 * endpoint — so what you hear is exactly what you have on screen, before
 * committing it.
 */

import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import { Transport } from '../../shared/transport';
import type { FieldSpec } from '../core/settings-schema';

const t = new Transport('');

type TestState =
  | { phase: 'idle' }
  | { phase: 'sending' }
  | { phase: 'done'; note: string; failed: boolean };

interface Props {
  fields: FieldSpec[];
  edits: Record<string, unknown>;
}

/** Effective (edited-first) value of a settings key. */
function eff(fields: FieldSpec[], edits: Record<string, unknown>, key: string): unknown {
  if (key in edits) return edits[key];
  return fields.find((f) => f.key === key)?.value;
}

export function SonosTestButton({ fields, edits }: Props) {
  const [state, setState] = useState<TestState>({ phase: 'idle' });

  const rooms = String(eff(fields, edits, 'announceRoom') ?? '')
    .split(',')
    .map((r) => r.trim())
    .filter(Boolean);

  const onTest = async () => {
    if (rooms.length === 0 || state.phase === 'sending') return;
    setState({ phase: 'sending' });
    const volume = Number(eff(fields, edits, 'announceVolume') ?? 35);
    const voice = String(eff(fields, edits, 'voice') ?? 'edge:en-US-AvaNeural');
    // A realistic warning line beats "test test": substitute the message
    // placeholders the way the app does for a 5-minute lead.
    const text = String(
      eff(fields, edits, 'warningMessage') ?? 'Screen time ends in %minutes% %unit%',
    )
      .replace('%minutes%', '5')
      .replace('%unit%', 'minutes');
    try {
      await Promise.all(
        rooms.map((room) =>
          t.post('/api/sonos/announce', { room, text, volume, voice }),
        ),
      );
      setState({
        phase: 'done',
        note: `queued on ${rooms.length} speaker${rooms.length === 1 ? '' : 's'}`,
        failed: false,
      });
    } catch (e) {
      setState({
        phase: 'done',
        note: `failed: ${e instanceof Error ? e.message : String(e)}`,
        failed: true,
      });
    }
  };

  return (
    <View style={styles.row}>
      <Pressable
        style={[styles.btn, rooms.length === 0 ? styles.btnDisabled : null]}
        disabled={rooms.length === 0 || state.phase === 'sending'}
        onPress={() => void onTest()}
      >
        <Text style={styles.btnText}>
          {state.phase === 'sending' ? 'Playing…' : '♪ Test'}
        </Text>
      </Pressable>
      {state.phase === 'done' ? (
        <Text style={state.failed ? styles.failed : styles.ok}>
          {state.failed ? '■ ' : '● '}
          {state.note}
        </Text>
      ) : rooms.length === 0 ? (
        <Text style={styles.hint}>select a speaker first</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingBottom: space.sm,
  },
  btn: {
    borderWidth: 1,
    borderColor: colors.borderActive,
    borderRadius: radius.chip,
    paddingHorizontal: 16,
    paddingVertical: 6,
  },
  btnDisabled: { opacity: 0.4, borderColor: colors.border },
  btnText: { color: colors.accent, fontSize: 13, fontWeight: '700' },
  ok: { color: colors.accent, fontSize: 12 },
  failed: { color: colors.danger, fontSize: 12 },
  hint: { color: colors.textFaint, fontSize: 12, fontStyle: 'italic' },
});
