FROM oven/bun:1 AS deps

WORKDIR /app

ENV PYTHON=/usr/bin/python3

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

FROM deps AS build

COPY . .
RUN bun run build

FROM node:22-trixie-slim AS prod-deps

WORKDIR /app

ENV PYTHON=/usr/bin/python3

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /usr/local/bin/bun /usr/local/bin/bun
COPY package.json bun.lock ./
RUN bun install --production --frozen-lockfile --ignore-scripts \
    && npm rebuild better-sqlite3

FROM node:22-trixie-slim AS runtime

WORKDIR /app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000 \
    CHATGAME_OPENAI_BASE_URL=http://chatgpt-api:8000/v1 \
    CHATGAME_OPENAI_API_KEY=local-dev-key \
    CHATGAME_AI_MODE=live \
    CHATGAME_DB_PATH=/data/chatgame.sqlite \
    CHATGAME_IMAGE_DIR=/data/images

COPY package.json ./
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=build /app/build ./build

RUN mkdir -p /data/images

EXPOSE 3000

CMD ["node", "build/index.js"]
