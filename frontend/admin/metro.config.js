// Metro config for the admin app in the frontend/ monorepo.
//
// admin/ and tiles/ are sibling Expo apps that both import frontend/shared/ and
// resolve dependencies from the hoisted frontend/node_modules. Metro must watch
// the parent (for shared/) and know where node_modules live. Identical pattern
// to tiles/metro.config.js — keep the two in lockstep.
const { getDefaultConfig } = require('expo/metro-config');
const path = require('path');

const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, '..'); // frontend/

const config = getDefaultConfig(projectRoot);

// Watch the whole frontend/ so imports of ../shared resolve + hot-reload.
config.watchFolders = [workspaceRoot];

// Resolve deps from the app first, then the hoisted frontend/node_modules.
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, 'node_modules'),
  path.resolve(workspaceRoot, 'node_modules'),
];

// frontend/node_modules is (temporarily) a symlink to the TILES install; the
// durable npm-workspaces install replaces it as a discrete coordinated step.
config.resolver.unstable_enableSymlinks = true;

module.exports = config;
