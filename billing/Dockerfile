FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=optional
COPY tsconfig.json drizzle.config.ts ./
COPY src ./src
COPY drizzle ./drizzle
COPY web ./web
# Heebo for Hebrew receipts (react-pdf embeds it in every PDF)
COPY assets ./assets
ENV NODE_ENV=production PORT=8080
EXPOSE 8080
# migrations run at startup (createContext); reconcile runs as a separate daily job:  npm run reconcile
CMD ["npx", "tsx", "src/server.ts"]
