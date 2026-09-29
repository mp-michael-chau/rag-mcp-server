# Next.js web UI (standalone output — see next.config.ts).
# API_ORIGIN is baked into the rewrite at build time; docker-compose passes
# http://api:8100. Rebuild (docker compose build web) if it changes.
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG API_ORIGIN=http://127.0.0.1:8100
ENV API_ORIGIN=${API_ORIGIN}
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
