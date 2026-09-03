/**
 * Browser entry point.
 *
 * The only module that reads `import.meta.env`, and the only one Jest never
 * loads — see `src/config.js` for why those two facts belong together. It
 * applies the resolved base URL to the shared API client before the first
 * render, so no view can issue a request against a stale default.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App.jsx';
import api from './api.js';
import { resolveApiBaseUrl } from './config.js';
import './index.css';

api.configure({ baseUrl: resolveApiBaseUrl(import.meta.env) });

const container = document.getElementById('root');
if (!container) {
  throw new Error('Missing #root element in index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
