FROM node:20-alpine AS builder
WORKDIR /app

# Copy workspace root for shared deps
COPY package.json ./
COPY packages/gateway/package.json packages/gateway/
COPY packages/shared/ packages/shared/

# Install dependencies
WORKDIR /app/packages/gateway
RUN npm install --production=false

# Copy source
COPY packages/gateway/src/ src/
COPY packages/gateway/tsconfig.json .

# Build
RUN npx tsc

# ── Production stage ──
FROM node:20-alpine
WORKDIR /app

COPY --from=builder /app/packages/gateway/package.json .
RUN npm install --production
COPY --from=builder /app/packages/gateway/dist/ dist/
COPY --from=builder /app/packages/shared/ ../shared/

ENV NODE_ENV=production
EXPOSE 8741
CMD ["node", "dist/server.js"]
