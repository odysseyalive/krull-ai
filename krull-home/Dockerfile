# Stage 1: build frontend (Vite) and compile server (tsc)
FROM node:lts-alpine AS build
WORKDIR /app
COPY package.json ./
RUN yarn install
COPY tsconfig.json tsconfig.server.json vite.config.ts index.html ./
COPY src ./src
COPY public ./public
COPY server ./server
RUN yarn build && yarn build:server

# Stage 2: minimal runtime — Node + docker CLI for restarting siblings
FROM node:lts-alpine
WORKDIR /app
RUN apk add --no-cache docker-cli bash curl
COPY package.json ./
RUN yarn install --production
COPY --from=build /app/dist ./dist
COPY --from=build /app/dist-server ./dist-server
EXPOSE 8000
ENV PORT=8000
ENV KRULL_REPO=/workspace
CMD ["node", "dist-server/index.js"]
