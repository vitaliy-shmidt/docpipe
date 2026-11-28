# Reproducible release build (see docs/operations/monitoring-operations.md §2):
# - base image pinned by digest (the tag is informational; Docker uses the digest),
# - Python packages fixed by constraints.txt (pip freeze of the release image),
# - build metadata passed as build arguments, available to the monitoring
#   overview (environment) and as OCI labels.
# Update the base digest and constraints.txt deliberately, never implicitly.
FROM python:3.12.16-slim@sha256:5b5fe805d367f8f5a359ded6ab2652b7f9a206453ee6ee789c0045ba6ec15016

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY system ./system

RUN useradd --create-home --uid 1000 docpipe \
    && chown -R docpipe:docpipe /app
USER docpipe

# Build metadata (all optional; empty means "unknown" in the monitoring overview):
#   docker build --build-arg DOCPIPE_BUILD_COMMIT=$(git rev-parse HEAD) \
#                --build-arg DOCPIPE_IMAGE_TAG=<tag> \
#                --build-arg DOCPIPE_BUILD_LOCAL_CHANGES=false -t docpipe:<tag> .
ARG DOCPIPE_BUILD_COMMIT=""
ARG DOCPIPE_IMAGE_TAG=""
ARG DOCPIPE_BUILD_LOCAL_CHANGES=""
ARG DOCPIPE_BUILD_LOCAL_PATCHES=""
ENV DOCPIPE_BUILD_COMMIT=${DOCPIPE_BUILD_COMMIT} \
    DOCPIPE_IMAGE_TAG=${DOCPIPE_IMAGE_TAG} \
    DOCPIPE_BUILD_LOCAL_CHANGES=${DOCPIPE_BUILD_LOCAL_CHANGES} \
    DOCPIPE_BUILD_LOCAL_PATCHES=${DOCPIPE_BUILD_LOCAL_PATCHES}
LABEL org.opencontainers.image.source="https://github.com/vitaliy-shmidt/docpipe" \
      org.opencontainers.image.revision="${DOCPIPE_BUILD_COMMIT}" \
      org.opencontainers.image.version="${DOCPIPE_IMAGE_TAG}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)" || exit 1

# --no-proxy-headers: Uvicorn must not rewrite the client address / scheme
# from X-Forwarded-* itself (by default it does so for 127.0.0.1). Forwarded
# headers are evaluated in exactly one place - DocPipe's monitoring web
# interface, and only from configured trusted proxies (hardening H5). The
# document API does not use the client address or scheme.
CMD ["uvicorn", "system.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
