/**
 * components/LogoMark.tsx — the MOBIUS.TILES 2x2 grid logo as pure RN Views.
 *
 * Faithful to the ORIGINAL TILES logo.svg (operator's brand: three #4A9FD8
 * blue tiles + one #E89B3C accent tile, bottom-right): the accent is carried
 * by POSITION, not needed for any state signal, so it is CVD-fine as brand art.
 *
 * Rendered as Views (not the SVG asset) on purpose: zero new dependencies
 * (react-native-svg would need the frozen node_modules to change) and it works
 * identically on web + native. The original .svg still ships in public/ for
 * anything that wants the file itself (e.g. an external link or docs).
 */
import { StyleSheet, View } from 'react-native';

const BLUE = '#4A9FD8';
const ORANGE = '#E89B3C';

export function LogoMark({ size = 28 }: { size?: number }) {
  const tile = (size - 4) / 2; // 2px gutter between tiles
  const r = Math.max(2, size / 12);
  const t = (bg: string) => [
    styles.tile,
    { width: tile, height: tile, borderRadius: r, backgroundColor: bg },
  ];
  return (
    <View style={{ width: size, height: size, justifyContent: 'space-between' }}>
      <View style={styles.row}>
        <View style={t(BLUE)} />
        <View style={t(BLUE)} />
      </View>
      <View style={styles.row}>
        <View style={t(BLUE)} />
        <View style={t(ORANGE)} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', justifyContent: 'space-between' },
  tile: {},
});
