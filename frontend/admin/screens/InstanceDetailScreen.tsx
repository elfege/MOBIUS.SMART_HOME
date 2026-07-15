/**
 * admin/screens/InstanceDetailScreen.tsx — one instance's settings editor.
 *
 * Schema-driven: fields derive from the app type's settings_schema + the
 * instance's current settings (core/settings-schema.ts); each field kind
 * renders its dedicated component. Dirty tracking is per-key; Save sends ONLY
 * changed keys (the server merges) and goes through core/save-verify.ts, so a
 * dropped connection can never be reported as a failed write (RULE 9.1.4).
 *
 * Save is disabled while any numeric field is invalid, while nothing is dirty,
 * and while a save is in flight. The outcome banner states exactly what is
 * known: saved / saved-after-reconnect / rejected(HTTP n) / unverified.
 */

import { useCallback, useMemo, useState } from 'react';
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import { AdminApi } from '../core/admin-api';
import { instanceState } from '../core/admin-types';
import { useNav } from '../core/nav';
import { saveSettings, type SaveOutcome } from '../core/save-verify';
import {
  deriveFields,
  validateNumber,
  type FieldSpec,
} from '../core/settings-schema';
import { useAdminStore } from '../core/store';
import { BooleanField } from '../components/fields/BooleanField';
import { EnumField } from '../components/fields/EnumField';
import { NumberField } from '../components/fields/NumberField';
import { OpaqueField } from '../components/fields/OpaqueField';
import { StringField } from '../components/fields/StringField';
import { WeeklyWindowsField } from '../components/fields/WeeklyWindowsField';
import { SonosTestButton } from '../components/SonosTestButton';
import { StatusPill } from '../components/StatusPill';

const api = new AdminApi('');

type SaveState =
  | { phase: 'idle' }
  | { phase: 'saving' }
  | { phase: 'done'; outcome: SaveOutcome };

export function InstanceDetailScreen({ instanceId }: { instanceId: number }) {
  const backToList = useNav((s) => s.backToList);
  const row = useAdminStore((s) => s.instances[instanceId]);
  const appTypes = useAdminStore((s) => s.appTypes);
  const setInstance = useAdminStore((s) => s.setInstance);

  /** Per-key edits: committed-value candidates (booleans/enums/strings). */
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  /** Raw text + error per numeric key (text owns the input; value derives). */
  const [numText, setNumText] = useState<Record<string, string>>({});
  const [saveState, setSaveState] = useState<SaveState>({ phase: 'idle' });

  const appType = row ? appTypes[row.app_type_id] : undefined;
  const fields = useMemo(
    () =>
      row && appType
        ? deriveFields(appType.settings_schema, row.settings)
        : [],
    [row, appType],
  );

  const onValue = useCallback((key: string, value: unknown) => {
    setEdits((prev) => ({ ...prev, [key]: value }));
    setSaveState({ phase: 'idle' });
  }, []);

  const onNumText = useCallback(
    (key: string, text: string, error: string | null) => {
      setNumText((prev) => ({ ...prev, [key]: text }));
      if (error === null) {
        setEdits((prev) => ({ ...prev, [key]: Number(text) }));
      } else {
        // Invalid text: withdraw any pending edit for this key so a stale
        // valid value can never be saved underneath invalid visible input.
        setEdits((prev) => {
          const { [key]: _dropped, ...rest } = prev;
          return rest;
        });
      }
      setSaveState({ phase: 'idle' });
    },
    [],
  );

  /** True while any numeric input's visible text fails validation. */
  const hasInvalid = fields.some((f) => {
    if (f.kind !== 'integer' && f.kind !== 'number') return false;
    const text = numText[f.key];
    if (text === undefined) return false; // untouched -> current value stands
    return validateNumber(f, text) !== null;
  });

  const dirtyKeys = Object.keys(edits).filter(
    (k) => !valueEqualToCurrent(k, edits[k], fields),
  );
  const canSave =
    dirtyKeys.length > 0 && !hasInvalid && saveState.phase !== 'saving';

  const onSave = useCallback(async () => {
    if (!row) return;
    const payload: Record<string, unknown> = {};
    for (const k of dirtyKeys) payload[k] = edits[k];
    setSaveState({ phase: 'saving' });
    const outcome = await saveSettings(api, row.id, payload);
    if (outcome.result === 'saved' && outcome.row) {
      setInstance(outcome.row);
      setEdits({});
      setNumText({});
    }
    setSaveState({ phase: 'done', outcome });
  }, [row, dirtyKeys, edits, setInstance]);

  if (!row) {
    return (
      <View style={styles.center}>
        <Text style={styles.dim}>Instance not found.</Text>
        <Pressable style={styles.btn} onPress={backToList}>
          <Text style={styles.btnText}>Back</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Pressable onPress={backToList} hitSlop={8}>
          <Text style={styles.back}>‹ Back</Text>
        </Pressable>
        <Text style={styles.title} numberOfLines={1}>
          {row.label}
        </Text>
        <StatusPill state={instanceState(row)} />
      </View>
      <Text style={styles.subtitle}>
        {appType?.display_name ?? `App type ${row.app_type_id}`} · id {row.id}
      </Text>

      <ScrollView contentContainerStyle={styles.scroll}>
        {fields
          .filter((f) => isVisible(f, edits, fields))
          .map((f) => (
            <View key={f.key}>
              {renderField(f, edits, numText, onValue, onNumText)}
              {f.key === 'announceRoom' ? (
                <SonosTestButton fields={fields} edits={edits} />
              ) : null}
            </View>
          ))}
      </ScrollView>

      <View style={styles.footer}>
        <SaveBanner state={saveState} />
        <Pressable
          style={[styles.saveBtn, !canSave ? styles.saveBtnDisabled : null]}
          disabled={!canSave}
          onPress={() => void onSave()}
        >
          <Text style={styles.saveBtnText}>
            {saveState.phase === 'saving'
              ? 'Saving…'
              : dirtyKeys.length > 0
                ? `Save ${dirtyKeys.length} change${dirtyKeys.length === 1 ? '' : 's'}`
                : 'No changes'}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}

/** Schema-driven conditional visibility (`visibleWhen`): the controlling
 *  key's EFFECTIVE value (pending edit first, then saved) must equal the
 *  schema's `equals`. Lets a boolean gate its dependent fields live — e.g.
 *  wake-on-power seconds behind its toggle (operator 2026-07-15). */
function isVisible(
  f: FieldSpec,
  edits: Record<string, unknown>,
  fields: FieldSpec[],
): boolean {
  if (!f.visibleWhen) return true;
  const ctrl = fields.find((x) => x.key === f.visibleWhen!.key);
  const effective =
    f.visibleWhen.key in edits ? edits[f.visibleWhen.key] : ctrl?.value;
  return effective === f.visibleWhen.equals;
}

/** Compare a pending edit against the field's current server-known value. */
function valueEqualToCurrent(
  key: string,
  edited: unknown,
  fields: FieldSpec[],
): boolean {
  const f = fields.find((x) => x.key === key);
  if (!f) return false;
  return JSON.stringify(edited) === JSON.stringify(f.value);
}

/** Dispatch one field spec to its component, resolving the displayed value
 *  from pending edits first, then the server-known value. */
function renderField(
  f: FieldSpec,
  edits: Record<string, unknown>,
  numText: Record<string, string>,
  onValue: (key: string, value: unknown) => void,
  onNumText: (key: string, text: string, error: string | null) => void,
) {
  const effective = f.key in edits ? edits[f.key] : f.value;
  switch (f.kind) {
    case 'boolean':
      return (
        <BooleanField
          key={f.key}
          spec={f}
          value={effective === true}
          onChange={onValue}
        />
      );
    case 'enum':
      return (
        <EnumField
          key={f.key}
          spec={f}
          value={String(effective ?? '')}
          onChange={onValue}
        />
      );
    case 'integer':
    case 'number':
      return (
        <NumberField
          key={f.key}
          spec={f}
          text={numText[f.key] ?? String(effective ?? '')}
          onChangeText={onNumText}
        />
      );
    case 'string':
      return (
        <StringField
          key={f.key}
          spec={f}
          value={String(effective ?? '')}
          onChange={onValue}
        />
      );
    case 'windows':
      return (
        <WeeklyWindowsField
          key={f.key}
          spec={f}
          value={effective}
          onChange={onValue}
        />
      );
    case 'opaque':
      return <OpaqueField key={f.key} spec={f} />;
  }
}

/** The save-outcome banner. States exactly what is known — including the
 *  recovered-after-network-drop case that the 12:04 incident demands. */
function SaveBanner({ state }: { state: SaveState }) {
  if (state.phase !== 'done') return null;
  const o = state.outcome;
  if (o.result === 'saved') {
    return (
      <Text style={styles.saved}>
        ● saved{o.recovered ? ' (connection dropped — verified on the server)' : ''}
      </Text>
    );
  }
  if (o.result === 'rejected') {
    return <Text style={styles.failed}>■ rejected by server (HTTP {o.status})</Text>;
  }
  return <Text style={styles.failed}>◌ unverified: {o.detail}</Text>;
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg, paddingTop: 40 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
  },
  back: { color: colors.accent, fontSize: 16, fontWeight: '600' },
  title: {
    color: colors.text,
    fontSize: 20,
    fontWeight: '700',
    flexShrink: 1,
    flexGrow: 1,
  },
  subtitle: {
    color: colors.textFaint,
    fontSize: 12,
    paddingHorizontal: space.lg,
    marginTop: space.xs,
  },
  scroll: { padding: space.lg, paddingBottom: 120 },
  footer: {
    padding: space.lg,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    gap: space.sm,
    backgroundColor: colors.surface,
  },
  saveBtn: {
    backgroundColor: colors.accent,
    borderRadius: radius.chip,
    paddingVertical: 12,
    alignItems: 'center',
  },
  saveBtnDisabled: { opacity: 0.4 },
  saveBtnText: { color: colors.bg, fontWeight: '700', fontSize: 15 },
  saved: { color: colors.accent, fontSize: 13 },
  failed: { color: colors.danger, fontSize: 13 },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    backgroundColor: colors.bg,
  },
  dim: { color: colors.textFaint },
  btn: {
    backgroundColor: colors.accent,
    borderRadius: radius.chip,
    paddingVertical: 10,
    paddingHorizontal: 22,
  },
  btnText: { color: colors.bg, fontWeight: '700' },
});
