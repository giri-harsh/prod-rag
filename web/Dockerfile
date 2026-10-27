# Web client: Vite SPA build, then a small Node BFF (no runtime npm dependencies) that serves dist/ and proxies /api/* to the RAG API.
FROM node:22 AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json tsconfig.node.json vite.config.ts ./
COPY public ./public
COPY src ./src
RUN npm run build

FROM node:22-slim
ENV NODE_ENV=production BFF_HOST=0.0.0.0 BFF_PORT=8787
WORKDIR /web
COPY package.json ./
COPY server ./server
COPY --from=build /web/dist ./dist
USER node
EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8787/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["npm", "start"]
