/**
 * admin/App.tsx — the admin app root. Deliberately THIN: status bar + a switch
 * over the nav store's current view (core/nav.ts — hand-rolled, deps frozen).
 * All real logic lives in screens/ and core/ — this file should never grow
 * business logic (fanatic-modularization directive).
 */

import { StatusBar } from 'expo-status-bar';

import { useNav } from './core/nav';
import { HomeScreen } from './screens/HomeScreen';
import { InstanceDetailScreen } from './screens/InstanceDetailScreen';
import { InstancesScreen } from './screens/InstancesScreen';

export default function App() {
  const current = useNav((s) => s.current);
  return (
    <>
      <StatusBar style="light" />
      {current.view === 'detail' ? (
        <InstanceDetailScreen instanceId={current.instanceId} />
      ) : current.view === 'list' ? (
        <InstancesScreen />
      ) : (
        <HomeScreen />
      )}
    </>
  );
}
