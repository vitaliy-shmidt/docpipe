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

CMD ["uvicorn", "system.main:app", "--host", "0.0.0.0", "--port", "8000"]
