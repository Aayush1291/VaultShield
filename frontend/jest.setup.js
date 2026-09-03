/**
 * Runs before every Jest test file.
 *
 * Registers `@testing-library/jest-dom`'s DOM matchers (`toBeInTheDocument`,
 * `toHaveTextContent`, ...). React Testing Library's automatic per-test
 * `cleanup` is already wired by its own Jest integration, so nothing here has
 * to unmount components.
 */

import '@testing-library/jest-dom';
