/**
 * Runs before every Jest test file.
 *
 * Registers `@testing-library/jest-dom`'s DOM matchers (`toBeInTheDocument`,
 * `toHaveTextContent`, ...). React Testing Library's automatic per-test
 * `cleanup` is already wired by its own Jest integration, so nothing here has
 * to unmount components.
 */

import { TextDecoder, TextEncoder } from 'node:util';

import '@testing-library/jest-dom';

// react-router v7 references `TextEncoder`/`TextDecoder` at module load, and
// Jest's jsdom environment does not define them (they live on Node's `util`).
// Provide them before any test imports the router.
if (typeof globalThis.TextEncoder === 'undefined') {
  globalThis.TextEncoder = TextEncoder;
}
if (typeof globalThis.TextDecoder === 'undefined') {
  globalThis.TextDecoder = TextDecoder;
}
