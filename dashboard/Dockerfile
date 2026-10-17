# syntax=docker/dockerfile:1.7
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build -- --configuration production

FROM nginx:1.27-alpine
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 10-resolver.envsh /docker-entrypoint.d/10-resolver.envsh
COPY --from=build /app/dist/dashboard/browser /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
