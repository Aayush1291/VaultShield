# Image behind the `frontend` service in the root `docker-compose.yml`
# (design.md: "frontend: React dev/build server").
#
# The Vite dev server, not a production build behind nginx: this compose stack
# exists so a developer can exercise the review loop locally, and the dev
# server is also what applies `vite.config.js`'s `/api` and `/media` proxy to
# `http://web:8000` (Requirement 24.3). A static build would have no proxy and
# would need CORS middleware the backend does not install.

FROM node:22-alpine

WORKDIR /app

# Manifests first so the dependency layer is reused whenever only source
# changes. `npm ci` rather than `npm install`: package-lock.json is committed
# and the container should install exactly it.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# `vite.config.js` already sets `server.port` to 5173 and `server.host` to true
# (all interfaces), the latter specifically so this container is reachable from
# outside it — the default localhost-only bind is invisible across a container
# boundary.
EXPOSE 5173
CMD ["npm", "run", "dev"]
