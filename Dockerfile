# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="pocket-sync" \
      org.opencontainers.image.description="Syncs Pocket recordings into a local NAS archive" \
      org.opencontainers.image.source="https://github.com/ebialobrzeski/pocket_sync"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv
# Default mount points; compose may override the user with `user:` to match the NAS shares.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/meta/pocket /data/state /audio \
    && chown -R 1000:1000 /data /audio
USER 1000:1000
WORKDIR /data
# Web UI (`web` command); the sync loop does not listen on any port.
EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=30s --start-period=5m --retries=2 \
    CMD ["python", "-m", "pocket_sync", "healthcheck"]
ENTRYPOINT ["python", "-m", "pocket_sync"]
CMD ["run"]
