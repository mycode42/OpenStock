# OpenStock web app and monitor. The server binds to 127.0.0.1 only, so run the container with host networking.
# Keep the database in its own volume (not on top of /app/database) and mount Algo data read-only:
#   podman run --network host --env-file .env -e DATABASE_PATH=/data/openstock.db \
#     -v openstock-data:/data -v <path-to-repo>/algo-trader/data:/app/algo-trader/data:ro localhost/openstock:verify
# Secrets and databases are excluded from the image by .dockerignore; supply them at run time.
FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 make g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY . .

EXPOSE 3000
CMD ["node", "server.js"]
