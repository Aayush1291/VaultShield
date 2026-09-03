/**
 * Babel configuration used by `babel-jest` only.
 *
 * Jest runs each test file as CommonJS in Node, so the ES modules and JSX in
 * `src/` have to be transformed for it. `targets: { node: 'current' }` keeps
 * that transform minimal — it compiles module syntax and JSX and leaves
 * everything Node already supports alone.
 *
 * The browser build does not use this file: `vite.config.js` sets
 * `babelrc: false, configFile: false` on the React plugin precisely so a
 * Node-targeted `preset-env` never reaches the bundle.
 *
 * `.cjs` because `package.json` declares `"type": "module"`, which would
 * otherwise make Babel's `module.exports` an error.
 */

module.exports = {
  presets: [
    ['@babel/preset-env', { targets: { node: 'current' } }],
    // `runtime: 'automatic'` injects the JSX runtime import itself, so no
    // component file needs `import React from 'react'` just to use JSX.
    ['@babel/preset-react', { runtime: 'automatic' }],
  ],
};
