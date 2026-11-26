FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY system ./system

RUN useradd --create-home --uid 1000 docpipe \
    && chown -R docpipe:docpipe /app
USER docpipe

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)" || exit 1

# --no-proxy-headers: Uvicorn must not rewrite the client address / scheme
# from X-Forwarded-* itself (by default it does so for 127.0.0.1). Forwarded
# headers are evaluated in exactly one place - DocPipe's monitoring web
# interface, and only from configured trusted proxies (hardening H5). The
# document API does not use the client address or scheme.
CMD ["uvicorn", "system.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
