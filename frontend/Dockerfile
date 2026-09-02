FROM node:22-alpine AS build
WORKDIR /app

# VITE_API_BASE is baked into the bundle at build time.
# Leave it empty to have the SPA call the same origin it is served from
# (the right choice when a reverse proxy fronts both the UI and the API).
ARG VITE_API_BASE=""
ENV VITE_API_BASE=$VITE_API_BASE

COPY package.json package-lock.json* ./
RUN npm ci --silent || npm install --silent
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
