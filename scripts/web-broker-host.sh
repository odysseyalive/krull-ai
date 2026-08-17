#!/bin/bash
# web-broker-host.sh — OPTIONAL. The default deployment runs playwright-mcp as the
# in-network `krull-playwright` container (see docker-compose.yml), which needs no
# host instance and no ufw rule. Use THIS script only if you want inline headed
# CAPTCHA solving via a HOST-run instance (a headless container can't pop a window
# on your screen) and are willing to add the ufw allowance it prints. If you use
# it, point the broker at it with PLAYWRIGHT_MCP_URL=http://host.docker.internal:8765/mcp.
#
# It brings up the HOST playwright-mcp instance the broker talks to, and checks the
# one host-firewall prerequisite.
#
# The broker (container) reaches this instance over its no-auth loopback
# Streamable-HTTP transport (the LOCAL trust tier: session_* handoff tools —
# incl. session_solve_challenge for inline CAPTCHA solving — are available;
# arbitrary-code tools stay denied). Headed CAPTCHA windows open on the host
# display = your screen, which is exactly why this runs on the host, not in a
# container.
#
# Idempotent: if an instance is already listening on the port, it is left alone.
# Never touches an existing install; only starts a background process from the
# already-built dist.
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PW_DIR="${PLAYWRIGHT_MCP_DIR:-$(dirname "$PROJECT_DIR")/playwright-mcp}"
PORT="${PLAYWRIGHT_MCP_HOST_PORT:-8765}"
# Bind 0.0.0.0 so the docker bridge can reach it. ufw's default-DROP INPUT policy
# keeps the physical NIC closed; the scoped allow rule below opens the port to
# the docker subnets ONLY. Override with PLAYWRIGHT_MCP_HOST_BIND if you prefer
# to pin a specific bridge-gateway IP.
BIND="${PLAYWRIGHT_MCP_HOST_BIND:-0.0.0.0}"
RUN_DIR="$PROJECT_DIR/data/web-broker"
PIDFILE="$RUN_DIR/pw-mcp-host.pid"
LOGFILE="$RUN_DIR/pw-mcp-host.log"

mkdir -p "$RUN_DIR"

port_listening() {
    # Return 0 if something is already listening on $PORT.
    if command -v ss >/dev/null 2>&1; then
        ss -tlnH "sport = :$PORT" 2>/dev/null | grep -q ":$PORT"
    else
        # Fallback: bash /dev/tcp probe.
        (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { exec 3>&- 3<&-; return 0; } || return 1
    fi
}

echo "Checking host playwright-mcp instance (broker backend) on :$PORT ..."

if port_listening; then
    echo "[+] Something is already listening on :$PORT — leaving it (assumed playwright-mcp host instance)."
else
    if [ ! -f "$PW_DIR/dist/index.js" ]; then
        echo "[!] $PW_DIR/dist/index.js not found — playwright-mcp is not built."
        echo "    Build it, then re-run: ( cd \"$PW_DIR\" && npm ci && npm run build )"
    elif ! command -v node >/dev/null 2>&1; then
        echo "[!] node not found — cannot start the host playwright-mcp instance."
    else
        echo "    Starting playwright-mcp (local trust tier, no-auth loopback) -> :$PORT ..."
        # PLAYWRIGHT_MCP_PUBLIC_URL is the transport on-switch; ALLOW_NOAUTH selects
        # the local trust tier (no GitHub OAuth). Both required together.
        # Dedicated browser profile so this instance does NOT collide with a
        # Claude Code stdio playwright-mcp instance over Chromium's SingletonLock
        # (shared profile => second instance's web_fetch reads back `blocked`).
        PLAYWRIGHT_MCP_PUBLIC_URL="http://localhost:$PORT" \
        PLAYWRIGHT_MCP_ALLOW_NOAUTH=1 \
        PLAYWRIGHT_MCP_PORT="$PORT" \
        PLAYWRIGHT_MCP_BIND="$BIND" \
        PLAYWRIGHT_MCP_PROFILE_DIR="${PLAYWRIGHT_MCP_PROFILE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/playwright-mcp/profile-broker}" \
            nohup node "$PW_DIR/dist/index.js" >"$LOGFILE" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 2
        if port_listening; then
            echo "[+] playwright-mcp host instance started (pid $(cat "$PIDFILE"), log: $LOGFILE)."
        else
            echo "[!] playwright-mcp host instance did not come up — see $LOGFILE"
        fi
    fi
fi

# --- ufw prerequisite: allow the docker bridge to reach the host instance ---
# ufw's default INPUT policy is DROP on many setups. Container->host traffic
# (broker -> host.docker.internal:$PORT) hits the INPUT chain, NOT the DOCKER-USER
# forward chain the ufw-docker integration manages — so it is dropped even with
# ufw-docker installed. We DETECT and INSTRUCT rather than sudo silently: this is
# a host firewall change and it is the operator's call to apply it.
UFW_RULE="from 172.16.0.0/12 to any port $PORT proto tcp"
UFW_CMD="sudo ufw allow $UFW_RULE comment 'krull-web-broker -> host playwright-mcp'"
if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-active ufw 2>/dev/null)" = "active" ]; then
    if grep -qs "dpt:$PORT" /etc/ufw/user.rules 2>/dev/null || grep -qs "port $PORT" /etc/ufw/user.rules 2>/dev/null; then
        echo "[+] ufw already has a rule for port $PORT — broker should reach the host instance."
    else
        echo ""
        echo "[!] ACTION REQUIRED — ufw is active with a default-DROP INPUT policy."
        echo "    The krull-web-broker container cannot reach the host playwright-mcp"
        echo "    instance until you allow the docker bridge subnets to port $PORT:"
        echo ""
        echo "      $UFW_CMD"
        echo ""
        echo "    (Scoped to RFC1918 172.16/12 = docker bridges only; the physical NIC"
        echo "     stays closed under the default DROP policy. Without this, web search"
        echo "     still works via SearXNG, but full-page reads / CAPTCHA handoff return"
        echo "     honest 'unavailable' markers instead of live page content.)"
    fi
fi
