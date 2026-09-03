/**
 * Vite configuration for the VaultShield_Dashboard.
 *
 * ### The API is reached through a dev-server proxy, not CORS
 *
 * `backend/vaultshield_backend/settings.py` installs no CORS middleware, so
 * a browser on the dev server's origin cannot call the API cross-origin. The
 * proxy below removes the need for it: the dashboard requests relative paths
 * (`/api/projects/`), the dev server forwards them to the backend, and every
 * request is same-origin from the browser's point of view. That also means
 * `src/api.js` defaults to an empty base URL — see `src/config.js`.
 *
 * `/media` is proxied alongside `/api` because uploaded Screenshots are
 * served off `MEDIA_URL` (Task 16.7's screenshot ingest writes them there),
 * and the Screenshots tab will load them as plain `<img src>` URLs.
 *
 * The target is an environment variable because the same config runs in two
 * places, per design.md's compose topology: on a developer's host the
 * backend is a port-forwarded `localhost:8000`, while inside the compose
 * network the `frontend` service reaches the `web` service by name
 * (`VITE_API_PROXY_TARGET=http://web:8000`, Requirement 24.3).
 *
 * ### Babel config files are deliberately ignored here
 *
 * `babel.config.cjs` exists for Jest and compiles down to *Node*
 * (`preset-env` with `targets: { node: 'current' }`). Letting the browser
 * build pick that up would target the wrong runtime, so the React plugin is
 * told not to read project Babel config at all; it keeps using its own
 * transform plus react-refresh.
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000';

const proxy = {
  '/api': { target: API_PROXY_TARGET, changeOrigin: true },
  '/media': { target: API_PROXY_TARGET, changeOrigin: true },
};

export default defineConfig({
  plugins: [
    react({ babel: { babelrc: false, configFile: false } }),
    tailwindcss(),
  ],
  server: {
    port: 5173,
    // Bind on all interfaces so the compose `frontend` service is reachable
    // from the host; the default (localhost only) is invisible outside the
    // container.
    host: true,
    proxy,
  },
  // `vite preview` serves the production build, and it needs the same proxy
  // for the built dashboard to reach the API without CORS.
  preview: { port: 4173, host: true, proxy },
});
