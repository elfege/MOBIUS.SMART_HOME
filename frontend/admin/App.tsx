/**
 * admin/App.tsx — the admin app root. Deliberately THIN: status bar + the
 * current screen. Navigation between admin sections (instances, hubs, Matter,
 * settings, logs) arrives with the next milestone; the proto is the instances
 * screen, full-bleed. All real logic lives in screens/ and core/ — this file
 * should never grow business logic (fanatic-modularization directive).
 */

import { StatusBar } from 'expo-status-bar';

import { InstancesScreen } from './screens/InstancesScreen';

export default function App() {
  return (
    <>
      <StatusBar style="light" />
      <InstancesScreen />
    </>
  );
}
