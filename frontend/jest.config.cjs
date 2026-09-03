/**
 * Jest configuration for the VaultShield_Dashboard.
 *
 * design.md's technology stack names Jest + React Testing Library for
 * frontend unit tests and fast-check for property tests over pure rendering
 * helpers; both run through this one config, since a fast-check property is
 * an ordinary Jest test with a generated input space.
 *
 * `jsdom` rather than `node`: every test that touches a component needs a
 * DOM, and `src/api.js`'s default token store reads `window.localStorage`.
 *
 * `.cjs` for the same reason as `babel.config.cjs` — `package.json` is
 * `"type": "module"`.
 */

module.exports = {
  testEnvironment: 'jsdom',
  // `@testing-library/jest-dom` matchers and the per-test cleanup RTL needs.
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
  moduleNameMapper: {
    // Tailwind's stylesheet is a build-time concern. Under Jest a CSS import
    // would be parsed as JavaScript, so it is swapped for a proxy that
    // returns each requested class name as a string.
    '\\.(css|sass|scss)$': 'identity-obj-proxy',
  },
  // Tests live next to the code they cover, as `*.test.js` / `*.test.jsx`.
  testMatch: ['<rootDir>/src/**/*.test.js?(x)'],
  clearMocks: true,
};
