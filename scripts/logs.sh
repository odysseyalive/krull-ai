#!/bin/bash
# krull logs — View and search SSE proxy session logs
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"
LOG_FILE="$LOG_DIR/proxy.jsonl"

usage() {
    echo "Usage: ./krull logs <subcommand> [args]"
    echo ""
    echo "Subcommands:"
    echo "  tail                  Live tail of proxy logs (Ctrl-C to stop)"
    echo "  last [N]              Show last N lines (default 50)"
    echo "  session [ID]          Show logs for a session (default: current)"
    echo "  errors [N]            Show last N error/warn entries (default 20)"
    echo "  search <pattern>      Search logs by grep pattern"
    echo "  filters [N]           Show last N filter injection logs (default 30)"
    echo "  requests [N]          Show last N request summaries (default 20)"
    echo "  sessions              List recent sessions with request counts"
    echo "  clear                 Archive and clear the log file"
    echo ""
    echo "Examples:"
    echo "  ./krull logs tail"
    echo "  ./krull logs session 20260412T143000-12345"
    echo "  ./krull logs errors"
    echo "  ./krull logs search 'Kiwix error'"
    echo "  ./krull logs filters"
}

check_log() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "No log file found at $LOG_FILE"
        echo "Start krull-claude to generate logs."
        exit 1
    fi
}

subcmd="${1:-}"
shift 2>/dev/null || true

case "$subcmd" in
    tail)
        check_log
        echo "Tailing $LOG_FILE (Ctrl-C to stop)..."
        tail -f "$LOG_FILE" | while IFS= read -r line; do
            # Pretty-print: timestamp session category message
            ts=$(echo "$line" | jq -r '.ts // empty' 2>/dev/null)
            sid=$(echo "$line" | jq -r '.sid // empty' 2>/dev/null)
            cat=$(echo "$line" | jq -r '.cat // empty' 2>/dev/null)
            msg=$(echo "$line" | jq -r '.msg // empty' 2>/dev/null)
            lvl=$(echo "$line" | jq -r '.level // "info"' 2>/dev/null)
            if [ -n "$ts" ]; then
                # Color errors red, warnings yellow
                case "$lvl" in
                    error) printf "\033[31m%s [%s] [%s] %s\033[0m\n" "$ts" "${sid:0:16}" "$cat" "$msg" ;;
                    warn)  printf "\033[33m%s [%s] [%s] %s\033[0m\n" "$ts" "${sid:0:16}" "$cat" "$msg" ;;
                    *)     printf "%s [%s] [%s] %s\n" "$ts" "${sid:0:16}" "$cat" "$msg" ;;
                esac
            else
                echo "$line"
            fi
        done
        ;;

    last)
        check_log
        n="${1:-50}"
        tail -n "$n" "$LOG_FILE" | jq -r '
            "\(.ts) [\(.sid[0:16])] [\(.cat)] \(.msg)" +
            if .level == "error" then " [ERROR]"
            elif .level == "warn" then " [WARN]"
            else "" end
        ' 2>/dev/null || tail -n "$n" "$LOG_FILE"
        ;;

    session)
        check_log
        sid="${1:-}"
        if [ -z "$sid" ]; then
            # Try reading current session from file
            if [ -f "$HOME/.krull-session" ]; then
                sid=$(cat "$HOME/.krull-session")
            else
                echo "No active session. Provide a session ID or start krull-claude."
                echo "List sessions: ./krull logs sessions"
                exit 1
            fi
        fi
        echo "Session: $sid"
        echo "---"
        grep "\"$sid\"" "$LOG_FILE" | jq -r '
            "\(.ts) [\(.cat)] \(.msg)" +
            if .level == "error" then " [ERROR]"
            elif .level == "warn" then " [WARN]"
            else "" end
        ' 2>/dev/null || grep "$sid" "$LOG_FILE"
        ;;

    errors)
        check_log
        n="${1:-20}"
        grep -E '"level"\s*:\s*"(error|warn)"' "$LOG_FILE" | tail -n "$n" | jq -r '
            "\(.ts) [\(.sid[0:16])] [\(.level | ascii_upcase)] [\(.cat)] \(.msg)" +
            if .data then "\n  " + (.data | tostring) else "" end
        ' 2>/dev/null || grep -E '"(error|warn)"' "$LOG_FILE" | tail -n "$n"
        ;;

    search)
        check_log
        pattern="${1:-}"
        if [ -z "$pattern" ]; then
            echo "Usage: ./krull logs search <pattern>"
            exit 1
        fi
        grep -i "$pattern" "$LOG_FILE" | jq -r '
            "\(.ts) [\(.sid[0:16])] [\(.cat)] \(.msg)"
        ' 2>/dev/null || grep -i "$pattern" "$LOG_FILE"
        ;;

    filters)
        check_log
        n="${1:-30}"
        grep '"cat"\s*:\s*"FILTER"' "$LOG_FILE" | tail -n "$n" | jq -r '
            "\(.ts) [\(.sid[0:16])] \(.msg)"
        ' 2>/dev/null || grep 'FILTER' "$LOG_FILE" | tail -n "$n"
        ;;

    requests)
        check_log
        n="${1:-20}"
        grep -E 'Responses API|Chat completions|Passthrough' "$LOG_FILE" | tail -n "$n" | jq -r '
            "\(.ts) [\(.sid[0:16])] \(.msg)" +
            if .data then " | " + (.data | to_entries | map("\(.key)=\(.value)") | join(" ")) else "" end
        ' 2>/dev/null || grep -E 'Responses API|Chat completions|Passthrough' "$LOG_FILE" | tail -n "$n"
        ;;

    sessions)
        check_log
        echo "Recent sessions (newest first):"
        echo "---"
        jq -r '.sid' "$LOG_FILE" 2>/dev/null | sort | uniq -c | sort -rn | head -20 | while read -r count sid; do
            # Get first and last timestamp for this session
            first=$(grep "\"$sid\"" "$LOG_FILE" | head -1 | jq -r '.ts' 2>/dev/null)
            last=$(grep "\"$sid\"" "$LOG_FILE" | tail -1 | jq -r '.ts' 2>/dev/null)
            printf "  %-24s  %4d entries  %s → %s\n" "$sid" "$count" "$first" "$last"
        done
        ;;

    clear)
        check_log
        archive="$LOG_DIR/proxy-$(date +%Y%m%dT%H%M%S).jsonl"
        cp "$LOG_FILE" "$archive"
        : > "$LOG_FILE"
        echo "Archived to $archive"
        echo "Log file cleared."
        ;;

    ""|help|--help|-h)
        usage
        ;;

    *)
        echo "Unknown subcommand: $subcmd"
        echo ""
        usage
        exit 1
        ;;
esac
