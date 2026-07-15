/**
 * admin/screens/HomeScreen.tsx — the app's FRONT DOOR (cutover contract,
 * Architect MSG-1036/1041): on day one the RN app must present the whole app
 * even though only Automations is native.
 *
 * Two tile kinds, visually distinct but one grid:
 *   NATIVE  — opens an RN view (Automations today; each ported surface joins).
 *   LEGACY  — opens /legacy/<name> SAME-TAB (ruling MSG-1039/1044: preserves
 *             the one-app illusion + the RN session; the legacy base carries a
 *             "back to app" link). Marked "current version · migrating" so the
 *             operator always knows which world he is in.
 *
 * The legacy-tile count is the PUBLIC MIGRATION BURNDOWN: it reaches zero when
 * the port is done and /legacy retires. CVD-safe: kind is carried by a text
 * badge + border treatment (blue for native, neutral for legacy) — never hue
 * alone, never green.
 */

import { Image, Linking, Platform, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import { useNav } from '../core/nav';

/** A not-yet-ported surface, served by the legacy jQuery/Jinja app. The `path`
 *  is the Architect's /legacy/<name> route map (MSG-1036; stable URLs). */
interface LegacySurface {
  title: string;
  description: string;
  path: string;
}

const LEGACY_SURFACES: LegacySurface[] = [
  // Canonical map = apps/legacy_web/router.py (Architect, 2026-07-14). The
  // earlier six-tile guess pointed at routes that never existed
  // (/legacy/dashboard 404'd, /legacy/samsung-tv 404'd, /legacy/hubs was a
  // redirect) — hubs and TVs both live INSIDE the settings page.
  { title: 'Matter', description: 'Devices, commissioning, hub→hub copy, Get Code', path: '/legacy/matter' },
  { title: 'Dashboard', description: 'Device dashboard and event charts', path: '/legacy' },
  { title: 'Sonos', description: 'Speakers and grouping', path: '/legacy/sonos' },
  { title: 'Settings', description: 'System settings · hubs · TVs · certificates', path: '/legacy/admin/settings' },
];

/** Open a legacy surface SAME-TAB on web (back button returns to the RN home);
 *  native builds hand off to the system browser (no legacy inside the app). */
function openLegacy(path: string): void {
  if (Platform.OS === 'web' && typeof window !== 'undefined') {
    window.location.assign(path);
    return;
  }
  void Linking.openURL(path);
}

export function HomeScreen() {
  const openAutomations = useNav((s) => s.openAutomations);

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <View style={styles.brandRow}>
          {/* The canonical mobius brand mark (same file the legacy navbar and
              the favicon use), like the tiles app pairs its LogoMark with the
              wordmark (operator 2026-07-15). */}
          <Image
            source={{ uri: '/static/img/mobius.png' }}
            style={styles.logo}
            resizeMode="contain"
          />
          <Text style={styles.title}>
            MOBIUS<Text style={styles.titleAccent}>.HOME</Text>
          </Text>
        </View>
        <Text style={styles.dim}>Home control · admin</Text>
      </View>
      <ScrollView contentContainerStyle={styles.scroll}>
        <View style={styles.grid}>
          {/* NATIVE tiles — RN views. Automations is the first; every ported
              surface adds one here and removes itself from LEGACY_SURFACES. */}
          <Pressable style={[styles.tile, styles.tileNative]} onPress={openAutomations}>
            <View style={styles.tileTop}>
              <Text style={styles.tileTitle}>Automations</Text>
              <Text style={styles.badgeNative}>native</Text>
            </View>
            <Text style={styles.tileDesc}>
              Motion lighting, power management, screen time — all app instances
            </Text>
          </Pressable>

          {/* The wall-panel tiles app (frontend/tiles, RN) — a sibling RN app,
              not a legacy surface (operator 2026-07-15: "no access to tiles
              from new RN interface"). Same-tab; back returns here. */}
          <Pressable
            style={[styles.tile, styles.tileNative]}
            onPress={() => openLegacy('/static/panel/index.html')}
          >
            <View style={styles.tileTop}>
              <Text style={styles.tileTitle}>Wall Panel</Text>
              <Text style={styles.badgeNative}>native</Text>
            </View>
            <Text style={styles.tileDesc}>
              MOBIUS.TILES — room-by-room device control panel
            </Text>
          </Pressable>

          {/* LEGACY tiles — the burndown list. Same-tab links into /legacy. */}
          {LEGACY_SURFACES.map((s) => (
            <Pressable
              key={s.path}
              style={styles.tile}
              onPress={() => openLegacy(s.path)}
            >
              <View style={styles.tileTop}>
                <Text style={styles.tileTitle}>{s.title}</Text>
                <Text style={styles.badgeLegacy}>current version · migrating</Text>
              </View>
              <Text style={styles.tileDesc}>{s.description}</Text>
            </Pressable>
          ))}
        </View>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg, paddingTop: 40 },
  header: { paddingHorizontal: space.lg, paddingBottom: space.md },
  title: { color: colors.text, fontSize: 28, fontWeight: '700', letterSpacing: 0.5 },
  // The ".HOME" accent — same high-luminance blue as the legacy navbar's
  // logo-dot; luminance-contrast carries it (CVD-safe), the hue just agrees.
  titleAccent: { color: colors.accent },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  logo: { width: 34, height: 34 },
  dim: { color: colors.textFaint, fontSize: 13, marginTop: space.xs },
  scroll: { padding: space.lg },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: space.md },
  tile: {
    backgroundColor: colors.surface,
    borderRadius: radius.tile,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    padding: space.md,
    gap: space.sm,
    minWidth: 260,
    flexGrow: 1,
    flexBasis: 260,
    minHeight: 96,
  },
  // Native tiles read as "ours/ready": blue-shifted surface + blue border —
  // luminance + border + the text badge, never hue alone.
  tileNative: {
    backgroundColor: colors.surfaceActive,
    borderColor: colors.borderActive,
  },
  tileTop: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: space.sm,
  },
  tileTitle: { color: colors.text, fontSize: 17, fontWeight: '700', flexShrink: 1 },
  badgeNative: {
    color: colors.bg,
    backgroundColor: colors.accent,
    borderRadius: radius.chip,
    paddingHorizontal: 8,
    paddingVertical: 2,
    fontSize: 11,
    fontWeight: '700',
    overflow: 'hidden',
  },
  badgeLegacy: {
    color: colors.textDim,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radius.chip,
    paddingHorizontal: 8,
    paddingVertical: 2,
    fontSize: 11,
    fontWeight: '600',
    overflow: 'hidden',
  },
  tileDesc: { color: colors.textDim, fontSize: 13, lineHeight: 18 },
});
