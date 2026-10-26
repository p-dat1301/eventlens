FROM oven/bun:1.4.0 AS build

WORKDIR /app
COPY src/frontend/web/package.json src/frontend/web/bun.lock ./
RUN bun install --frozen-lockfile
COPY src/frontend/web/ ./
RUN bun run build

FROM nginx:1.27-alpine
COPY src/frontend/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
