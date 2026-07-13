/**
 * App.tsx — the tiles PROTO screen.
 *
 * Definition of done (methodology): a tablet loads mobius.tiles, sees its
 * devices as touch tiles, and can toggle one. Renders the SERVER-RESOLVED roster
 * (GET /api/panel/devices) grouped by section; toggling a switch/dimmer/color
 * tile posts to the panel command endpoint (which delegates to the shared
 * commander, Matter-first-then-Hubitat). Freshness by ~12s polling for the proto
 * (see shared/ws.ts for why WS live-updates are a coordinated follow-on).
 *
 * Auth for the proto: an enrolled panel Bearer token, pasted once and stored
 * (shared/auth). The zero-touch wall-tablet LAN bootstrap is a later backend
 * addition; a paste-once flow keeps the proto pure-frontend / zero-restart.
 */
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useState } from 'react';
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { getToken, setToken } from '../shared/auth';
import { colors, radius, space } from '../shared/tokens';
import { TransportError } from '../shared/transport';
import { DeviceTile } from './components/DeviceTile';
import { PanelApi } from './core/panel-api';
import type { Tile } from './core/panel-types';
import { useTilesStore } from './core/store';

const api = new PanelApi('');
const POLL_MS = 12000;

export default function App() {
  const sections = useTilesStore((s) => s.sections);
  const tiles = useTilesStore((s) => s.tiles);
  const order = useTilesStore((s) => s.order);
  const status = useTilesStore((s) => s.status);
  const setRoster = useTilesStore((s) => s.setRoster);
  const setStatus = useTilesStore((s) => s.setStatus);
  const setPrimaryValue = useTilesStore((s) => s.setPrimaryValue);
  const [tokenInput, setTokenInput] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await api.roster();
      setRoster(r.sections, r.tiles);
    } catch (e) {
      if (e instanceof TransportError && e.status === 401) setStatus('unauthorized');
      else setStatus('error');
    }
  }, [setRoster, setStatus]);

  useEffect(() => {
    void load();
    const id = setInterval(() => {
      if (getToken()) void load();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const onToggle = useCallback(
    async (t: Tile) => {
      const next = (t.primary_value ?? '').toLowerCase() === 'on' ? 'off' : 'on';
      setPrimaryValue(t.id, next); // optimistic; the refresh reconciles
      try {
        await api.command(t.id, next);
      } catch {
        /* command failed — the poll refresh will restore the true state */
      }
      void load();
    },
    [setPrimaryValue, load],
  );

  const saveToken = () => {
    const t = tokenInput.trim();
    if (!t) return;
    setToken(t);
    setStatus('loading');
    void load();
  };

  // Group tile ids by section, preserving server order.
  const bySection: Record<string, number[]> = {};
  for (const id of order) {
    const tile = tiles[id];
    if (!tile) continue;
    (bySection[tile.section_slug] ??= []).push(id);
  }
  const onCount = order.filter((id) => (tiles[id]?.primary_value ?? '').toLowerCase() === 'on').length;

  if (status === 'unauthorized') {
    return (
      <View style={styles.center}>
        <StatusBar style="light" />
        <Text style={styles.title}>MOBIUS</Text>
        <Text style={styles.dim}>Enter this panel&apos;s access token</Text>
        <TextInput
          style={styles.input}
          placeholder="panel token"
          placeholderTextColor={colors.textFaint}
          autoCapitalize="none"
          value={tokenInput}
          onChangeText={setTokenInput}
          onSubmitEditing={saveToken}
          secureTextEntry
        />
        <Pressable style={styles.btn} onPress={saveToken}>
          <Text style={styles.btnText}>Connect</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <StatusBar style="light" />
      <View style={styles.header}>
        <Text style={styles.title}>MOBIUS</Text>
        <Text style={styles.dim}>
          {order.length} devices · {onCount} on{status === 'loading' ? ' · loading…' : ''}
        </Text>
      </View>
      {status === 'error' ? (
        <View style={styles.center}>
          <Text style={styles.err}>● Backend unreachable</Text>
          <Pressable style={styles.btn} onPress={() => { setStatus('loading'); void load(); }}>
            <Text style={styles.btnText}>Retry</Text>
          </Pressable>
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.scroll}>
          {sections.map((sec) => {
            const ids = bySection[sec.slug];
            if (!ids || ids.length === 0) return null;
            return (
              <View key={sec.slug} style={styles.section}>
                <Text style={styles.sectionTitle}>{sec.name}</Text>
                <View style={styles.grid}>
                  {ids.map((id) => {
                    const tile = tiles[id];
                    return tile ? (
                      <DeviceTile key={id} tile={tile} onToggle={onToggle} />
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
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: space.md },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
    backgroundColor: colors.bg,
    padding: space.lg,
  },
  err: { color: colors.danger, fontSize: 15 },
  input: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderRadius: radius.chip,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    minWidth: 280,
  },
  btn: {
    backgroundColor: colors.accent,
    borderRadius: radius.chip,
    paddingVertical: 11,
    paddingHorizontal: 24,
    alignItems: 'center',
  },
  btnText: { color: colors.bg, fontWeight: '700' },
});
