# krull-web-broker — deterministic web-access broker for the Open WebUI → Ollama
# chat path. Speaks the OWUI external search/loader HTTP contracts; routes intent
# to SearXNG + the host playwright-mcp (web_fetch + headed CAPTCHA handoff).
FROM python:3.12-slim

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source.
COPY *.py ./

EXPOSE 8130

# Unbuffered so logs stream to `docker logs` in real time (verify step reads them).
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
