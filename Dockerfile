FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS dashboard-build

ARG RAILWAY_GIT_COMMIT_SHA
ARG VITE_SENTRY_DSN
ARG VITE_SENTRY_ENVIRONMENT=qualification
ENV VITE_SENTRY_DSN="$VITE_SENTRY_DSN" \
    VITE_SENTRY_ENVIRONMENT="$VITE_SENTRY_ENVIRONMENT" \
    VITE_SENTRY_RELEASE="$RAILWAY_GIT_COMMIT_SHA"
WORKDIR /src/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --fund=false
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS python-build

ARG RAILWAY_GIT_COMMIT_SHA
ARG MAAIS_SOURCE_CLEAN
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/maais/.venv
RUN python -m pip install --no-cache-dir "uv==0.11.16"
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-editable --no-install-project
COPY maais/ ./maais/
COPY alembic/ ./alembic/
COPY alembic.ini Dockerfile ./
COPY dashboard/package-lock.json ./dashboard/package-lock.json
COPY --from=dashboard-build /src/dashboard/dist /src/dashboard/dist
RUN uv sync --locked --no-dev --no-editable
RUN test "$MAAIS_SOURCE_CLEAN" = "true" \
    && printf '%s' "$RAILWAY_GIT_COMMIT_SHA" | grep -Eq '^[0-9a-f]{40}$'
RUN mkdir -p /build \
    && uv run maais candidate-descriptor \
        --repository /src \
        --dashboard-dir /src/dashboard/dist \
        --git-sha "$RAILWAY_GIT_COMMIT_SHA" \
        --source-clean "$MAAIS_SOURCE_CLEAN" \
        --output /build/candidate.json

FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ARG RAILWAY_GIT_COMMIT_SHA
LABEL org.opencontainers.image.revision="$RAILWAY_GIT_COMMIT_SHA" \
      io.maais.candidate.schema="1" \
      io.maais.safety.paper-only="true"
ENV HOME=/tmp \
    PATH=/opt/maais/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUN_MODE=paper_live
WORKDIR /app
COPY --from=python-build /opt/maais/.venv /opt/maais/.venv
COPY --from=python-build /src/alembic /app/alembic
COPY --from=python-build /src/alembic.ini /app/alembic.ini
COPY --from=python-build /build/candidate.json /app/candidate.json
COPY --from=dashboard-build /src/dashboard/dist /app/dashboard
RUN rm -rf /usr/local/lib/python3.12/ensurepip \
        /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.12 \
    && chmod -R a-w /app /opt/maais
USER 10001:10001
ENTRYPOINT ["/opt/maais/.venv/bin/maais"]
