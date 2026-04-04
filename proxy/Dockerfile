FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx
COPY sse_patch.py .
CMD ["python", "sse_patch.py"]
