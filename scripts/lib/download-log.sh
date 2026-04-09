# Shared progress-tracking helpers for download-*.sh scripts.
#
# Writes a manifest-based state file at data/downloads/state.json that any
# reader (the web UI, a CLI watcher, etc.) can poll to compute download
# progress by stat-ing the listed files on disk. No PID tracking, no IPC —
# just declared intent + live file sizes.
#
# Also appends curl failures (bad URLs, HTTP errors) to
# data/downloads/errors.jsonl so stale catalog entries can be triaged
# later.
#
# Usage (source from a download-*.sh script):
#
#     source "$(dirname "${BASH_SOURCE[0]}")/lib/download-log.sh"
#     dl_state_begin knowledge devdocs-python "Python stdlib docs"
#     dl_state_add "$ZIM_DIR/file.zim" "$(dl_parse_size '4 MB')"
#     dl_run_curl "$ZIM_DIR/file.zim" "https://..." --progress-bar
#     dl_state_end done
#
# Uses node (which is shipped inside the krull-home container and required
# by the host toolchain anyway) rather than python so the helper works
# unchanged whether it is invoked from the web UI (container) or from the
# `./krull download-*` CLI wrapper.
#
# Env vars:
#   KRULL_REPO           Repo root. Defaults to the parent of scripts/.
#   KRULL_DOWNLOAD_KIND  Optional kind label (knowledge|wikipedia|maps).
#   KRULL_DOWNLOAD_JOB_ID  Optional job id set by the backend installer;
#                        if unset a synthetic cli-<pid>-<epoch> is used so
#                        terminal-driven downloads still show up in the UI.

_dl_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DL_REPO="${KRULL_REPO:-$(cd "$_dl_script_dir/../.." && pwd)}"
DL_STATE_DIR="$DL_REPO/data/downloads"
DL_STATE_FILE="$DL_STATE_DIR/state.json"
DL_ERRORS_FILE="$DL_STATE_DIR/errors.jsonl"

_dl_ensure_dir() {
    mkdir -p "$DL_STATE_DIR"
}

# dl_parse_size "100 MB" -> 104857600
dl_parse_size() {
    node -e '
const s = process.argv[1] || "";
const m = s.match(/^\s*~?\s*([\d.]+)\s*([KMGT]?)B?/i);
if (!m) { console.log(0); process.exit(0); }
const v = parseFloat(m[1]);
const u = (m[2] || "").toUpperCase();
const mult = u === "T" ? 1024**4 : u === "G" ? 1024**3 : u === "M" ? 1024**2 : u === "K" ? 1024 : 1;
console.log(Math.round(v * mult));
' "$1"
}

# dl_state_begin <kind> <key> <name>
# Open a new active entry with an empty manifest.
dl_state_begin() {
    _dl_ensure_dir
    local kind="$1" key="$2" name="$3"
    local job_id="${KRULL_DOWNLOAD_JOB_ID:-cli-$$-$(date +%s)}"
    DL_CURRENT_KIND="$kind"
    DL_CURRENT_KEY="$key"
    DL_CURRENT_JOB_ID="$job_id"
    export DL_CURRENT_KIND DL_CURRENT_KEY DL_CURRENT_JOB_ID
    node -e '
const fs = require("fs");
const path = require("path");
const [file, jobId, kind, key, name] = process.argv.slice(1);
let doc = { active: null, queue: [] };
try { doc = JSON.parse(fs.readFileSync(file, "utf8")); } catch (_) {}
const now = Date.now();
doc.active = {
    jobId, kind, key, name,
    phase: "downloading",
    startedAt: now, updatedAt: now,
    manifest: [],
};
const tmp = path.join(path.dirname(file), "." + path.basename(file) + "." + process.pid + ".tmp");
fs.writeFileSync(tmp, JSON.stringify(doc));
fs.renameSync(tmp, file);
' "$DL_STATE_FILE" "$job_id" "$kind" "$key" "$name"
}

# dl_state_add <path> <expected_bytes>
# Append one file to the active manifest. Call once per target file.
dl_state_add() {
    _dl_ensure_dir
    local file_path="$1" expected="${2:-0}"
    node -e '
const fs = require("fs");
const path = require("path");
const [file, filePath, expectedRaw] = process.argv.slice(1);
let doc;
try { doc = JSON.parse(fs.readFileSync(file, "utf8")); } catch (_) { process.exit(0); }
if (!doc.active) process.exit(0);
const exp = parseInt(expectedRaw, 10) || 0;
(doc.active.manifest = doc.active.manifest || []).push({ path: filePath, expectedBytes: exp });
doc.active.updatedAt = Date.now();
const tmp = path.join(path.dirname(file), "." + path.basename(file) + "." + process.pid + ".tmp");
fs.writeFileSync(tmp, JSON.stringify(doc));
fs.renameSync(tmp, file);
' "$DL_STATE_FILE" "$file_path" "$expected"
}

# dl_state_end [status]   status: done|failed  (default: done)
# Clears the active slot. Does not touch the queue — the backend
# manages the queue for krull-home-driven jobs, and for CLI-driven
# jobs there is no queue.
dl_state_end() {
    _dl_ensure_dir
    local status="${1:-done}"
    node -e '
const fs = require("fs");
const path = require("path");
const [file] = process.argv.slice(1);
let doc;
try { doc = JSON.parse(fs.readFileSync(file, "utf8")); } catch (_) { process.exit(0); }
doc.active = null;
const tmp = path.join(path.dirname(file), "." + path.basename(file) + "." + process.pid + ".tmp");
fs.writeFileSync(tmp, JSON.stringify(doc));
fs.renameSync(tmp, file);
' "$DL_STATE_FILE" "$status"
    unset DL_CURRENT_KIND DL_CURRENT_KEY DL_CURRENT_JOB_ID
}

# dl_log_error <kind> <key> <file> <url> <http_status> <curl_exit> <reason>
# Append a single JSONL entry to data/downloads/errors.jsonl.
dl_log_error() {
    _dl_ensure_dir
    local kind="$1" key="$2" file="$3" url="$4" status="$5" code="$6" reason="$7"
    node -e '
const fs = require("fs");
const [file, kind, key, srcFile, url, statusRaw, codeRaw, reason] = process.argv.slice(1);
const httpStatus = /^\d+$/.test(statusRaw) ? parseInt(statusRaw, 10) : null;
const curlExit = /^-?\d+$/.test(codeRaw) ? parseInt(codeRaw, 10) : null;
const entry = {
    timestamp: new Date().toISOString(),
    kind, key, file: srcFile, url,
    httpStatus, curlExit, reason,
    source: "script",
};
fs.appendFileSync(file, JSON.stringify(entry) + "\n");
' "$DL_ERRORS_FILE" "$kind" "$key" "$file" "$url" "$status" "$code" "$reason"
}

# --- Internals: HEAD probe + sidecar reconcilation -----------------------
#
# curl -C - is dumb: it sends `Range: bytes=N-` and blindly appends the
# response to our partial. If the upstream file has been replaced with a
# new edition (different ETag, different size, different bytes at offset
# N) curl happily stitches the new bytes onto our old prefix, producing
# a corrupted hybrid. Worse, it exits 0 so we think everything's fine.
#
# To detect upstream drift we probe with `curl -sIL` before each resume
# and store the remote ETag, Last-Modified, and Content-Length in a
# sidecar file ($FILE.meta) alongside the partial. On the next resume
# we compare the sidecar to the current server headers and if they
# disagree we wipe the partial and start over.

_dl_parse_header() {
    # $1 = header blob, $2 = lowercase header name.
    # Prints the LAST matching value (handles 3xx header chains correctly).
    local blob="$1" name="$2"
    printf '%s' "$blob" | awk -v n="$name" '
        BEGIN { IGNORECASE = 1; val = "" }
        tolower($0) ~ "^" n ":" {
            sub(/^[^:]*:[ \t]*/, "")
            sub(/[\r\n]+$/, "")
            val = $0
        }
        END { print val }
    '
}

_dl_save_meta() {
    # $1 = sidecar path, $2 = url, $3 = header blob (may be empty)
    local meta="$1" url="$2" blob="$3"
    local etag lm length
    etag=$(_dl_parse_header "$blob" "etag")
    lm=$(_dl_parse_header "$blob" "last-modified")
    length=$(_dl_parse_header "$blob" "content-length")
    {
        printf 'url=%s\n' "$url"
        [ -n "$etag" ] && printf 'etag=%s\n' "$etag"
        [ -n "$lm" ] && printf 'last-modified=%s\n' "$lm"
        [ -n "$length" ] && printf 'content-length=%s\n' "$length"
    } > "$meta"
}

_dl_read_meta_field() {
    # $1 = sidecar path, $2 = field name. Prints value or empty.
    local meta="$1" name="$2"
    [ -f "$meta" ] || { printf ''; return; }
    awk -F= -v n="$name" '$1 == n { sub(/^[^=]*=/, ""); print; exit }' "$meta"
}

# _dl_probe_and_reconcile <out_path> <url>
# Inspects the local partial file against current remote headers and
# deletes it (+ its sidecar) if the upstream has drifted. Silent if the
# network is unreachable — in that case curl -C - will take its chances.
_dl_probe_and_reconcile() {
    local out="$1" url="$2"
    local meta="$out.meta"

    local blob
    blob=$(curl -sIL --max-time 15 "$url" 2>/dev/null || true)
    if [ -z "$blob" ]; then
        # Probe failed — don't touch the file, let curl -C - decide.
        return 0
    fi

    # No partial yet (fresh download). Drop any stale sidecar from a
    # previous aborted attempt and seed a new one with the current
    # remote Content-Length / ETag / Last-Modified so the krull-home
    # progress endpoint can read the authoritative total from byte
    # zero, instead of relying on the catalog estimate string for the
    # entire first run.
    if [ ! -s "$out" ]; then
        rm -f "$meta"
        _dl_save_meta "$meta" "$url" "$blob"
        return 0
    fi

    local remote_etag remote_lm remote_length local_size
    remote_etag=$(_dl_parse_header "$blob" "etag")
    remote_lm=$(_dl_parse_header "$blob" "last-modified")
    remote_length=$(_dl_parse_header "$blob" "content-length")
    local_size=$(stat -c%s "$out" 2>/dev/null || stat -f%z "$out" 2>/dev/null || echo 0)

    # Case 1: local partial is larger than what the server now has.
    # Either the file was replaced by a smaller one or shrunk. Wipe.
    if [ -n "$remote_length" ] && [ "$local_size" -gt "$remote_length" ]; then
        echo "[!] Local partial ($local_size B) exceeds remote ($remote_length B); wiping stale file." >&2
        rm -f "$out" "$meta"
        return 0
    fi

    # Case 2: sidecar recorded an ETag/Last-Modified and either has
    # changed -> upstream edition changed -> wipe and restart.
    if [ -f "$meta" ]; then
        local saved_etag saved_lm
        saved_etag=$(_dl_read_meta_field "$meta" "etag")
        saved_lm=$(_dl_read_meta_field "$meta" "last-modified")
        if [ -n "$saved_etag" ] && [ -n "$remote_etag" ] && [ "$saved_etag" != "$remote_etag" ]; then
            echo "[!] Upstream ETag changed; wiping stale partial and restarting." >&2
            rm -f "$out" "$meta"
            # Save new baseline for the fresh download.
            _dl_save_meta "$meta" "$url" "$blob"
            return 0
        fi
        if [ -n "$saved_lm" ] && [ -n "$remote_lm" ] && [ "$saved_lm" != "$remote_lm" ]; then
            echo "[!] Upstream Last-Modified changed; wiping stale partial and restarting." >&2
            rm -f "$out" "$meta"
            _dl_save_meta "$meta" "$url" "$blob"
            return 0
        fi
    fi

    # Safe to resume. Refresh/initialize the sidecar so future runs
    # have a baseline to compare against.
    _dl_save_meta "$meta" "$url" "$blob"
}

# dl_run_curl <out_path> <url> [extra curl args...]
# Runs curl with --fail -L -C - and captures the HTTP status code.
# Before resuming an existing partial it probes the upstream with a
# HEAD request and wipes the local file if ETag/Last-Modified/size
# have drifted (see _dl_probe_and_reconcile). On any failure it
# appends a structured entry to errors.jsonl with the URL, HTTP
# status, and curl exit code so bad catalog URLs can be diagnosed
# later. Returns curl's exit code so callers can react.
dl_run_curl() {
    local out="$1" url="$2"; shift 2

    _dl_probe_and_reconcile "$out" "$url"

    local http_code curl_exit
    # Use a conditional so an outer `set -e` does not abort on a
    # non-zero curl exit before we get to log it.
    http_code=$(curl --fail -L -C - -o "$out" -w '%{http_code}' "$url" "$@") && curl_exit=0 || curl_exit=$?
    if [ "$curl_exit" -ne 0 ]; then
        dl_log_error \
            "${DL_CURRENT_KIND:-unknown}" \
            "${DL_CURRENT_KEY:-unknown}" \
            "$out" "$url" "${http_code:-0}" "$curl_exit" "curl --fail exited $curl_exit (HTTP ${http_code:-?})"
    fi
    return "$curl_exit"
}
