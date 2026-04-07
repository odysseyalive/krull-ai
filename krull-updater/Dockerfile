# krull-updater: idle sidecar that listens for update requests from krull-home.
#
# Why this exists as a separate container:
# When krull-home triggers an update, it rebuilds itself. A subprocess
# spawned from inside krull-home would die with the parent container
# (no init reparenting inside docker). The updater lives in its own
# container so it survives the rebuild and can drive it to completion.
FROM alpine:latest

RUN apk add --no-cache \
    bash \
    git \
    curl \
    docker-cli \
    docker-cli-compose

# Modern git refuses to operate on a repo owned by a different UID
# (CVE-2022-24765 / "dubious ownership"). The repo on the host is
# owned by the user running krull, but this container runs as root.
# Whitelist /workspace so git treats it as trustworthy.
RUN git config --system --add safe.directory /workspace

WORKDIR /workspace
COPY watch.sh /usr/local/bin/watch.sh
RUN chmod +x /usr/local/bin/watch.sh

CMD ["/usr/local/bin/watch.sh"]
