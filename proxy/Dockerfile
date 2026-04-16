FROM python:3.11-slim
WORKDIR /app
# uvicorn[standard] pulls in watchfiles, which --reload needs to detect
# changes efficiently when /app is bind-mounted from the host.
# playwright + chromium power the /fetch endpoint (JS-rendered URL
# fetch); html2text converts rendered HTML → markdown. `playwright
# install --with-deps chromium` pulls chromium itself (~280MB) plus
# the system libs it needs at runtime.
RUN pip install --no-cache-dir \
      fastapi \
      'uvicorn[standard]' \
      httpx \
      patchright \
      html2text \
 && patchright install --with-deps chromium
COPY sse_patch.py .
# --reload watches the working directory for .py changes and restarts
# the app when one is edited. Combined with the ./proxy bind mount in
# docker-compose.yml, this means edits on the host take effect inside
# the container without rebuilding the image. Port falls back to 8081
# but honors the existing PORT env var via shell expansion.
CMD ["sh", "-c", "uvicorn sse_patch:app --host 0.0.0.0 --port ${PORT:-8081} --reload --reload-include=*.py"]
