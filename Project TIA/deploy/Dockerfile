# TIA container image: one image, three commands (server, worker, migrate).
#   docker build -f deploy/Dockerfile -t tia:<version> .
# Status: written and statically checked (tests/test_deploy.py); NOT built or run here - no Docker on the development machine.

FROM python:3.11-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY tia ./tia
RUN python -m pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist .

FROM python:3.11-slim-bookworm
# git is the only system dependency: TIA reads repositories with it and never executes their code
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 tia \
 && useradd --system --uid 10001 --gid tia --home-dir /var/lib/tia --shell /usr/sbin/nologin tia \
 && mkdir -p /var/lib/tia/repos \
 && chown -R tia:tia /var/lib/tia
COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir "$(ls /tmp/*.whl)[api]" && rm -f /tmp/*.whl

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIA_HOST=0.0.0.0 \
    TIA_PORT=8080 \
    TIA_WORKSPACE=/var/lib/tia/repos \
    TIA_LOG_FORMAT=json \
    TIA_AUTH_REQUIRED=true \
    HOME=/var/lib/tia
USER 10001:10001
WORKDIR /var/lib/tia
VOLUME ["/var/lib/tia/repos"]
EXPOSE 8080 9090
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["python", "-m"]
CMD ["tia.server"]
