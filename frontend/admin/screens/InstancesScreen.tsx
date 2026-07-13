/**
 * admin/screens/InstancesScreen.tsx — the admin PROTO screen.
 *
 * Definition of done (parallel-agent methodology, admin twin of the tiles
 * proto): a browser loads the admin app, sees every automation instance
 * grouped by app type with an honest lifecycle state, and can pause/resume
 * one. Freshness by ~15s polling for the proto (shared/ws.ts live-updates are
 * the same coordinated follow-on as for tiles).
 *
 * Pause/resume is optimistic (busy-guarded per instance) and ALWAYS reconciled
 * by an immediate refetch — the UI never asserts a state the server hasn't
 * confirmed for longer than one round-trip.
 */

import { useCallback, useEffect } from 'react';
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { colors, space } from '../../shared/tokens';
import { AdminApi } from '../core/admin-api';
import { instanceState, type InstanceRow } from '../core/admin-types';
import { useAdminStore } from '../core/store';
import { InstanceCard } from '../components/InstanceCard';

const api = new AdminApi('');
const POLL_MS = 15000;

export function InstancesScreen() {
  const instances = useAdminStore((s) => s.instances);
  const order = useAdminStore((s) => s.order);
  const appTypes = useAdminStore((s) => s.appTypes);
  const status = useAdminStore((s) => s.status);
  const busy = useAdminStore((s) => s.busy);
  const setData = useAdminStore((s) => s.setData);
  const setStatus = useAdminStore((s) => s.setStatus);
  const setPaused = useAdminStore((s) => s.setPaused);
  const setBusy = useAdminStore((s) => s.setBusy);

  const load = useCallback(async () => {
    try {
      const [rows, types] = await Promise.all([api.instances(), api.appTypes()]);
      setData(rows, types);
    } catch {
      setStatus('error');
    }
  }, [setData, setStatus]);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  /** Run one pause/resume action: optimistic flip, busy guard, reconcile. */
  const act = useCallback(
    async (row: InstanceRow, action: 'pause' | 'resume') => {
      setBusy(row.id, true);
      setPaused(row.id, action === 'pause'); // optimistic; refetch reconciles
      try {
        if (action === 'pause') await api.pause(row.id);
        else await api.resume(row.id);
      } catch {
        /* failed/dropped — the refetch below restores the true state */
      }
      await load();
      setBusy(row.id, false);
    },
    [setBusy, setPaused, load],
  );

  const onPause = useCallback((row: InstanceRow) => void act(row, 'pause'), [act]);
  const onResume = useCallback((row: InstanceRow) => void act(row, 'resume'), [act]);

  // Group instance ids by app type, preserving server (created_at desc) order.
  const byType: Record<number, number[]> = {};
  for (const id of order) {
    const row = instances[id];
    if (!row) continue;
    (byType[row.app_type_id] ??= []).push(id);
  }
  // Stable group order: by app-type display name.
  const typeIds = Object.keys(byType)
    .map(Number)
    .sort((a, b) =>
      (appTypes[a]?.display_name ?? String(a)).localeCompare(
        appTypes[b]?.display_name ?? String(b),
      ),
    );

  const pausedCount = order.filter((id) => instances[id]?.is_paused).length;

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Text style={styles.title}>MOBIUS Admin</Text>
        <Text style={styles.dim}>
          {order.length} automations · {pausedCount} paused
          {status === 'loading' ? ' · loading…' : ''}
        </Text>
      </View>

      {status === 'error' ? (
        <View style={styles.center}>
          <Text style={styles.err}>● Backend unreachable</Text>
          <Pressable
            style={styles.btn}
            onPress={() => {
              setStatus('loading');
              void load();
            }}
          >
            <Text style={styles.btnText}>Retry</Text>
          </Pressable>
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.scroll}>
          {typeIds.map((typeId) => {
            const ids = byType[typeId];
            if (!ids || ids.length === 0) return null;
            const runningCount = ids.filter(
              (id) => {
                const row = instances[id];
                return row ? instanceState(row) === 'running' : false;
              },
            ).length;
            return (
              <View key={typeId} style={styles.section}>
                <Text style={styles.sectionTitle}>
                  {appTypes[typeId]?.display_name ?? `App type ${typeId}`}
                  <Text style={styles.sectionCount}>
                    {'  '}{runningCount}/{ids.length} running
                  </Text>
                </Text>
                <View style={styles.grid}>
                  {ids.map((id) => {
                    const row = instances[id];
                    return row ? (
                      <InstanceCard
                        key={id}
                        row={row}
                        busy={busy[id] === true}
                        onPause={onPause}
                        onResume={onResume}
                      />
                    ) : null;
                  })}
                </View>
              </View>
            );
          })}
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg, paddingTop: 40 },
  header: { paddingHorizontal: space.lg, paddingBottom: space.md },
  title: { color: colors.text, fontSize: 26, fontWeight: '700', letterSpacing: 0.5 },
  dim: { color: colors.textFaint, fontSize: 13, marginTop: space.xs },
  scroll: { padding: space.lg, gap: space.lg },
  section: { marginBottom: space.lg },
  sectionTitle: {
    color: colors.textDim,
    fontSize: 15,
    fontWeight: '600',
    marginBottom: space.md,
  },
  sectionCount: { color: colors.textFaint, fontSize: 12, fontWeight: '400' },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: space.md },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    padding: space.lg,
  },
  err: { color: colors.danger, fontSize: 15 },
  btn: {
    backgroundColor: colors.accent,
    borderRadius: 14,
    paddingVertical: 11,
    paddingHorizontal: 24,
    alignItems: 'center',
  },
  btnText: { color: colors.bg, fontWeight: '700' },
});
