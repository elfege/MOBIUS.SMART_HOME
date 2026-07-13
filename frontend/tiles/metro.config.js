// Metro config for the tiles app in the frontend/ monorepo.
//
// tiles/ and admin/ are sibling Expo apps that both import frontend/shared/ and
// resolve dependencies from the hoisted frontend/node_modules. Metro must watch
// the parent (for shared/) and know where node_modules live.
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

// The seed's node_modules is a symlink (frontend/node_modules -> the TILES
// install); allow metro to follow it.
config.resolver.unstable_enableSymlinks = true;

module.exports = config;
