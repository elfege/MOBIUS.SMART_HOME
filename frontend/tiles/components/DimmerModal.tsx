/**
 * components/DimmerModal.tsx — the dimmer + RGB control surface, opened by a
 * long-press on a dimmer/color tile. Ported from the original MOBIUS.TILES
 * BaseDevice interaction design (spec: docs handoff RESUME + the extraction
 * workflow wf_96328a4a). Functionality-first; appearance intentionally minimal.
 *
 * LOAD-BEARING RULES carried verbatim from the original (do not "simplify"):
 *  - Level slider THROTTLES to 300ms during drag AND dedups equal values, but
 *    ALWAYS sends the final value on release (unthrottled). A dropped final
 *    tick otherwise leaves the bulb one step off where the finger let go.
 *  - setLevel is preceded by a ONE-TIME 'on' per drag/tap — but ONLY if the
 *    device has the Switch capability (MISS 1/2 from adversarial verify: a
 *    SwitchLevel-only dimmer has no 'on'/'off' command; turning it off means
 *    setLevel(0), and it must never be pre-sent 'on').
 *  - setLevel arg is a BARE integer clamped 0-99. setColor is ONE map
 *    {hue,saturation,level} on the HUBITAT 0-100 scale (NOT 0-360); the Matter
 *    client translates 0-100 -> 0-254 natively.
 *  - Optimistic local paint during drag; the roster poll reconciles.
 *
 * RGB here is a hue slider + saturation slider + preset chips (pure Views +
 * PanResponder) rather than a 2-D wheel — the wheel needs react-native-skia/svg,
 * which are not in the frozen node_modules. This is the functional RGB picker;
 * the wheel is a later enhancement once the workspace dep model lands.
 */
import { useMemo, useRef, useState } from 'react';
import {
  LayoutChangeEvent,
  Modal,
  PanResponder,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { colors, radius, space } from '../../shared/tokens';
import { PanelApi } from '../core/panel-api';
import type { Tile } from '../core/panel-types';

const api = new PanelApi('');
const THROTTLE_MS = 300;

function clamp(n: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, n));
}

/** RN's DimensionValue accepts a `${number}%` literal but not a plain string;
 *  this produces the right literal type for percentage style values. */
function pct(n: number): `${number}%` {
  return `${n}%` as `${number}%`;
}

/** 9 presets — hue/saturation on the Hubitat 0-100 scale (verbatim from the
 *  original preset table). Chips SHOW their real colour (that's the feature);
 *  each is also text-labelled so identification never relies on hue alone. */
const PRESETS: { name: string; hue: number; sat: number }[] = [
  { name: 'Red', hue: 0, sat: 100 },
  { name: 'Orange', hue: 10, sat: 100 },
  { name: 'Yellow', hue: 15, sat: 100 },
  { name: 'Green', hue: 33, sat: 100 },
  { name: 'Cyan', hue: 50, sat: 100 },
  { name: 'Blue', hue: 66, sat: 100 },
  { name: 'Purple', hue: 75, sat: 100 },
  { name: 'Pink', hue: 83, sat: 100 },
  { name: 'White', hue: 0, sat: 0 },
];

/** hue/sat (0-100) -> css hsl() string for a preview swatch. Degrees are
 *  display-only: cssHue = hue * 3.6. */
function swatch(hue: number, sat: number): string {
  return `hsl(${Math.round(hue * 3.6)}, ${sat}%, 50%)`;
}

export function DimmerModal({
  tile,
  visible,
  onClose,
}: {
  tile: Tile;
  visible: boolean;
  onClose: () => void;
}) {
  const hasSwitch = useMemo(
    () => tile.capabilities.some((c) => c.toLowerCase() === 'switch'),
    [tile.capabilities],
  );
  const isColor = tile.tile_type === 'color';

  // Optimistic local state (reconciled by the roster poll after close).
  const startLevel = Number(tile.attributes['level'] ?? 0);
  const [level, setLevel] = useState<number>(
    (tile.primary_value ?? 'off').toLowerCase() === 'on' ? startLevel : 0,
  );
  const [hue, setHue] = useState<number>(Number(tile.attributes['hue'] ?? 0));
  const [sat, setSat] = useState<number>(Number(tile.attributes['saturation'] ?? 100));

  // CRITICAL: PanResponder.create runs ONCE (useRef), so its closures capture
  // the FIRST render's state forever. Mirror every live value into a ref and
  // have the pan handlers read/write the ref — otherwise release sends the
  // stale initial value and the color sliders cross-reference stale hue/sat.
  const levelRef = useRef<number>(
    (tile.primary_value ?? 'off').toLowerCase() === 'on' ? startLevel : 0,
  );
  const hueRef = useRef<number>(Number(tile.attributes['hue'] ?? 0));
  const satRef = useRef<number>(Number(tile.attributes['saturation'] ?? 100));
  const setLvl = (v: number) => { levelRef.current = v; setLevel(v); };
  const setH = (v: number) => { hueRef.current = v; setHue(v); };
  const setS = (v: number) => { satRef.current = v; setSat(v); };

  // Per-drag send bookkeeping (refs so PanResponder closures see live values).
  const lastSentAt = useRef(0);
  const lastSentLevel = useRef(-1);
  const sentOnThisDrag = useRef(false);
  const levelTrackH = useRef(1);

  // --- level send (the on-then-setLevel sequence, Switch-cap-aware) ---
  const sendLevel = async (raw: number) => {
    const lvl = clamp(Math.round(raw), 0, 99); // setLevel ceiling is 99
    if (lvl > 0 && hasSwitch && !sentOnThisDrag.current) {
      sentOnThisDrag.current = true;
      try { await api.command(tile.id, 'on'); } catch { /* poll reconciles */ }
    }
    lastSentLevel.current = lvl;
    lastSentAt.current = Date.now();
    try { await api.command(tile.id, 'setLevel', lvl); } catch { /* poll reconciles */ }
  };

  const throttledSendLevel = (lvl: number) => {
    const now = Date.now();
    if (now - lastSentAt.current >= THROTTLE_MS && lvl !== lastSentLevel.current) {
      void sendLevel(lvl);
    }
  };

  // Vertical slider: top = 99, bottom = 0 (inverted fill).
  const levelPan = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: () => true,
      onPanResponderGrant: (e) => {
        sentOnThisDrag.current = false;
        const lvl = clamp(
          Math.round((1 - e.nativeEvent.locationY / levelTrackH.current) * 99),
          0, 99,
        );
        setLvl(lvl);
        throttledSendLevel(lvl);
      },
      onPanResponderMove: (e) => {
        const lvl = clamp(
          Math.round((1 - e.nativeEvent.locationY / levelTrackH.current) * 99),
          0, 99,
        );
        setLvl(lvl);
        throttledSendLevel(lvl);
      },
      onPanResponderRelease: () => { void sendLevel(levelRef.current); }, // ALWAYS send final
      onPanResponderTerminate: () => { void sendLevel(levelRef.current); },
    }),
  ).current;

  // --- horizontal hue/sat sliders (color only) ---
  const hueTrackW = useRef(1);
  const satTrackW = useRef(1);
  const lastColorAt = useRef(0);
  const lastColorKey = useRef('');

  const sendColor = async (h: number, s: number) => {
    const map = { hue: clamp(Math.round(h), 0, 100), saturation: clamp(Math.round(s), 0, 100), level: clamp(Math.round(levelRef.current) || 100, 0, 100) };
    lastColorKey.current = `${map.hue}:${map.saturation}`;
    lastColorAt.current = Date.now();
    try { await api.command(tile.id, 'setColor', map); } catch { /* poll reconciles */ }
  };
  const throttledSendColor = (h: number, s: number) => {
    const now = Date.now();
    const key = `${Math.round(h)}:${Math.round(s)}`;
    if (now - lastColorAt.current >= THROTTLE_MS && key !== lastColorKey.current) {
      void sendColor(h, s);
    }
  };

  const makeHorizPan = (
    trackW: React.MutableRefObject<number>,
    set: (v: number) => void,
    onDrag: (v: number) => void,
    onRelease: (v: number) => void,
  ) =>
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: () => true,
      onPanResponderGrant: (e) => {
        const v = clamp(Math.round((e.nativeEvent.locationX / trackW.current) * 100), 0, 100);
        set(v); onDrag(v);
      },
      onPanResponderMove: (e) => {
        const v = clamp(Math.round((e.nativeEvent.locationX / trackW.current) * 100), 0, 100);
        set(v); onDrag(v);
      },
      onPanResponderRelease: (e) => {
        const v = clamp(Math.round((e.nativeEvent.locationX / trackW.current) * 100), 0, 100);
        set(v); onRelease(v);
      },
    });

  const huePan = useRef(
    makeHorizPan(hueTrackW, setH, (v) => throttledSendColor(v, satRef.current), (v) => void sendColor(v, satRef.current)),
  ).current;
  const satPan = useRef(
    makeHorizPan(satTrackW, setS, (v) => throttledSendColor(hueRef.current, v), (v) => void sendColor(hueRef.current, v)),
  ).current;

  const applyPreset = (h: number, s: number) => {
    setH(h); setS(s);
    void sendColor(h, s);
  };

  const fillPct = pct(clamp(level, 0, 99));

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose}>
        {/* stop propagation: taps inside the sheet must not close it */}
        <Pressable style={styles.sheet} onPress={() => {}}>
          <Text style={styles.title} numberOfLines={1}>{tile.label}</Text>

          <View style={styles.row}>
            {/* vertical level slider */}
            <View
              style={styles.levelTrack}
              onLayout={(ev: LayoutChangeEvent) => { levelTrackH.current = ev.nativeEvent.layout.height || 1; }}
              {...levelPan.panHandlers}
            >
              <View style={[styles.levelFill, { height: fillPct }]} />
              <Text style={styles.levelLabel}>{clamp(level, 0, 99)}%</Text>
            </View>

            <View style={styles.stepCol}>
              <Pressable style={styles.stepBtn} onPress={() => void sendLevel(Math.min(99, level + 5))}>
                <Text style={styles.stepTxt}>＋</Text>
              </Pressable>
              <Pressable style={styles.stepBtn} onPress={() => void sendLevel(Math.max(0, level - 5))}>
                <Text style={styles.stepTxt}>－</Text>
              </Pressable>
            </View>
          </View>

          {isColor ? (
            <View style={styles.colorSection}>
              <View style={styles.colorHeader}>
                <Text style={styles.sectionLabel}>Color</Text>
                <View style={[styles.preview, { backgroundColor: swatch(hue, sat) }]} />
              </View>

              <Text style={styles.miniLabel}>Hue</Text>
              <View style={styles.hTrack} onLayout={(e) => { hueTrackW.current = e.nativeEvent.layout.width || 1; }} {...huePan.panHandlers}>
                <View style={[styles.hKnob, { left: pct(hue) }]} />
              </View>

              <Text style={styles.miniLabel}>Saturation</Text>
              <View style={styles.hTrack} onLayout={(e) => { satTrackW.current = e.nativeEvent.layout.width || 1; }} {...satPan.panHandlers}>
                <View style={[styles.hKnob, { left: pct(sat) }]} />
              </View>

              <View style={styles.presets}>
                {PRESETS.map((p) => (
                  <Pressable key={p.name} style={styles.chipWrap} onPress={() => applyPreset(p.hue, p.sat)}>
                    <View style={[styles.chip, { backgroundColor: swatch(p.hue, p.sat) }]} />
                    <Text style={styles.chipTxt}>{p.name}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : null}

          <Pressable style={styles.closeBtn} onPress={onClose}>
            <Text style={styles.closeTxt}>Done</Text>
          </Pressable>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.55)', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: radius.modal,
    borderTopRightRadius: radius.modal,
    padding: space.lg,
    gap: space.md,
  },
  title: { color: colors.text, fontSize: 18, fontWeight: '700' },
  row: { flexDirection: 'row', gap: space.lg, alignItems: 'stretch' },
  levelTrack: {
    flex: 1,
    height: 200,
    backgroundColor: colors.bg,
    borderRadius: radius.chip,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    overflow: 'hidden',
    justifyContent: 'flex-end',
  },
  levelFill: { width: '100%', backgroundColor: colors.accent },
  levelLabel: {
    position: 'absolute', top: 8, alignSelf: 'center',
    color: colors.text, fontSize: 15, fontWeight: '700',
  },
  stepCol: { justifyContent: 'space-between', width: 56 },
  stepBtn: {
    backgroundColor: colors.bg, borderRadius: radius.chip, paddingVertical: 18,
    alignItems: 'center', borderWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
  },
  stepTxt: { color: colors.text, fontSize: 22, fontWeight: '700' },
  colorSection: { gap: space.sm, marginTop: space.sm },
  colorHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sectionLabel: { color: colors.textDim, fontSize: 14, fontWeight: '600' },
  preview: { width: 32, height: 32, borderRadius: 8, borderWidth: 1, borderColor: colors.border },
  miniLabel: { color: colors.textFaint, fontSize: 12, marginTop: space.xs },
  hTrack: {
    height: 28, backgroundColor: colors.bg, borderRadius: 14, justifyContent: 'center',
    borderWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
  },
  hKnob: {
    position: 'absolute', width: 20, height: 20, borderRadius: 10, marginLeft: -10,
    backgroundColor: colors.accent, borderWidth: 2, borderColor: colors.text,
  },
  presets: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, marginTop: space.sm },
  chipWrap: { alignItems: 'center', width: 60, gap: 2 },
  chip: { width: 40, height: 28, borderRadius: 8, borderWidth: 1, borderColor: colors.border },
  chipTxt: { color: colors.textDim, fontSize: 10 },
  closeBtn: {
    backgroundColor: colors.accent, borderRadius: radius.chip, paddingVertical: 12,
    alignItems: 'center', marginTop: space.sm,
  },
  closeTxt: { color: colors.bg, fontWeight: '700' },
});
