#!/bin/bash
# krull bakeoff — head-to-head of candidate Ollama models on THIS machine.
#
# Implements the checklist and decision rule from the user's handoff doc
# "Krull AI: Local Coding Model Handoff":
#   - tool-call reliability is a hard gate: a model that fails any box is
#     disqualified, whatever its speed;
#   - the primary offline model is whichever passes every box with the best
#     quality; if no Qwen run passes, stay on gemma4:e4b;
#   - gemma4:e4b stays installed as the known-good fallback either way.
#
# Per model it measures the largest context that loads, VRAM, tokens/sec at
# 8K and at the max context, and tool_use success through the SSE proxy
# (krull-claude -p in a throwaway project). Results go to
# data/bakeoff/<timestamp>/results.md with raw logs beside it.
#
# Model switching reuses the Settings-page mechanism (krull-home API):
#   PUT  /api/env            OLLAMA_NUM_CTX + CONTEXT_COMPACT_LIMIT, which
#                            recreates krull-ollama and krull-sse-proxy
#   POST /api/models/select  OLLAMA_MODEL + litellm/config.yaml model lines,
#                            restarts krull-litellm, sets the WebUI default
# The original OLLAMA_MODEL / OLLAMA_NUM_CTX / CONTEXT_COMPACT_LIMIT and the
# exact bytes of litellm/config.yaml are restored on exit, failure, hangup or
# Ctrl-C. .env itself is put back byte-for-byte (owner and mode included)
# from a 0600 snapshot kept in the private temp dir, never in the run dir,
# unless some OTHER key changed during the run; then only the three keys are
# restored. Afterwards the krull-ollama / krull-sse-proxy environment is
# compared with the pre-run one and force-recreated if a racing recreate
# left test values behind. Any model the run loaded, other than the restored
# OLLAMA_MODEL, is then unloaded so it does not sit on its 30m keep_alive.
#
# Containment of the model under test: krull-claude runs with
# --setting-sources "" and --strict-mcp-config (none of the user's settings,
# allow-lists, hooks or MCP servers load), --permission-mode dontAsk, and only
# Read(./**), Edit(./**) and `bash run_tests.sh` allowed. run_tests.sh is
# read-only and re-execs itself under bwrap: no network, / read-only, $HOME,
# the repo and /run hidden, only its own task dir writable. Model-edited code
# (calc.sh) therefore never runs outside the sandbox, including when this
# script re-runs the tests itself. The claude process is NOT sandboxed: it
# has to reach LiteLLM on localhost:4000.
#
# Side writes outside data/bakeoff/, all undone in `finish`:
#   ~/.krull-session   krull-claude overwrites it per task (the proxy reads it
#                      to tag logs; the path is fixed) and its exit watcher
#                      only deletes it, so the pre-run file is backed up and
#                      put back after every task and on exit. A krull-claude
#                      session of your own that runs DURING the bake-off is
#                      logged under the test session ids.
#   ~/.claude/projects/<temp project>-*   Claude Code transcripts of the test
#                      sessions; deleted on exit (copies are in raw/).
# One bake-off at a time: data/bakeoff/.lock (flock).
#
# Linux + NVIDIA only (it reads VRAM with nvidia-smi and reaches Ollama
# through the krull-home container). It never touches network config: run it
# with the network unplugged yourself; it records whether DNS resolved.

# No `set -e`: a failing step is a measurement for that model, not a reason
# to abort the whole bake-off. Every step checks its own return value.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"
LITELLM_CONFIG="$PROJECT_DIR/litellm/config.yaml"
KRULL="$PROJECT_DIR/krull"
KRULL_CLAUDE="$SCRIPT_DIR/krull-claude"
HOME_API="${KRULL_HOME_URL:-http://localhost:8000}"
OLLAMA_INTERNAL="http://krull-ollama:11434"

DEFAULT_MODELS=(gemma4:e2b gemma4:e4b qwen3.6:35b-a3b-coding qwen3.6:27b-q4_K_M)
FALLBACK_MODEL="gemma4:e4b"
CTX_LADDER=(8192 16384 32768 49152 65536 98304 131072)
REQUIRED_CONTAINERS=(krull-ollama krull-webui krull-litellm krull-sse-proxy krull-home)

# Per-task wall-clock limits (seconds). Hitting one is scored as a stall.
T_LOAD=900; T_CHAT=600; T_SIMPLE=600; T_LOOP=1200; T_NEAR=3600

# Loop/stall markers the SSE proxy writes (proxy/sse_patch.py proxy_log).
# results.md lists the markers from this same string, so the two agree.
LOOP_RE='\+loop_break|\+temp_escalation|\+stalled_progress_warning|\+approach_exhausted|\+hard_tool_cap'
HARD_RE='EMPTY OUTPUT|\[ERROR\]'
COMPACT_RE='Context compacted'

DRY_RUN=0; ASSUME_YES=0; SKIP_PULL=0; PULL_ONLY=0
MAX_CTX=131072; TEST_CTX_CAP=65536; LOOPS=3
MODELS=()

usage() {
    cat <<EOF
Usage: ./krull bakeoff [options] [model ...]

Head-to-head of Ollama models on this machine. Default models:
  ${DEFAULT_MODELS[*]}

Options:
  --dry-run         Print every step and check prerequisites; change nothing
  --pull-only       Pull missing models (needs network), then stop
  --skip-pull       Never pull; models that are not installed are skipped
  --max-ctx N       Largest context to try when probing (default $MAX_CTX)
  --test-ctx N      Cap on the context used for the tool tests (default $TEST_CTX_CAP)
  --yes             Do not pause for the "disconnect the network" prompt
  -h, --help        This help

Run order: pull while online (or --pull-only first), unplug the network,
then run again with --skip-pull. Results: data/bakeoff/<timestamp>/results.md
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --pull-only) PULL_ONLY=1 ;;
        --skip-pull) SKIP_PULL=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --max-ctx) shift; MAX_CTX="${1:-}" ;;
        --test-ctx) shift; TEST_CTX_CAP="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1"; usage; exit 1 ;;
        *) MODELS+=("$1") ;;
    esac
    shift
done
[ ${#MODELS[@]} -eq 0 ] && MODELS=("${DEFAULT_MODELS[@]}")

for n in "$MAX_CTX" "$TEST_CTX_CAP"; do
    [[ "$n" =~ ^[0-9]+$ ]] || { echo "ERROR: context sizes must be integers (got '$n')"; exit 1; }
done
# Same safety regex krull-home's selectActiveModel applies to model keys.
for m in "${MODELS[@]}"; do
    [[ "$m" =~ ^[A-Za-z0-9._/:-]+$ ]] || { echo "ERROR: unsafe model tag: $m"; exit 1; }
done
if [ "$PULL_ONLY" -eq 1 ] && [ "$SKIP_PULL" -eq 1 ]; then
    echo "ERROR: --pull-only and --skip-pull contradict each other"; exit 1
fi

TS="$(date +%Y%m%dT%H%M%S)-$$"
RUN_DIR="$PROJECT_DIR/data/bakeoff/$TS"
LOCK_FILE="$PROJECT_DIR/data/bakeoff/.lock"
WORK_DIR=""
PRIVATE_DIR=""    # $WORK_DIR/.private (0700): .env snapshot, session backup
SANDBOX_RUN=""    # $WORK_DIR/sandbox-run, the bwrap wrapper
RUN_STARTED=0     # set once anything may have been changed
STACK_TOUCHED=0   # set just before the first PUT /api/env; restore skips the API without it
LOADED_TAGS=()    # every tag the run sent to Ollama; finish unloads them (unload_run_models)
RUNNER_MANIFEST="" # "<sha256>  <path>" per run_tests.sh written; the Bash gate checks it
RESTORED=0
RESULTS_TSV=""

log() {
    echo "$*"
    if [ "$RUN_STARTED" -eq 1 ] && [ -d "$RUN_DIR" ]; then
        echo "$(date +%H:%M:%S) $*" >> "$RUN_DIR/bakeoff.log"
    fi
}

# ── .env helpers (named keys only; never dump the file, it holds secrets) ──

env_has() { [ -f "$ENV_FILE" ] && grep -qE "^$1=" "$ENV_FILE"; }
env_get() {
    # Last assignment wins; strip one layer of surrounding quotes, as envFile.ts does.
    local v
    v="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}
# Write bytes from stdin to .env through a temp file in the same dir plus
# rename (krull-home does the same, and runs as root, so after a Settings
# write the old file may not be writable by us in place). The new file gets
# the pre-run mode and, where permitted, owner. .env is never a single-file
# bind mount (krull-home and krull-updater mount the whole repo), so a new
# inode is safe.
ENV_MODE=""; ENV_UID=""; ENV_GID=""
env_write_atomic() {
    python3 -I -c '
import os, sys
path, mode, uid, gid = sys.argv[1], int(sys.argv[2], 8), int(sys.argv[3]), int(sys.argv[4])
data = sys.stdin.buffer.read()
tmp = os.path.join(os.path.dirname(path), ".env.bakeoff.%d" % os.getpid())
try:
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(data)
        f.flush()
        os.fchmod(f.fileno(), mode)
        try:
            os.fchown(f.fileno(), uid, gid)
        except PermissionError:
            pass
        os.fsync(f.fileno())
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    raise
' "$ENV_FILE" "${ENV_MODE:-600}" "${ENV_UID:-$(id -u)}" "${ENV_GID:-$(id -g)}"
}

# Put the pre-run .env bytes back if the only keys that differ from the
# snapshot are the ones this script changes. Prints one status word:
# same | restored | other-keys-changed:<names> (names only, never values).
env_restore_bytes() {   # snapshot key...
    local status
    status="$(python3 -I -c '
import re, sys
path, snap, targets = sys.argv[1], sys.argv[2], set(sys.argv[3:])
KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
def parse(b):
    d = {}
    for line in b.decode("utf-8", "replace").splitlines():
        m = KV.match(line)
        if m and not line.lstrip().startswith("#"):
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"\x27":
                v = v[1:-1]
            d[m.group(1)] = v
    return {k: v for k, v in d.items() if k not in targets}
cur, orig = open(path, "rb").read(), open(snap, "rb").read()
if cur == orig:
    print("same")
else:
    a, b = parse(cur), parse(orig)
    diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    print("other-keys-changed:" + ",".join(diff) if diff else "restore")
' "$ENV_FILE" "$@")" || { echo "error"; return 1; }
    if [ "$status" = "restore" ]; then
        env_write_atomic < "$1" && status="restored" || status="error"
    fi
    echo "$status"
}

# Direct .env edit, used only as the restore fallback when the krull-home API
# did not land. Keeps every other line; written via env_write_atomic.
# A value of __ABSENT__ deletes the key.
env_set_direct() {
    python3 -I -c '
import re, sys
path, pairs = sys.argv[1], sys.argv[2:]
want = dict(p.split("=", 1) for p in pairs)
lines = open(path).read().splitlines(keepends=True)
out, done = [], set()
for line in lines:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
    if m and m.group(1) in want:
        k = m.group(1)
        if k in done or want[k] == "__ABSENT__":
            continue
        out.append("%s=%s\n" % (k, want[k])); done.add(k); continue
    out.append(line)
for k, v in want.items():
    if k not in done and v != "__ABSENT__":
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("%s=%s\n" % (k, v))
sys.stdout.write("".join(out))
' "$ENV_FILE" "$@" > "$PRIVATE_DIR/env.new" && env_write_atomic < "$PRIVATE_DIR/env.new"
    local rc=$?
    rm -f -- "${PRIVATE_DIR:?}/env.new"
    return "$rc"
}

# ── Stack helpers ────────────────────────────────────────────────────────────

docker_ok() { timeout 10 docker info >/dev/null 2>&1; }
gpu_ok() { timeout 10 nvidia-smi -L >/dev/null 2>&1; }
container_running() {
    [ "$(docker inspect --format='{{.State.Status}}' "$1" 2>/dev/null)" = "running" ]
}
vram_used_mib() {
    timeout 10 nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '
}
dns_state() {
    if timeout 5 getent hosts ollama.com >/dev/null 2>&1; then echo "ONLINE"; else echo "OFFLINE"; fi
}

# Ollama's port is not published and its image has no curl, so reach its HTTP
# API through krull-home, which ships curl and sits on the same network.
ollama_get() {
    docker exec krull-home curl -s --max-time 30 "$OLLAMA_INTERNAL$1"
}
ollama_post() {   # path json-body max-seconds
    printf '%s' "$2" | docker exec -i krull-home curl -s --max-time "${3:-60}" \
        -X POST "$OLLAMA_INTERNAL$1" -H 'Content-Type: application/json' --data-binary @-
}

# Health probes, same checks scripts/update.sh uses.
check_webui()   { docker exec krull-webui curl -sf -o /dev/null http://localhost:8080/api/config; }
check_litellm() { curl -sf -o /dev/null --max-time 5 http://localhost:4000/; }
check_proxy()   { curl -sf -o /dev/null --max-time 5 http://localhost:4001/; }
check_ollama()  { docker exec krull-ollama ollama list; }
wait_for() {
    local label="$1" limit="$2"; shift 2
    local waited=0
    until "$@" >/dev/null 2>&1; do
        if [ "$waited" -ge "$limit" ]; then log "    $label: not ready after ${limit}s"; return 1; fi
        sleep 2; waited=$((waited + 2))
    done
    return 0
}

# Compose from the repo dir so .env's COMPOSE_FILE (the platform and GPU
# override files start.sh selects) applies, as scripts/update.sh runs it.
compose() { ( cd "$PROJECT_DIR" && docker compose "$@" ); }

# A container's environment, one VAR=value per line, sorted. It can hold
# secrets, so it lives in shell variables only: never written, never logged.
container_env() {
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null | sed '/^$/d' | sort
}
env_diff_names() {   # orig-env current-env -> names of the differing vars, no values
    diff <(printf '%s\n' "$1") <(printf '%s\n' "$2") | sed -n 's/^[<>] \([^=]*\)=.*/\1/p' | sort -u | tr '\n' ' '
}

# ── Sandbox for model-edited code ────────────────────────────────────────────
# sandbox-run DIR CMD... runs CMD under bwrap with DIR as the only writable
# path: / read-only; $HOME, the repo, the private dir, /tmp and /run replaced
# by empty read-only tmpfs (a read-only bind does not stop connect() on a unix
# socket, so docker.sock and the session sockets under /run must be hidden);
# no network but loopback; own pid/ipc/uts; clean environment; fds 3-9 (the
# lock) closed; dies with its parent. It lives outside every task dir, so the
# model's Edit(./**) cannot reach it.
# DIR/run_tests.sh is bound read-only over the writable DIR: inside, unlink
# and rename of a mount point fail (EBUSY) and writes fail (EROFS), so code
# under test cannot swap the runner the model next runs on the host. DIR
# must hold run_tests.sh as a regular file, or sandbox-run refuses.
write_sandbox_run() {   # path
    local masks=() m r
    for m in "$HOME" "$PROJECT_DIR" "$PRIVATE_DIR"; do
        [ -n "$m" ] || continue
        r="$(cd "$m" 2>/dev/null && pwd -P)" || continue
        [ "$r" != "/" ] && masks+=("$r")
    done
    {
        echo '#!/bin/bash'
        echo '# Generated by scripts/model-bakeoff.sh. Usage: sandbox-run DIR CMD...'
        echo 'd="$(cd "${1:?usage: sandbox-run DIR CMD...}" && pwd -P)" || exit 2; shift'
        echo 'r="$d/run_tests.sh"'
        echo '[ -f "$r" ] && [ ! -L "$r" ] || { echo "sandbox-run: $r is not a regular file, refusing" >&2; exit 2; }'
        printf 'exec bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run'
        for m in "${masks[@]}"; do printf ' --tmpfs %q' "$m"; done
        printf ' \\\n    --bind "$d" "$d" --ro-bind "$r" "$r" --remount-ro /tmp --remount-ro /run'
        for m in "${masks[@]}"; do printf ' --remount-ro %q' "$m"; done
        printf ' \\\n'
        echo '    --chdir "$d" --unshare-net --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try \'
        echo '    --die-with-parent --new-session --clearenv \'
        echo '    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME "$d" --setenv BAKEOFF_SANDBOX 1 \'
        echo '    -- "$@" 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-'
    } > "$1" && chmod 555 "$1"
}

# Every run_tests.sh opens with this prelude: unless it already sees no
# network interface but loopback (it is inside), it re-execs itself under
# sandbox-run, so the calc.sh it sources never runs outside. If the re-exec
# fails, bash exits there and the tests never run. The file is 0444, denied
# to Edit, bound read-only inside the sandbox, and its sha256 goes in
# RUNNER_MANIFEST: the Bash gate runs `bash run_tests.sh` only when the file
# in the session's cwd is that exact regular file.
write_runner() {   # dir sandbox-run body
    local f
    f="$(cd "$1" && pwd -P)/run_tests.sh" || return 1
    {
        echo '#!/bin/bash'
        echo 'if [ "$(grep -cvE "^ *(lo:|Inter-|face )" /proc/self/net/dev 2>/dev/null)" != 0 ]; then'
        echo '    [ -n "${BAKEOFF_SANDBOX:-}" ] && { echo "sandbox: network visible inside, refusing" >&2; exit 3; }'
        printf '    exec %q "$(dirname "$0")" /bin/bash "$(basename "$0")"\n' "$2"
        echo 'fi'
        printf '%s\n' "$3"
    } > "$f" && chmod 444 "$f" || return 1
    [ -z "$RUNNER_MANIFEST" ] || printf '%s  %s\n' "$(sha256sum < "$f" | cut -d' ' -f1)" "$f" >> "$RUNNER_MANIFEST"
}

# Proves the sandbox on this host before any model gets Bash: inside it a
# write to $HOME and a TCP connect to 1.1.1.1 must fail and the docker socket
# must be invisible, and run_tests.sh must survive unlink, overwrite, rename
# over it and chmod, while the task dir stays writable. Leaves nothing behind.
sandbox_selftest() {
    local t probe="$HOME/.krull-bakeoff-selftest-$$" out sum
    t="$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/krull-bakeoff-selftest.XXXXXX")" || { echo "mktemp failed"; return 1; }
    mkdir -p "$t/task"
    write_sandbox_run "$t/sandbox-run"
    RUNNER_MANIFEST="" write_runner "$t/task" "$t/sandbox-run" 'echo runner'
    sum="$(sha256sum < "$t/task/run_tests.sh")"
    out="$(timeout 20 "$t/sandbox-run" "$t/task" /bin/bash -c '
        touch "$1" 2>/dev/null && echo "home writable;"
        (exec 3<>/dev/tcp/1.1.1.1/80) 2>/dev/null && echo "network reachable;"
        { [ -S /run/docker.sock ] || [ -S /var/run/docker.sock ]; } && echo "docker socket visible;"
        touch ./w 2>/dev/null || echo "task dir not writable;"
        rm -f run_tests.sh 2>/dev/null; [ -f run_tests.sh ] || echo "runner unlinked;"
        { printf x > run_tests.sh; } 2>/dev/null && echo "runner overwritten;"
        printf x > ./new 2>/dev/null && mv -f ./new run_tests.sh 2>/dev/null && echo "runner renamed over;"
        chmod u+w run_tests.sh 2>/dev/null && echo "runner chmod-ed;"
        echo done' _ "$probe" 2>&1)"
    [ -e "$probe" ] && { rm -f -- "$probe"; out="$out home write landed;"; }
    [ "$(sha256sum < "$t/task/run_tests.sh" 2>/dev/null)" = "$sum" ] || out="$out runner changed on the host;"
    chmod -R u+rwX "$t" 2>/dev/null; rm -rf -- "${t:?}"
    [ "$out" = "done" ] && return 0
    echo "${out:-bwrap produced no output}" | tr '\n' ' '
    return 1
}

# ── Claude Code containment ──────────────────────────────────────────────────
# Tools krull-claude may use unattended: Read and Edit inside the task dir,
# and Bash only as the fixture's self-sandboxing runner. dontAsk turns every
# other call into a denial (counted: a wrong-tool signal); it also closes
# acceptEdits' auto-approved mv/cp/rm, which could replace run_tests.sh.
# --setting-sources "" (narrowest the CLI accepts) and --strict-mcp-config
# keep the user's settings, allow-list, hooks, plugins and MCP servers out.
ALLOWED_TOOLS=("Read(./**)" "Edit(./**)" "Bash(bash run_tests.sh)" "Bash(bash ./run_tests.sh)")
DENIED_TOOLS=(Write NotebookEdit "Edit(./run_tests.sh)")
CLAUDE_TASK_FLAGS=()

# Claude Code runs commands it classes as read-only (ls, cat, ...) without
# consulting the allow-list, even under dontAsk, and they would run outside
# the sandbox. This PreToolUse hook (matcher "*"), passed with --settings
# (still honoured under --setting-sources ""), is an allow-list:
#   Read, Glob, Grep  only paths that resolve inside the session's cwd
#   Edit              same, and never a symlink, a non-regular file or run_tests.sh
#   Bash              only `bash run_tests.sh` / `bash ./run_tests.sh`, and only
#                     when cwd/run_tests.sh is a regular file whose sha256 is the
#                     one write_runner recorded in the manifest
# Every other tool is denied. Passing it is not an allow: the permission
# rules above still apply.
write_bash_gate() {   # path
    cat > "$1" <<'EOF'
# Generated by scripts/model-bakeoff.sh: PreToolUse gate for the test sessions.
# Usage: python3 -I bash-gate MANIFEST  (hook input JSON on stdin)
import hashlib, json, os, sys

def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
          "permissionDecision": "deny", "permissionDecisionReason": why}}))
    sys.exit(0)

try:
    ev = json.load(sys.stdin)
    tool = ev.get("tool_name") or ""
    inp = ev.get("tool_input") or {}
    cwd = os.path.realpath(ev.get("cwd") or os.getcwd())
except Exception:
    deny("Unreadable hook input.")

def resolve(p):
    return os.path.realpath(os.path.join(cwd, p))

def inside(p):
    rp = resolve(p)
    return rp == cwd or rp.startswith(cwd + os.sep)

def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

if tool in ("Read", "Glob", "Grep"):
    p = inp.get("file_path" if tool == "Read" else "path") or "."
    if inside(p):
        sys.exit(0)
    deny("Only files inside the task directory may be read.")
if tool == "Edit":
    p = inp.get("file_path") or ""
    full = os.path.normpath(os.path.join(cwd, p))
    if not p or not inside(p) or resolve(p) != full:
        deny("Edit only regular files inside the task directory, never through a symlink.")
    if os.path.lexists(full) and not os.path.isfile(full):
        deny("Edit only regular files.")
    if full == os.path.join(cwd, "run_tests.sh"):
        deny("run_tests.sh must not be changed.")
    sys.exit(0)
if tool == "Bash":
    if (inp.get("command") or "").strip() not in ("bash run_tests.sh", "bash ./run_tests.sh"):
        deny("Only the command bash run_tests.sh may be run here.")
    r = os.path.join(cwd, "run_tests.sh")
    if os.path.islink(r) or not os.path.isfile(r):
        deny("run_tests.sh is missing or not a regular file here.")
    want = {}
    try:
        for line in open(sys.argv[1]):
            s, _, path = line.rstrip("\n").partition("  ")
            want[path] = s
        ok = want.get(r) == sha256(r)
    except Exception:
        ok = False
    if not ok:
        deny("run_tests.sh is not the copy the bake-off wrote.")
    sys.exit(0)
deny("Tool %s is not available in this test; use Read, Edit or bash run_tests.sh." % tool)
EOF
    chmod 444 "$1"
}
set_claude_task_flags() {   # gate-path manifest
    local settings
    settings="$(python3 -I -c 'import json, shlex, sys; print(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 -I " + shlex.quote(sys.argv[1]) + " " + shlex.quote(sys.argv[2])}]}]}}))' "$1" "$2")"
    CLAUDE_TASK_FLAGS=(--setting-sources "" --strict-mcp-config --permission-mode dontAsk --settings "$settings"
        --disallowedTools "${DENIED_TOOLS[@]}" --allowedTools "${ALLOWED_TOOLS[@]}")
}

# krull-claude puts --permission-mode acceptEdits BEFORE our args in -p mode,
# so the CLI must take the last one. Checked against a dead endpoint: only
# the init event is read and nothing leaves the machine.
claude_flags_check() {
    local t got
    t="$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/krull-bakeoff-flags.XXXXXX")" || return 1
    write_bash_gate "$t/bash-gate"; set_claude_task_flags "$t/bash-gate" "$t/runners.sha256"
    got="$(cd "$t" && ANTHROPIC_BASE_URL=http://127.0.0.1:9 ANTHROPIC_AUTH_TOKEN=unused \
        DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
        timeout 30 claude --permission-mode acceptEdits -p "unused" --output-format stream-json \
        --verbose --max-turns 1 "${CLAUDE_TASK_FLAGS[@]}" < /dev/null 2>/dev/null | python3 -I -c '
import json, sys
for line in sys.stdin:
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        print("mode=%s mcp=%d" % (ev.get("permissionMode"), len(ev.get("mcp_servers") or [])))
        break
')"
    remove_test_transcripts "$t" > /dev/null
    rm -rf -- "${t:?}"
    echo "${got:-no init event}"
    [ "$got" = "mode=dontAsk mcp=0" ]
}

# ── Side writes krull-claude makes outside the run dir ───────────────────────

SESSION_FILE="$HOME/.krull-session"
SESSION_BACKUP=""   # set when ~/.krull-session existed before the run
restore_session_file() {
    if [ -n "$SESSION_BACKUP" ] && [ -f "$SESSION_BACKUP" ]; then
        cp -p "$SESSION_BACKUP" "$SESSION_FILE"
    else
        rm -f -- "$SESSION_FILE"
    fi
}

# Claude Code keeps a transcript per cwd under projects/<cwd, non-alnum -> ->.
# Deletes the ones for a temp dir and everything below it; prints the count.
remove_test_transcripts() {   # temp-dir
    local proj="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects" prefix p n=0
    prefix="$(printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g')"
    case "$prefix" in *krull-bakeoff*) ;; *) echo 0; return 0 ;; esac
    for p in "${proj:?}/${prefix:?}" "${proj:?}/${prefix:?}"-*; do
        [ -d "$p" ] && rm -rf -- "$p" && n=$((n + 1))
    done
    echo "$n"
}

installed_models() {   # one name per line
    ollama_get /api/tags | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for m in d.get("models") or []:
    print(m.get("name", ""))
'
}

# Mirror of krull-home resolveInstalledTag: exact tag, then the
# preferred-quant variant pull-model may have installed, then any "<tag>-*".
resolve_installed_tag() {
    local tag="$1" installed quant
    installed="$(installed_models)"
    quant="$(env_get OLLAMA_PREFERRED_QUANT)"
    if grep -qxF "$tag" <<<"$installed"; then echo "$tag"; return 0; fi
    if [ -n "$quant" ] && grep -qxF "$tag-$quant" <<<"$installed"; then echo "$tag-$quant"; return 0; fi
    local v
    v="$(awk -v p="$tag-" 'index($0, p) == 1 { print; exit }' <<<"$installed")"
    [ -n "$v" ] && { echo "$v"; return 0; }
    return 1
}

approx_size_gb() {   # sizes from ollama.com, for the disk check only
    case "$1" in
        gemma4:e2b) echo 4 ;;
        gemma4:e4b) echo 10 ;;
        qwen3.6:35b-a3b-coding) echo 24 ;;
        qwen3.6:27b-q4_K_M) echo 17 ;;
        *) echo 0 ;;
    esac
}

ps_names() {   # Ollama /api/ps JSON on stdin -> one loaded model name per line
    python3 -I -c '
import json, sys
try:
    print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("models") or []))
except Exception:
    pass
'
}

unload_all() {
    local m
    for m in $(ollama_get /api/ps | ps_names); do
        ollama_post /api/generate "$(jobj model "$m" keep_alive 0)" 30 >/dev/null
    done
    sleep 3
}

# JSON built from argv, never by string interpolation. All values are strings.
jobj() {   # key value [key value ...] -> {"key": "value", ...}
    python3 -I -c 'import json, sys; a = sys.argv[1:]; print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
}
jenv() {   # key value ... -> {"values": {...}} for PUT /api/env; __ABSENT__ values are left out
    python3 -I -c '
import json, sys
a = sys.argv[1:]
print(json.dumps({"values": {k: v for k, v in zip(a[::2], a[1::2]) if v != "__ABSENT__"}}))
' "$@"
}

# ── Preflight (shared by --dry-run and the real run) ─────────────────────────

PREFLIGHT_FAIL=0
pf() {   # status label detail
    printf '  %-5s %-28s %s\n' "$1" "$2" "${3:-}"
    [ "$1" = "FAIL" ] && PREFLIGHT_FAIL=$((PREFLIGHT_FAIL + 1))
    return 0
}

preflight() {
    echo "Prerequisites:"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        pf FAIL "platform" "macOS: bake-off needs nvidia-smi and the krull-ollama container (Linux only)"
    else
        pf OK "platform" "Linux"
    fi
    local t
    for t in docker curl python3 timeout getent df bwrap flock; do
        if command -v "$t" >/dev/null 2>&1; then pf OK "tool: $t"; else pf FAIL "tool: $t" "not found"; fi
    done
    local why
    if ! command -v bwrap >/dev/null 2>&1; then
        pf FAIL "bwrap sandbox" "bwrap missing: model-edited code would have no sandbox"
    elif why="$(sandbox_selftest)"; then
        pf OK "bwrap sandbox" "home write, network, docker socket and runner swap blocked inside"
    else
        pf FAIL "bwrap sandbox" "$why"
    fi
    if command -v claude >/dev/null 2>&1; then
        pf OK "tool: claude" "(used by krull-claude)"
        if why="$(claude_flags_check)"; then
            pf OK "claude containment flags" "$why (last --permission-mode wins)"
        else
            pf FAIL "claude containment flags" "$why (want mode=dontAsk mcp=0)"
        fi
    else
        pf FAIL "tool: claude" "Claude Code CLI not on PATH"
    fi
    if [ -x "$KRULL_CLAUDE" ]; then pf OK "scripts/krull-claude"; else pf FAIL "scripts/krull-claude" "missing or not executable"; fi
    if [ -f "$ENV_FILE" ]; then pf OK ".env" "present"; else pf FAIL ".env" "missing (run ./krull start once)"; fi
    if [ -f "$LITELLM_CONFIG" ]; then pf OK "litellm/config.yaml"; else pf FAIL "litellm/config.yaml" "missing"; fi

    if gpu_ok; then
        pf OK "nvidia-smi" "$(timeout 10 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)"
    else
        pf FAIL "nvidia-smi" "GPU not reachable (driver not loaded?)"
    fi

    DOCKER_UP=0
    if docker_ok; then
        DOCKER_UP=1; pf OK "docker" "daemon reachable"
        local c
        for c in "${REQUIRED_CONTAINERS[@]}"; do
            if container_running "$c"; then pf OK "container $c" "running"; else pf FAIL "container $c" "not running (./krull start)"; fi
        done
        if curl -sf -o /dev/null --max-time 5 "$HOME_API/api/models"; then
            pf OK "krull-home API" "$HOME_API"
        else
            pf FAIL "krull-home API" "$HOME_API unreachable"
        fi
    else
        pf FAIL "docker" "daemon not reachable (socket permission or daemon down)"
        pf SKIP "containers" "cannot check without docker"
        pf SKIP "krull-home API" "cannot check without the stack"
    fi

    # Disk: models not yet installed must fit on the data/ollama filesystem.
    local need=0 m installed="" unknown=""
    [ "$DOCKER_UP" -eq 1 ] && installed="$(installed_models 2>/dev/null)"
    NEED_PULL=()
    for m in "${MODELS[@]}"; do
        if [ -n "$installed" ] && awk -v t="$m" '$0 == t || index($0, t "-") == 1 { f = 1 } END { exit !f }' <<<"$installed"; then
            continue
        fi
        NEED_PULL+=("$m")
        local s; s="$(approx_size_gb "$m")"
        [ "$s" -eq 0 ] && unknown="$unknown $m"
        need=$((need + s))
    done
    local avail_gb
    avail_gb="$(df -BG --output=avail "$PROJECT_DIR/data/ollama" 2>/dev/null | tail -1 | tr -dc '0-9')"
    local need_margin=$(( need + need / 10 + 5 ))
    local basis="installed list read from Ollama"
    [ -z "$installed" ] && basis="Ollama unreachable, assuming every model needs pulling"
    if [ "${#NEED_PULL[@]}" -eq 0 ]; then
        pf OK "disk space" "all models already installed"
    elif [ -z "$avail_gb" ]; then
        pf FAIL "disk space" "could not read free space for data/ollama"
    elif [ "$SKIP_PULL" -eq 1 ]; then
        pf SKIP "disk space" "--skip-pull: ${#NEED_PULL[@]} model(s) not installed will be skipped"
    elif [ "$avail_gb" -ge "$need_margin" ]; then
        pf OK "disk space" "${avail_gb} GB free, ~${need_margin} GB needed for ${#NEED_PULL[@]} pull(s) ($basis)"
    else
        pf FAIL "disk space" "${avail_gb} GB free, ~${need_margin} GB needed ($basis)"
    fi
    [ -n "$unknown" ] && pf WARN "disk space" "size unknown for:$unknown (counted as 0 GB)"

    local mem_total mem_avail
    mem_total="$(awk '/^MemTotal:/{printf "%.1f", $2/1048576}' /proc/meminfo 2>/dev/null)"
    mem_avail="$(awk '/^MemAvailable:/{printf "%.1f", $2/1048576}' /proc/meminfo 2>/dev/null)"
    pf INFO "system RAM" "${mem_total:-?} GiB total, ${mem_avail:-?} GiB available"
    pf INFO "external DNS" "$(dns_state) (checklist asks for the network to be disconnected during tests)"
    echo ""
}

# ── Dry run: the full plan, executing nothing that changes state ─────────────

print_plan() {
    echo "Plan (dry run — nothing below is executed):"
    echo "  1. Record .env OLLAMA_MODEL='$(env_get OLLAMA_MODEL)' OLLAMA_NUM_CTX='$(env_get OLLAMA_NUM_CTX)' CONTEXT_COMPACT_LIMIT='$(env_get CONTEXT_COMPACT_LIMIT)'"
    echo "     and copy litellm/config.yaml (sha256 $(sha256sum "$LITELLM_CONFIG" 2>/dev/null | cut -c1-12)) to the run dir."
    echo "     Snapshot .env (0600, private temp dir, never the run dir), its mode/owner, ~/.krull-session,"
    echo "     and the krull-ollama / krull-sse-proxy environment (in memory only)."
    echo "     Install an EXIT/INT/TERM/HUP trap that restores all of it."
    echo "  2. Take data/bakeoff/.lock (flock; a second bake-off refuses to start). Create"
    echo "     data/bakeoff/<timestamp>-<pid>/ (self-ignoring .gitignore, so ./krull update never sees"
    echo "     it as a dirty tree) and a temp project under \${XDG_CACHE_HOME:-~/.cache} with the"
    echo "     bwrap wrapper sandbox-run beside the task dirs."
    if [ "$SKIP_PULL" -eq 1 ]; then
        echo "  3. Pull: skipped (--skip-pull); models not installed are marked SKIP."
    else
        echo "  3. Pull missing models with: ./krull pull-model <tag>   (needs network)"
        local m; for m in "${NEED_PULL[@]}"; do echo "       ./krull pull-model $m"; done
        [ "$PULL_ONLY" -eq 1 ] && { echo "     --pull-only: stop here."; echo ""; return; }
    fi
    echo "  4. Print the 'disconnect the network' reminder; record whether external DNS resolves."
    echo "  5. For each model: ${MODELS[*]}"
    echo "     a. Unload everything from Ollama (keep_alive 0) and read baseline VRAM (nvidia-smi)."
    echo "     b. Context ladder, ascending, stop at first failure:"
    local ladder=() c; for c in "${CTX_LADDER[@]}"; do [ "$c" -le "$MAX_CTX" ] && ladder+=("$c"); done
    echo "          num_ctx ${ladder[*]} (capped at the model's native context)"
    echo "        each rung: POST $OLLAMA_INTERNAL/api/generate via krull-home (num_predict 256, think false)"
    echo "        -> tokens/sec = eval_count / eval_duration; VRAM = nvidia-smi; GPU share = /api/ps size_vram/size"
    echo "     c. test ctx = min(max loaded ctx, $TEST_CTX_CAP); compact limit = 3/4 of it."
    echo "        PUT $HOME_API/api/env {OLLAMA_NUM_CTX, CONTEXT_COMPACT_LIMIT}  (recreates krull-ollama, krull-sse-proxy)"
    echo "        POST $HOME_API/api/models/select {key: <installed tag>}   (OLLAMA_MODEL, litellm/config.yaml, restarts krull-litellm)"
    echo "        wait for Ollama, SSE proxy, LiteLLM, Open WebUI health."
    echo "     d. Open WebUI simple chat: POST /api/chat/completions inside krull-webui."
    echo "     e. krull-claude -p in the temp project (stream-json), --setting-sources \"\" --strict-mcp-config"
    echo "        --permission-mode dontAsk; allowed: ${ALLOWED_TOOLS[*]}; denied: ${DENIED_TOOLS[*]};"
    echo "        a PreToolUse hook (--settings, matcher *) allows only Read/Glob/Grep/Edit inside the task dir and"
    echo "        bash run_tests.sh when it matches the sha256 recorded at write time; every other tool is denied."
    echo "        run_tests.sh is 0444 and re-execs itself under sandbox-run (no network, / read-only,"
    echo "        only its task dir writable, run_tests.sh itself bound read-only); the script's own re-run uses it too."
    echo "          basic prompt      -> reply contains READY"
    echo "          Read              -> Read tool used, codeword from notes/briefing.txt reported"
    echo "          Edit              -> Edit tool used, settings.conf matches the expected bytes"
    echo "          Bash              -> Bash tool used, nonce from run_tests.sh reported"
    echo "          full loop x$LOOPS     -> fix calc.sh, tests re-run after the edit, bash run_tests.sh exits 0,"
    echo "                               run_tests.sh untouched, no loop/stall markers; pass only if $LOOPS/$LOOPS"
    echo "          near-limit        -> read part files until the proxy (or Claude Code) compacts;"
    echo "                               pass if compaction logged, last KEY reported, no EMPTY OUTPUT/[ERROR]"
    echo "        After each: ./krull logs session <sid> saved and scanned for loop/stall markers."
    echo "     f. ./krull logs errors 50 saved per model."
    echo "  6. Restore (also on failure/Ctrl-C/hangup): PUT /api/env originals, POST /api/models/select original,"
    echo "     verify .env keys, put .env back byte-for-byte (mode/owner too) when only those keys differ,"
    echo "     put litellm/config.yaml back (temp file + mv), compose up + restart krull-litellm if anything was"
    echo "     restored outside the API, then wait out any krull-home recreate and force-recreate krull-ollama /"
    echo "     krull-sse-proxy if their environment differs from before. Unload (keep_alive 0) any model the run"
    echo "     loaded except the restored OLLAMA_MODEL. Put ~/.krull-session back and delete"
    echo "     the test sessions' transcripts under ~/.claude/projects."
    echo "  7. Write data/bakeoff/<timestamp>/results.md + results.tsv, apply the decision rule, print the winner"
    echo "     (fallback stays $FALLBACK_MODEL)."
    echo ""
}

# ── Restore (EXIT trap) ──────────────────────────────────────────────────────

ORIG_MODEL=""; ORIG_CTX=""; ORIG_COMPACT=""
HAS_MODEL=0; HAS_CTX=0; HAS_COMPACT=0
CONFIG_BACKUP=""; CONFIG_SHA=""
ENV_SNAPSHOT=""                       # $PRIVATE_DIR/env.orig, 0600
ORIG_ENV_OLLAMA=""; ORIG_ENV_PROXY=""  # pre-run container env, memory only
SELECT_OK=0
RESTORE_NOTES=()

# A Ctrl-C during PUT /api/env kills curl, not krull-home's recreate, which
# can then finish AFTER the restore and leave test values in a container.
# Wait for any in-flight recreate (krull-home parks the old container as
# <name>__old), then compare each container's env with its pre-run copy and
# force-recreate from the restored .env where it differs.
verify_container_env() {
    local waited=0 c svc orig cur names
    while [ "$waited" -lt 180 ] && \
          docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qE '^krull-(ollama|sse-proxy)__old$'; do
        sleep 3; waited=$((waited + 3))
    done
    for c in krull-ollama krull-sse-proxy; do
        case "$c" in
            krull-ollama) svc=ollama; orig="$ORIG_ENV_OLLAMA" ;;
            krull-sse-proxy) svc=sse-proxy; orig="$ORIG_ENV_PROXY" ;;
        esac
        [ -n "$orig" ] || continue
        cur="$(container_env "$c")"
        if [ "$cur" = "$orig" ]; then
            RESTORE_NOTES+=("verified: $c environment matches its pre-run state")
            continue
        fi
        names="$(env_diff_names "$orig" "$cur")"
        log "  $c environment differs from pre-run (${names}); force-recreating"
        compose up -d --force-recreate --no-deps "$svc" >> "$RUN_DIR/restore.log" 2>&1
        cur="$(container_env "$c")"
        if [ "$cur" = "$orig" ]; then
            RESTORE_NOTES+=("$c force-recreated: environment (${names}) back to its pre-run state")
        else
            RESTORE_NOTES+=("WARNING: $c environment still differs from pre-run ($(env_diff_names "$orig" "$cur")) — run ./krull start")
        fi
    done
}

# PUT /api/env answers 200 even when a recreate failed; it reports that in
# recreateErrors. Prints the non-empty object (first 300 chars); false when
# there is none or the reply is not JSON.
recreate_errors() {   # reply.json
    python3 -I -c '
import json, sys
try:
    e = json.load(open(sys.argv[1])).get("recreateErrors")
except Exception:
    sys.exit(1)
if not e:
    sys.exit(1)
print(json.dumps(e)[:300])
' "$1"
}

# Direct .env edit of the three bake-off keys where they differ from the
# pre-run values. True only when it rewrote .env (containers then need
# recreating); false when nothing differed or the write failed (noted).
restore_env_keys() {   # label for the notes
    local fix=() k orig has cur
    for k in OLLAMA_MODEL OLLAMA_NUM_CTX CONTEXT_COMPACT_LIMIT; do
        case "$k" in
            OLLAMA_MODEL) orig="$ORIG_MODEL"; has=$HAS_MODEL ;;
            OLLAMA_NUM_CTX) orig="$ORIG_CTX"; has=$HAS_CTX ;;
            CONTEXT_COMPACT_LIMIT) orig="$ORIG_COMPACT"; has=$HAS_COMPACT ;;
        esac
        if [ "$has" -eq 1 ]; then
            cur="$(env_get "$k")"
            if ! env_has "$k" || [ "$cur" != "$orig" ]; then fix+=("$k=$orig"); fi
        elif env_has "$k"; then
            fix+=("$k=__ABSENT__")
        fi
    done
    if [ ${#fix[@]} -eq 0 ]; then
        RESTORE_NOTES+=("$1: OLLAMA_MODEL, OLLAMA_NUM_CTX, CONTEXT_COMPACT_LIMIT match their pre-run values")
        return 1
    fi
    if env_set_direct "${fix[@]}"; then
        RESTORE_NOTES+=("$1: direct .env restore of: ${fix[*]%%=*}")
        return 0
    fi
    RESTORE_NOTES+=("$1: FAILED to restore .env keys: ${fix[*]%%=*} — fix them by hand from env.before")
    return 1
}

# .env bytes: krull-home rewrites the whole file (normalized, mode 0644).
# Put the snapshot back unless some other key changed during the run. A
# failed byte restore keeps the snapshot (release_env_snapshot).
ENV_SNAPSHOT_KEEP=0
restore_env_snapshot() {
    [ -n "$ENV_SNAPSHOT" ] && [ -f "$ENV_SNAPSHOT" ] || return 0
    local st
    st="$(env_restore_bytes "$ENV_SNAPSHOT" OLLAMA_MODEL OLLAMA_NUM_CTX CONTEXT_COMPACT_LIMIT)"
    case "$st" in
        same) RESTORE_NOTES+=("verified: .env is byte-identical to its pre-run state") ;;
        restored) RESTORE_NOTES+=(".env restored byte-for-byte from the pre-run snapshot") ;;
        other-keys-changed:*) RESTORE_NOTES+=(".env: other keys changed during the run (${st#*:}); kept them, restored only the bake-off keys") ;;
        *) ENV_SNAPSHOT_KEEP=1
           RESTORE_NOTES+=("WARNING: byte-for-byte .env restore failed; the bake-off keys were checked above") ;;
    esac
    local now
    now="$(stat -c '%a %u %g' "$ENV_FILE" 2>/dev/null)"
    if [ -n "$ENV_MODE" ] && [ "$now" != "$ENV_MODE $ENV_UID $ENV_GID" ]; then
        chmod "$ENV_MODE" "$ENV_FILE" 2>/dev/null
        now="$(stat -c '%a %u %g' "$ENV_FILE" 2>/dev/null)"
        [ "$now" = "$ENV_MODE $ENV_UID $ENV_GID" ] \
            || RESTORE_NOTES+=("WARNING: .env mode/uid/gid is now '$now', was '$ENV_MODE $ENV_UID $ENV_GID'")
    fi
}

# The snapshot holds secrets, so it is deleted once the restore is done,
# unless a byte restore failed. Then it is moved out of the temp dir (which
# finish deletes) to a 0600 file beside it, and the notes say where.
release_env_snapshot() {
    [ -n "$ENV_SNAPSHOT" ] && [ -f "$ENV_SNAPSHOT" ] || return 0
    if [ "$ENV_SNAPSHOT_KEEP" -eq 1 ]; then
        local keep="${XDG_CACHE_HOME:-$HOME/.cache}/krull-bakeoff-env.$TS"
        if mv -- "$ENV_SNAPSHOT" "$keep" && chmod 600 "$keep"; then
            RESTORE_NOTES+=("pre-run .env snapshot KEPT at $keep (0600, holds secrets): compare it with .env, then delete it")
        else
            RESTORE_NOTES+=("WARNING: could not keep the pre-run .env snapshot; it was deleted with the temp dir")
        fi
    else
        rm -f -- "$ENV_SNAPSHOT"
    fi
    ENV_SNAPSHOT=""
}

note_config_state() {
    if [ "$(sha256sum "$LITELLM_CONFIG" | cut -d' ' -f1)" = "$CONFIG_SHA" ]; then
        RESTORE_NOTES+=("verified: litellm/config.yaml matches its pre-run bytes")
    else
        RESTORE_NOTES+=("WARNING: litellm/config.yaml differs from its pre-run copy at $CONFIG_BACKUP")
    fi
}

recreate_and_restart_litellm() {   # restart-litellm(0|1) need-up(0|1)
    if [ "$2" -eq 1 ]; then
        if compose up -d ollama sse-proxy litellm >> "$RUN_DIR/restore.log" 2>&1; then
            RESTORE_NOTES+=("docker compose up -d ollama sse-proxy litellm: ok")
        else
            RESTORE_NOTES+=("docker compose up failed — run ./krull start to apply the restored .env")
        fi
    fi
    if [ "$2" -eq 1 ] || [ "$1" -eq 1 ]; then
        docker restart krull-litellm >> "$RUN_DIR/restore.log" 2>&1 \
            && RESTORE_NOTES+=("krull-litellm restarted") \
            || RESTORE_NOTES+=("krull-litellm restart failed — run: docker restart krull-litellm")
    fi
}

# Called only from finish, which has already set INT/TERM/HUP/PIPE to ignore.
restore_settings() {
    [ "$RUN_STARTED" -eq 1 ] || return 0
    [ "$RESTORED" -eq 1 ] && return 0
    RESTORED=1
    log ""
    local n

    # Nothing switched the stack (stopped in preflight-to-ladder, or during a
    # context ladder): no API call, no .env write, no container touched.
    if [ "$STACK_TOUCHED" -eq 0 ]; then
        log "Checking settings (the stack was never switched, so nothing is restored)..."
        RESTORE_NOTES+=("stack never switched: restore API calls, .env rewrite and container checks skipped")
        if [ -n "$ENV_SNAPSHOT" ] && cmp -s "$ENV_SNAPSHOT" "$ENV_FILE"; then
            RESTORE_NOTES+=("verified: .env is byte-identical to its pre-run state")
        else
            RESTORE_NOTES+=("WARNING: .env changed during the run, not by the bake-off (it never switched the stack); left as it is")
        fi
        release_env_snapshot
        note_config_state
        for n in "${RESTORE_NOTES[@]}"; do log "  $n"; done
        return 0
    fi

    log "Restoring original settings..."
    local need_up=0 errs

    # 1. Settings-page path: same API the page uses, so containers are recreated.
    #    A failed PUT or a non-empty recreateErrors sends us to the compose path.
    local vals
    vals="$(jenv OLLAMA_NUM_CTX "$([ $HAS_CTX -eq 1 ] && echo "$ORIG_CTX" || echo __ABSENT__)" \
        CONTEXT_COMPACT_LIMIT "$([ $HAS_COMPACT -eq 1 ] && echo "$ORIG_COMPACT" || echo __ABSENT__)")"
    if ! curl -sf -o "$RUN_DIR/restore-env.json" --max-time 300 -X PUT "$HOME_API/api/env" \
            -H 'Content-Type: application/json' -d "$vals"; then
        RESTORE_NOTES+=("PUT /api/env failed; using direct fallback")
        need_up=1
    elif errs="$(recreate_errors "$RUN_DIR/restore-env.json")"; then
        RESTORE_NOTES+=("PUT /api/env saved the keys but recreate failed: $errs; recreating with compose")
        need_up=1
    else
        RESTORE_NOTES+=("PUT /api/env with the original context keys: ok")
    fi
    wait_for "Ollama" 90 check_ollama
    if [ $HAS_MODEL -eq 1 ] && [ -n "$ORIG_MODEL" ]; then
        if curl -sf -o "$RUN_DIR/restore-select.json" --max-time 300 -X POST "$HOME_API/api/models/select" \
                -H 'Content-Type: application/json' -d "$(jobj key "$ORIG_MODEL")"; then
            SELECT_OK=1
            RESTORE_NOTES+=("POST /api/models/select original model: ok")
        else
            RESTORE_NOTES+=("POST /api/models/select original model failed; using direct fallback")
        fi
    fi

    # 2. Verify the three keys and fall back to a direct restore where the API did not land.
    restore_env_keys "key check" && need_up=1

    # 3. .env bytes, mode and owner.
    restore_env_snapshot

    # 4. litellm/config.yaml: pre-run bytes via temp file + mv (./litellm is a
    #    directory bind mount, so the container sees the new inode on restart).
    #    Keeps the tracked file clean (PAT-2026-08-16: a changed tracked file
    #    blocks ./krull update).
    local restart_litellm=0 tmp
    if [ -n "$CONFIG_BACKUP" ] && [ -f "$CONFIG_BACKUP" ] && \
       [ "$(sha256sum "$LITELLM_CONFIG" | cut -d' ' -f1)" != "$CONFIG_SHA" ]; then
        tmp="$(dirname "$LITELLM_CONFIG")/.config.yaml.bakeoff.$$"
        if cp -p "$CONFIG_BACKUP" "$tmp" && mv -f "$tmp" "$LITELLM_CONFIG"; then
            restart_litellm=1
            RESTORE_NOTES+=("litellm/config.yaml restored from the pre-run copy")
        else
            rm -f -- "$tmp"
            RESTORE_NOTES+=("FAILED to write litellm/config.yaml back; the pre-run copy is $CONFIG_BACKUP")
        fi
    fi

    # 5. Containers. `compose up -d` does not recreate krull-litellm for a
    #    config.yaml change (a bind mount, not part of its compose config), so
    #    on the fallback path litellm is restarted unconditionally.
    recreate_and_restart_litellm "$restart_litellm" "$need_up"
    [ "$SELECT_OK" -eq 1 ] || RESTORE_NOTES+=("Open WebUI default model was NOT reset (the restore did not go through POST /api/models/select); pick ${ORIG_MODEL:-your model} again in Settings")

    # 6. Catch a recreate that raced the restore.
    verify_container_env

    # 7. A krull-home .env write that finished during step 6 can still change
    #    the keys: check them once more and re-apply if so.
    if restore_env_keys "re-check after the container check"; then
        restore_env_snapshot
        recreate_and_restart_litellm 1 1
    fi

    release_env_snapshot
    note_config_state
    for n in "${RESTORE_NOTES[@]}"; do log "  $n"; done
}

# A run stopped mid-ladder or mid-task leaves the candidate loaded for its
# 30m keep_alive. Unload what this run loaded, except the model the stack is
# back on. No-op when the run loaded nothing or Ollama does not answer; every
# call is under `timeout`, so exit waits about 10s per loaded model at most.
unload_run_models() {
    [ "${#LOADED_TAGS[@]}" -gt 0 ] || return 0
    local keep="" ps m t loaded n=0
    [ "$HAS_MODEL" -eq 1 ] && keep="$ORIG_MODEL"
    ps="$(timeout 10 docker exec krull-home curl -s --max-time 5 "$OLLAMA_INTERNAL/api/ps" 2>/dev/null)"
    if [ -z "$ps" ]; then
        RESTORE_NOTES+=("Ollama did not answer: models this run loaded were left to expire on their keep_alive")
        log "  ${RESTORE_NOTES[${#RESTORE_NOTES[@]}-1]}"
        return 0
    fi
    for m in $(ps_names <<<"$ps"); do
        loaded=0   # a loop, not printf | grep -q: under pipefail an early grep exit fails the pipe
        for t in "${LOADED_TAGS[@]}"; do [ "$t" = "$m" ] && loaded=1; done
        [ "$loaded" -eq 1 ] || continue
        # The restored OLLAMA_MODEL may be installed as "<tag>:latest" or "<tag>-<quant>".
        [ -n "$keep" ] && { [ "$m" = "$keep" ] || [ "$m" = "$keep:latest" ] || [[ "$m" == "$keep-"* ]]; } && continue
        jobj model "$m" keep_alive 0 | timeout 10 docker exec -i krull-home curl -s --max-time 8 \
            -X POST "$OLLAMA_INTERNAL/api/generate" -H 'Content-Type: application/json' \
            --data-binary @- >/dev/null 2>&1 && n=$((n + 1))
    done
    RESTORE_NOTES+=("unloaded $n model(s) this run had loaded in Ollama (keep_alive 0); kept ${keep:-none}")
    log "  ${RESTORE_NOTES[${#RESTORE_NOTES[@]}-1]}"
}

finish() {
    trap '' INT TERM HUP PIPE   # a second Ctrl-C, a hangup or a closed pipe must not cut the restore short
    local rc="${1:-0}"
    trap - EXIT
    if [ "$RUN_STARTED" -eq 1 ]; then
        restore_settings
        unload_run_models
        restore_session_file
        local n; n="$(remove_test_transcripts "$WORK_DIR")"
        RESTORE_NOTES+=("~/.krull-session put back; $n Claude Code transcript dir(s) of the test sessions deleted")
        log "  ${RESTORE_NOTES[${#RESTORE_NOTES[@]}-1]}"
        if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
            # Sandboxed code may have chmod-ed its own task dir; undo before removing.
            chmod -R u+rwX "$WORK_DIR" 2>/dev/null
            rm -rf -- "${WORK_DIR:?}"
        fi
        write_results "$rc"
    fi
    exit "$rc"
}

# ── Fixtures for the krull-claude tasks ──────────────────────────────────────

rand_word() {
    local words=(HELIOTROPE MARIGOLD OBSIDIAN TAMARACK QUILLWORT BASALT CORMORANT SAXIFRAGE)
    echo "${words[RANDOM % ${#words[@]}]}-$((RANDOM % 900 + 100))"
}

make_loop_fixture() {   # dir
    mkdir -p "$1"
    cat > "$1/calc.sh" <<'EOF'
#!/bin/bash
# Small arithmetic helpers used by run_tests.sh.
add() { echo $(( $1 - $2 )); }
multiply() { echo $(( $1 * $2 )); }
EOF
    write_runner "$1" "$SANDBOX_RUN" "$(cat <<'EOF'
cd "$(dirname "$0")" || exit 2
source ./calc.sh
fail=0
check() {
    if [ "$2" = "$3" ]; then echo "PASS: $1"; else echo "FAIL: $1 (expected $3, got $2)"; fail=1; fi
}
check "add 2 3" "$(add 2 3)" 5
check "add 10 4" "$(add 10 4)" 14
check "multiply 3 4" "$(multiply 3 4)" 12
if [ "$fail" -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "TESTS FAILED"; exit 1; fi
EOF
)"
}

make_near_fixture() {   # dir parts -> prints last key
    python3 -I -c '
import os, random, sys
d, n = sys.argv[1], int(sys.argv[2])
os.makedirs(d, exist_ok=True)
rng = random.Random(n)
vocab = ("river stone lantern harbor meadow copper signal valley orchard ember glacier "
         "compass thicket beacon granite willow tide summit hollow falcon").split()
last = ""
for i in range(1, n + 1):
    key = "%s%03d" % (rng.choice(["AMBER", "CEDAR", "DELTA", "FJORD", "GARNET", "INDIGO"]), rng.randrange(1000))
    lines = [" ".join(rng.choice(vocab) for _ in range(16)) for _ in range(110)]
    lines.insert(rng.randrange(10, 100), "KEY: " + key)
    with open(os.path.join(d, "part%02d.txt" % i), "w") as f:
        f.write("\n".join(lines) + "\n")
    last = key
print(last)
' "$1" "$2"
}

# ── krull-claude task runner ─────────────────────────────────────────────────

PARSE_STREAM='
import json, sys
tools, res, compact, mode = [], None, 0, ""
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    t = ev.get("type")
    if t == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                tools.append(c.get("name", ""))
    elif t == "system" and ev.get("subtype") == "compact_boundary":
        compact = 1
    elif t == "system" and ev.get("subtype") == "init":
        mode = ev.get("permissionMode") or ""
    elif t == "result":
        res = ev
r = res or {}
edits = [i for i, n in enumerate(tools) if n in ("Edit", "MultiEdit", "Write")]
bash_after = int(bool(edits) and "Bash" in tools[edits[-1] + 1:])
with open(sys.argv[2], "w") as f:
    f.write(str(r.get("result") or ""))
print("tools=" + ",".join(tools))
print("result_seen=%d" % (res is not None))
print("is_error=%d" % (1 if r.get("is_error") else 0))
print("subtype=%s" % (r.get("subtype") or "none"))
print("turns=%s" % (r.get("num_turns") or 0))
print("denials=%d" % len(r.get("permission_denials") or []))
print("compact=%d" % compact)
print("bash_after_edit=%d" % bash_after)
print("mode=%s" % mode)
'

# Sets: T_RC T_SID T_RESULT T_LOOPS T_HARD T_COMPACT_LOG P_TOOLS P_RESULT_SEEN
#       P_IS_ERROR P_SUBTYPE P_TURNS P_DENIALS P_COMPACT P_BASH_AFTER_EDIT P_MODE
run_claude_task() {   # name dir prompt timeout max-turns
    local name="$1" dir="$2" prompt="$3" limit="$4" turns="$5"
    local raw="$MODEL_RAW/$name"
    log "    task $name (timeout ${limit}s)"
    ( cd "$dir" && timeout "$limit" "$KRULL_CLAUDE" -p "$prompt" \
        --output-format stream-json --verbose --max-turns "$turns" \
        "${CLAUDE_TASK_FLAGS[@]}" ) > "$raw.stream.jsonl" 2> "$raw.stderr.log" < /dev/null
    T_RC=$?
    # krull-claude's detached exit watcher (the "Clean up the session file"
    # block in scripts/krull-claude) removes
    # ~/.krull-session up to a second after claude exits, and only if it still
    # holds this test session's id. Putting the pre-run file back here at once
    # is still needed (the watcher deletes, it never restores), and the
    # watcher then leaves the restored file alone: its id no longer matches.
    restore_session_file
    T_SID="$(sed -n 's/^krull-claude session: //p' "$raw.stream.jsonl" | head -1)"
    P_TOOLS=""; P_RESULT_SEEN=0; P_IS_ERROR=1; P_SUBTYPE=none; P_TURNS=0
    P_DENIALS=0; P_COMPACT=0; P_BASH_AFTER_EDIT=0; P_MODE=""
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            tools) P_TOOLS="$v" ;; result_seen) P_RESULT_SEEN="$v" ;; is_error) P_IS_ERROR="$v" ;;
            subtype) P_SUBTYPE="$v" ;; turns) P_TURNS="$v" ;; denials) P_DENIALS="$v" ;;
            compact) P_COMPACT="$v" ;; bash_after_edit) P_BASH_AFTER_EDIT="$v" ;; mode) P_MODE="$v" ;;
        esac
    done < <(python3 -I -c "$PARSE_STREAM" "$raw.stream.jsonl" "$raw.result.txt")
    # Belt to the preflight check: a session that ran in any mode but dontAsk
    # had acceptEdits' auto-approved shell commands; stop the bake-off.
    if [ -n "$P_MODE" ] && [ "$P_MODE" != "dontAsk" ]; then
        log "ABORT: task $name ran with permission mode '$P_MODE', not dontAsk; containment not guaranteed."
        exit 3
    fi
    T_RESULT="$(cat "$raw.result.txt" 2>/dev/null)"
    T_LOOPS=0; T_HARD=0; T_COMPACT_LOG=0
    if [ -n "$T_SID" ]; then
        "$KRULL" logs session "$T_SID" > "$raw.session.log" 2>&1
        T_LOOPS="$(grep -cE "$LOOP_RE" "$raw.session.log")"
        T_HARD="$(grep -cE "$HARD_RE" "$raw.session.log")"
        T_COMPACT_LOG="$(grep -cE "$COMPACT_RE" "$raw.session.log")"
        local lines; lines="$(grep -c . "$raw.session.log")"
        [ "$lines" -le 2 ] && note "$name: no proxy log lines for session $T_SID"
    else
        note "$name: no krull-claude session id in output"
    fi
    log "      rc=$T_RC subtype=$P_SUBTYPE turns=$P_TURNS tools=[${P_TOOLS}] denials=$P_DENIALS loop-markers=$T_LOOPS hard-errors=$T_HARD"
}

task_ok() {   # common success: finished, not an error, not timed out
    [ "$T_RC" -eq 0 ] && [ "$P_RESULT_SEEN" -eq 1 ] && [ "$P_IS_ERROR" -eq 0 ]
}
task_why() {
    if [ "$T_RC" -eq 124 ]; then echo "timeout (stall)"
    elif [ "$P_SUBTYPE" = "error_max_turns" ]; then echo "hit max turns"
    elif [ "$P_RESULT_SEEN" -eq 0 ]; then echo "no result (rc=$T_RC)"
    elif [ "$P_IS_ERROR" -eq 1 ]; then echo "error result ($P_SUBTYPE)"
    else echo "rc=$T_RC"; fi
}
has_tool() { [[ ",$P_TOOLS," == *",$1,"* ]]; }

# Task dirs hold whatever the model and the sandbox left there: a symlink to
# a secret, a FIFO that blocks a reader. The host reads or copies a task file
# only when it is a regular file and not a symlink, copies with cp -P under a
# timeout, and scores anything else as a failed task.
regular_file() { [ -f "$1" ] && [ ! -L "$1" ]; }
copy_task_file() {   # src dst
    regular_file "$1" && timeout 10 cp -P -- "$1" "$2"
}

# ── Per-model run ────────────────────────────────────────────────────────────

NOTES=""
note() { NOTES="${NOTES:+$NOTES; }$*"; log "      note: $*"; }

# Columns of results.tsv (header written in main).
emit_row() {
    local out=() f
    for f in "$@"; do f="${f//$'\t'/ }"; out+=("${f//$'\n'/ }"); done
    printf '%s\n' "$(IFS=$'\t'; echo "${out[*]}")" >> "$RESULTS_TSV"
}

gen_probe() {   # tag ctx -> sets G_OK G_TPS G_ERR
    local body resp
    # think:false keeps thinking-capable models (gemma4, qwen3.x) from spending
    # the 256-token budget on a hidden trace (INC-2026-08-15 empty-content bail).
    body="$(python3 -I -c '
import json, sys
print(json.dumps({"model": sys.argv[1], "stream": False, "think": False,
    "prompt": "Write a detailed 300-word description of how a lighthouse works.",
    "options": {"num_ctx": int(sys.argv[2]), "num_predict": 256, "temperature": 0, "seed": 1},
    "keep_alive": "30m"}))
' "$1" "$2")"
    resp="$(ollama_post /api/generate "$body" "$T_LOAD")"
    if grep -q 'does not support thinking' <<<"$resp"; then
        body="$(python3 -I -c 'import json,sys; d=json.loads(sys.argv[1]); d.pop("think"); print(json.dumps(d))' "$body")"
        resp="$(ollama_post /api/generate "$body" "$T_LOAD")"
    fi
    printf '%s\n' "$resp" >> "$MODEL_RAW/ctx-ladder.jsonl"
    local out
    out="$(python3 -I -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("0\t\tno JSON reply (timeout or crash)"); sys.exit(0)
if d.get("error"):
    print("0\t\t" + str(d["error"])[:160]); sys.exit(0)
ec, ed = d.get("eval_count") or 0, d.get("eval_duration") or 0
print("1\t%.1f\t" % (ec / (ed / 1e9)) if ec and ed else "0\t\tno eval stats")
' "$resp")"
    IFS=$'\t' read -r G_OK G_TPS G_ERR <<<"$out"
}

gpu_share() {   # tag -> percent of the model resident in VRAM
    ollama_get /api/ps | python3 -I -c '
import json, sys
try:
    for m in json.load(sys.stdin).get("models") or []:
        if m.get("name") == sys.argv[1] and m.get("size"):
            print("%d" % round(100.0 * m.get("size_vram", 0) / m["size"])); break
except Exception:
    pass
' "$1"
}

native_ctx() {
    ollama_post /api/show "$(jobj model "$1")" 30 | python3 -I -c '
import json, sys
try:
    mi = json.load(sys.stdin).get("model_info") or {}
    print(next(v for k, v in mi.items() if k.endswith(".context_length")))
except Exception:
    print(0)
'
}

run_model() {
    local model="$1"
    local tag="" load=FAIL webui=SKIP basic=SKIP read=SKIP edit=SKIP bash_box=SKIP
    local loop_box=SKIP loop_n=0 near=SKIP
    local nat=0 max_ok=0 test_ctx=0 vram8="" gpu8="" tps8="" vrammax="" gpumax="" tpsmax=""
    NOTES=""
    MODEL_RAW="$RUN_DIR/raw/$(printf '%s' "$model" | tr '/:' '__')"
    mkdir -p "$MODEL_RAW"
    log ""
    log "=== $model ==="

    if ! tag="$(resolve_installed_tag "$model")"; then
        note "not installed (pull it first)"
        emit_row "$model" "-" "$nat" "$max_ok" "$test_ctx" "$vram8" "$gpu8" "$tps8" "$vrammax" "$gpumax" "$tpsmax" \
            SKIP SKIP SKIP SKIP SKIP SKIP "0/$LOOPS" SKIP SKIP "$NOTES"
        return
    fi
    [ "$tag" != "$model" ] && note "installed as $tag"
    LOADED_TAGS+=("$tag")

    # a/b. Context ladder straight against Ollama.
    unload_all
    local base_vram; base_vram="$(vram_used_mib)"
    nat="$(native_ctx "$tag")"; [[ "$nat" =~ ^[0-9]+$ ]] || nat=0
    log "  baseline VRAM ${base_vram:-?} MiB, native context ${nat:-?}"
    local c
    for c in "${CTX_LADDER[@]}"; do
        [ "$c" -le "$MAX_CTX" ] || break
        [ "$nat" -gt 0 ] && [ "$c" -gt "$nat" ] && break
        log "  ctx $c ..."
        gen_probe "$tag" "$c"
        if [ "$G_OK" != "1" ]; then
            log "    failed: $G_ERR"
            [ "$c" -eq 8192 ] && note "8K context failed: $G_ERR"
            break
        fi
        max_ok="$c"; vrammax="$(vram_used_mib)"; gpumax="$(gpu_share "$tag")"; tpsmax="$G_TPS"
        if [ "$c" -eq 8192 ]; then vram8="$vrammax"; gpu8="$gpumax"; tps8="$G_TPS"; fi
        log "    ok: ${G_TPS} tok/s, VRAM ${vrammax:-?} MiB, ${gpumax:-?}% on GPU"
    done

    if [ "$max_ok" -eq 0 ]; then
        emit_row "$model" "$tag" "$nat" "$max_ok" "$test_ctx" "$vram8" "$gpu8" "$tps8" "$vrammax" "$gpumax" "$tpsmax" \
            FAIL SKIP SKIP SKIP SKIP SKIP "0/$LOOPS" SKIP SKIP "$NOTES"
        return
    fi
    unload_all

    # c. Switch the stack to this model the way the Settings page does.
    test_ctx="$max_ok"; [ "$test_ctx" -gt "$TEST_CTX_CAP" ] && test_ctx="$TEST_CTX_CAP"
    local compact=$(( test_ctx * 3 / 4 ))
    log "  switching stack: model $tag, OLLAMA_NUM_CTX $test_ctx, CONTEXT_COMPACT_LIMIT $compact"
    load=PASS
    # From here a Ctrl-C can leave a recreate in flight, so restore runs the API path.
    STACK_TOUCHED=1
    local errs
    if ! curl -sf -o "$MODEL_RAW/put-env.json" --max-time 300 -X PUT "$HOME_API/api/env" \
            -H 'Content-Type: application/json' \
            -d "$(jenv OLLAMA_NUM_CTX "$test_ctx" CONTEXT_COMPACT_LIMIT "$compact")"; then
        load=FAIL; note "PUT /api/env failed"
    elif errs="$(recreate_errors "$MODEL_RAW/put-env.json")"; then
        load=FAIL; note "recreate errors: $errs"
    fi
    wait_for "Ollama" 120 check_ollama || { load=FAIL; note "Ollama not healthy after recreate"; }
    wait_for "SSE proxy" 120 check_proxy || { load=FAIL; note "SSE proxy not healthy after recreate"; }
    if [ "$load" = PASS ] && ! curl -sf -o "$MODEL_RAW/select.json" --max-time 300 -X POST "$HOME_API/api/models/select" \
            -H 'Content-Type: application/json' -d "$(jobj key "$tag")"; then
        load=FAIL; note "POST /api/models/select failed: $(cat "$MODEL_RAW/select.json" 2>/dev/null | head -c 200)"
    fi
    wait_for "LiteLLM" 120 check_litellm || { load=FAIL; note "LiteLLM not healthy after restart"; }
    wait_for "Open WebUI" 60 check_webui || { load=FAIL; note "Open WebUI not healthy"; }

    if [ "$load" != PASS ]; then
        emit_row "$model" "$tag" "$nat" "$max_ok" "$test_ctx" "$vram8" "$gpu8" "$tps8" "$vrammax" "$gpumax" "$tpsmax" \
            "$load" SKIP SKIP SKIP SKIP SKIP "0/$LOOPS" SKIP SKIP "$NOTES"
        return
    fi

    # d. Simple chat reply in Open WebUI. The API key is read from .env and
    # handed to curl on stdin, so it never appears on a command line or in logs.
    log "  Open WebUI chat"
    local key; key="$(env_get KRULL_WEBUI_API_KEY)"
    local chat_body; chat_body="$(python3 -I -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "stream": False, "messages": [{"role": "user", "content": "In one short sentence: what is 2 + 2?"}]}))' "$tag")"
    { [ -n "$key" ] && printf 'Authorization: Bearer %s\n' "$key"; } | \
        docker exec -i krull-webui curl -s --max-time "$T_CHAT" -X POST http://localhost:8080/api/chat/completions \
            -H @- -H 'Content-Type: application/json' -d "$chat_body" > "$MODEL_RAW/webui-chat.json" 2>&1
    unset key
    if python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
t = d["choices"][0]["message"]["content"] or ""
sys.exit(0 if ("4" in t or "four" in t.lower()) else 1)
' "$MODEL_RAW/webui-chat.json" 2>/dev/null; then webui=PASS; else webui=FAIL; note "webui chat: $(head -c 160 "$MODEL_RAW/webui-chat.json")"; fi

    # e. krull-claude through LiteLLM -> SSE proxy -> Ollama.
    local d="$WORK_DIR/$(basename "$MODEL_RAW")"
    mkdir -p "$d/basic"
    run_claude_task basic "$d/basic" "Reply with exactly the word READY and nothing else." "$T_SIMPLE" 5
    if task_ok && grep -qi 'READY' <<<"$T_RESULT"; then basic=PASS; else basic=FAIL; note "basic: $(task_why)"; fi

    local word; word="$(rand_word)"
    mkdir -p "$d/read/notes"
    printf 'Field notes, week 3.\nWeather held. Supplies are adequate.\nThe rendezvous codeword is %s.\nNext check-in is Thursday.\n' "$word" > "$d/read/notes/briefing.txt"
    run_claude_task read "$d/read" "Use the Read tool to read notes/briefing.txt, then give a one-sentence summary that includes the exact rendezvous codeword." "$T_SIMPLE" 10
    if task_ok && has_tool Read && grep -qF "$word" <<<"$T_RESULT"; then read=PASS
    else read=FAIL; note "read: $(task_why); tools=[$P_TOOLS]; codeword reported: $(grep -qF "$word" <<<"$T_RESULT" && echo yes || echo no)"; fi

    mkdir -p "$d/edit"
    printf '# service settings\nname = krull-demo\nretries = 3\ntimeout = 30\n' > "$d/edit/settings.conf"
    printf '# service settings\nname = krull-demo\nretries = 5\ntimeout = 30\n' > "$d/edit.expected"
    run_claude_task edit "$d/edit" "In settings.conf, change the retries value from 3 to 5 using the Edit tool. Change nothing else." "$T_SIMPLE" 10
    local edit_ok=no
    regular_file "$d/edit/settings.conf" && timeout 10 cmp -s "$d/edit/settings.conf" "$d/edit.expected" && edit_ok=yes
    if task_ok && has_tool Edit && [ "$edit_ok" = yes ]; then edit=PASS
    else edit=FAIL; note "edit: $(task_why); tools=[$P_TOOLS]; file correct: $edit_ok"; fi
    copy_task_file "$d/edit/settings.conf" "$MODEL_RAW/edit.settings.conf"

    local nonce=$((RANDOM % 900 + 100))
    mkdir -p "$d/bash"
    write_runner "$d/bash" "$SANDBOX_RUN" "$(printf 'echo "running checks..."\necho "RESULT: %s checks passed"' "$nonce")"
    run_claude_task bash "$d/bash" "Run the command: bash run_tests.sh — then tell me the number on the RESULT line." "$T_SIMPLE" 8
    if task_ok && has_tool Bash && grep -qF "$nonce" <<<"$T_RESULT"; then bash_box=PASS
    else bash_box=FAIL; note "bash: $(task_why); tools=[$P_TOOLS]"; fi

    local i ld sum
    for i in $(seq 1 "$LOOPS"); do
        ld="$d/loop$i"; make_loop_fixture "$ld"
        sum="$(sha256sum "$ld/run_tests.sh" | cut -d' ' -f1)"
        run_claude_task "loop$i" "$ld" "Run: bash run_tests.sh — a test fails because of a bug in calc.sh. Fix the bug in calc.sh (do not change run_tests.sh), then run bash run_tests.sh again and confirm every test passes." "$T_LOOP" 30
        local why=""
        task_ok || why="$(task_why)"
        [ -z "$why" ] && ! { has_tool Edit || has_tool MultiEdit || has_tool Write; } && why="no edit tool used"
        [ -z "$why" ] && [ "$P_BASH_AFTER_EDIT" -ne 1 ] && why="tests not re-run after the fix"
        [ -z "$why" ] && ! regular_file "$ld/calc.sh" && why="calc.sh is not a regular file (symlink, FIFO or gone)"
        [ -z "$why" ] && [ "$(timeout 10 sha256sum "$ld/run_tests.sh" | cut -d' ' -f1)" != "$sum" ] && why="run_tests.sh was modified"
        # The model edited calc.sh: re-run the tests only inside the sandbox.
        [ -z "$why" ] && ! timeout 30 "$SANDBOX_RUN" "$ld" /bin/bash run_tests.sh > "$MODEL_RAW/loop$i.verify.log" 2>&1 && why="tests still fail"
        [ -z "$why" ] && [ "$T_LOOPS" -gt 0 ] && why="$T_LOOPS loop/stall marker(s) in proxy log"
        [ -z "$why" ] && [ "$T_HARD" -gt 0 ] && why="$T_HARD EMPTY OUTPUT/ERROR line(s) in proxy log"
        [ -z "$why" ] && [ "$P_DENIALS" -ge 3 ] && why="$P_DENIALS denied tool calls (wrong-tool loop)"
        copy_task_file "$ld/calc.sh" "$MODEL_RAW/loop$i.calc.sh" \
            || { [ -z "$why" ] && why="calc.sh could not be copied as a regular file"; }
        if [ -z "$why" ]; then loop_n=$((loop_n + 1)); else note "loop$i: $why"; fi
    done
    if [ "$loop_n" -eq "$LOOPS" ]; then loop_box=PASS; else loop_box=FAIL; fi

    # Near-limit: enough part files that the conversation crosses the proxy's
    # compaction threshold (0.75 x CONTEXT_COMPACT_LIMIT tokens at ~4 chars a
    # token). Each part stays under the proxy's 15000-char tool-result cap so
    # nothing is truncated away. Sequential Reads trip the proxy's loop_break
    # hint by design, so loop markers are recorded here but not scored.
    local parts=$(( (compact * 3 / 4) * 4 / 12000 + 3 ))
    [ "$parts" -lt 10 ] && parts=10
    local last_key; last_key="$(make_near_fixture "$d/near" "$parts")"
    run_claude_task near "$d/near" "This directory has $parts files, part01.txt to part$(printf '%02d' "$parts").txt. Use the Read tool to read every one of them, one at a time, in order. Each contains exactly one line starting with KEY:. After reading all of them, reply with only the KEY value from the last file, part$(printf '%02d' "$parts").txt." "$T_NEAR" $(( parts * 2 + 10 ))
    local why=""
    task_ok || why="$(task_why)"
    [ -z "$why" ] && ! grep -qF "$last_key" <<<"$T_RESULT" && why="last KEY not reported"
    [ -z "$why" ] && [ "$T_COMPACT_LOG" -eq 0 ] && [ "$P_COMPACT" -eq 0 ] && why="auto-compact never triggered"
    [ -z "$why" ] && [ "$T_HARD" -gt 0 ] && why="$T_HARD EMPTY OUTPUT/ERROR line(s) in proxy log"
    if [ -z "$why" ]; then near=PASS; else near=FAIL; note "near-limit ($parts parts): $why"; fi
    [ "$T_LOOPS" -gt 0 ] && log "      near-limit loop markers (not scored): $T_LOOPS"

    "$KRULL" logs errors 50 > "$MODEL_RAW/errors.log" 2>&1
    unload_all

    emit_row "$model" "$tag" "$nat" "$max_ok" "$test_ctx" "$vram8" "$gpu8" "$tps8" "$vrammax" "$gpumax" "$tpsmax" \
        "$load" "$webui" "$basic" "$read" "$edit" "$bash_box" "$loop_n/$LOOPS" "$loop_box" "$near" "$NOTES"
}

# ── Results + decision rule ──────────────────────────────────────────────────

write_results() {
    local rc="$1"
    [ -n "$RESULTS_TSV" ] && [ -f "$RESULTS_TSV" ] || return 0
    local restore_txt; restore_txt="$(printf '%s\n' "${RESTORE_NOTES[@]:-}")"
    python3 -I - "$RESULTS_TSV" "$RUN_DIR/meta.txt" "$RUN_DIR/results.md" "$FALLBACK_MODEL" "$rc" "$restore_txt" "$LOOP_RE" <<'PY'
import sys
tsv, meta_path, out_path, fallback, rc, restore, loop_re = sys.argv[1:8]
loop_markers = ", ".join(m.replace("\\+", "+") for m in loop_re.split("|"))
meta = dict(l.rstrip("\n").split("=", 1) for l in open(meta_path) if "=" in l)
# Plain tab split: emit_row already strips tabs/newlines out of every field.
lines = open(tsv).read().splitlines()
hdr = lines[0].split("\t")
rows = [dict(zip(hdr, l.split("\t"))) for l in lines[1:] if l]
BOXES = ["load", "webui", "basic", "read", "edit", "bash", "loop", "near"]
for r in rows:
    failed = [b for b in BOXES if r[b] != "PASS"]
    r["verdict"] = "ALL PASS" if not failed else "DISQUALIFIED (" + ", ".join(failed) + ")"
passing = [r for r in rows if r["verdict"] == "ALL PASS"]
qwen_pass = [r for r in passing if "qwen" in r["model"].lower()]

def size_key(r):
    try:
        return float(r.get("size_gb") or 0)
    except ValueError:
        return 0.0

if not qwen_pass:
    winner = fallback
    why = "No Qwen run passed every box, so the rule says stay on %s." % fallback
    fb = next((r for r in rows if r["model"] == fallback), None)
    if fb is None:
        why += " (%s was not part of this run.)" % fallback
    elif fb["verdict"] != "ALL PASS":
        why += " WARNING: %s itself did not pass every box this run: %s." % (fallback, fb["verdict"])
else:
    best = sorted(passing, key=lambda r: (size_key(r), float(r["tps_8k"] or 0)), reverse=True)[0]
    winner = best["model"]
    why = ("Passed every box. Among the %d model(s) that passed, quality is ranked by model size "
           "(a proxy: read the loop transcripts in raw/ before switching), then tokens/sec."
           % len(passing))

L = []
L.append("# Krull model bake-off — %s" % meta.get("started", ""))
L.append("")
L.append("- Host GPU: %s" % meta.get("gpu", "?"))
L.append("- System RAM: %s GiB" % meta.get("ram", "?"))
L.append("- External DNS at test start: **%s** (the checklist asks for the network to be disconnected)" % meta.get("dns", "?"))
L.append("- Settings before the run: OLLAMA_MODEL=`%s`, OLLAMA_NUM_CTX=`%s`, CONTEXT_COMPACT_LIMIT=`%s`"
         % (meta.get("orig_model", ""), meta.get("orig_ctx", ""), meta.get("orig_compact", "")))
L.append("- Exit status: %s%s" % (rc, " (interrupted or failed before the end)" if rc != "0" else ""))
L.append("")
L.append("## Results")
L.append("")
L.append("| Model | Max ctx loaded | Test ctx | VRAM @8K (MiB) | GPU % @8K | tok/s @8K | VRAM @max (MiB) | GPU % @max | tok/s @max | Load | WebUI chat | Basic | Read | Edit | Bash | Full loop | Near-limit compact | Verdict |")
L.append("|" + "---|" * 18)
for r in rows:
    L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
        r["model"] + ("" if r["tag"] in ("-", r["model"]) else " (`%s`)" % r["tag"]),
        r["max_ctx"], r["test_ctx"], r["vram_8k"] or "-", r["gpu_8k"] or "-", r["tps_8k"] or "-",
        r["vram_max"] or "-", r["gpu_max"] or "-", r["tps_max"] or "-",
        r["load"], r["webui"], r["basic"], r["read"], r["edit"], r["bash"],
        "%s (%s)" % (r["loop"], r["loop_n"]), r["near"], r["verdict"]))
L.append("")
L.append("## Decision")
L.append("")
L.append("**Primary offline model: `%s`.** %s" % (winner, why))
L.append("")
L.append("Keep `%s` installed as the known-good fallback whatever wins." % fallback)
L.append("")
L.append("Passing settings for the winner: OLLAMA_MODEL=`%s`, OLLAMA_NUM_CTX=`%s`, CONTEXT_COMPACT_LIMIT=`%s`."
         % (winner, next((r["test_ctx"] for r in rows if r["model"] == winner), "?"),
            next((str(int(r["test_ctx"]) * 3 // 4) for r in rows if r["model"] == winner and r["test_ctx"].isdigit()), "?")))
L.append("")
L.append("## Notes per model")
L.append("")
for r in rows:
    L.append("- **%s**: %s" % (r["model"], r["notes"] or "none"))
L.append("")
L.append("## How it was measured")
L.append("")
L.append("- Max ctx: ascending num_ctx ladder against Ollama /api/generate, stopping at the first failure; capped at --max-ctx and the model's native context.")
L.append("- tok/s = eval_count / eval_duration for a 256-token reply with a short prompt. The @max figure reflects layer offload caused by the larger KV cache, not attention over a full window.")
L.append("- VRAM: nvidia-smi memory.used with only the candidate loaded; GPU % = Ollama /api/ps size_vram / size (below 100 means partial CPU offload).")
L.append("- Tool tests ran through krull-claude -p (LiteLLM -> SSE proxy -> Ollama) in a throwaway project, at the test ctx. Full loop passes only if all runs pass: edit tool used, tests re-run after the fix, run_tests.sh untouched, tests pass when re-run by the script, and no loop/stall markers (%s) or EMPTY OUTPUT/ERROR lines in `./krull logs session`." % loop_markers)
L.append("- Near-limit: sequential Reads of part files sized to cross the proxy compaction threshold; passes if compaction is logged (proxy or Claude Code), the last KEY is reported, and no EMPTY OUTPUT/ERROR lines appear.")
L.append("- Not scripted: 'stack starts' is checked as health after each recreate, not a cold `./krull start`. Disconnecting the network is up to you; DNS state is recorded above.")
L.append("")
L.append("## Restore")
L.append("")
for n in restore.splitlines():
    if n.strip():
        L.append("- " + n)
L.append("")
L.append("Raw logs: `raw/<model>/` (stream-json transcripts, proxy session logs, ladder replies).")
open(out_path, "w").write("\n".join(L) + "\n")
print("")
print("Results: " + out_path)
print("Winner: %s — %s" % (winner, why))
print("Fallback (keep installed): %s" % fallback)
PY
}

# ── Main ─────────────────────────────────────────────────────────────────────

echo "Krull model bake-off"
echo "Models: ${MODELS[*]}"
[ "$DRY_RUN" -eq 1 ] && echo "Mode: DRY RUN (nothing will be changed)"
echo ""

preflight

if [ "$DRY_RUN" -eq 1 ]; then
    print_plan
    if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
        echo "Dry run: $PREFLIGHT_FAIL prerequisite(s) failed; a real run would stop here."
        exit 1
    fi
    echo "Dry run: all prerequisites met."
    exit 0
fi

if [ "$PREFLIGHT_FAIL" -gt 0 ]; then
    echo "ERROR: $PREFLIGHT_FAIL prerequisite(s) failed. Nothing was changed."
    exit 1
fi

# One bake-off at a time. Self-ignoring results dir: ./krull update refuses to
# run on a dirty tree, and data/bakeoff/ is not in the repo's .gitignore. The
# lock fd is read-only, and sandbox-run closes it.
mkdir -p "$PROJECT_DIR/data/bakeoff" || { echo "ERROR: cannot create data/bakeoff"; exit 1; }
[ -f "$PROJECT_DIR/data/bakeoff/.gitignore" ] || printf '*\n' > "$PROJECT_DIR/data/bakeoff/.gitignore"
: >> "$LOCK_FILE" && exec 9< "$LOCK_FILE" || { echo "ERROR: cannot open $LOCK_FILE"; exit 1; }
flock -n 9 || { echo "ERROR: another bake-off is already running (lock: data/bakeoff/.lock). Nothing was changed."; exit 1; }

# Pull phase (needs network). pull-model does not touch .env or litellm.
if [ "$SKIP_PULL" -eq 0 ] && [ "${#NEED_PULL[@]}" -gt 0 ]; then
    for m in "${NEED_PULL[@]}"; do
        echo "Pulling $m ..."
        "$KRULL" pull-model "$m" || echo "WARNING: pull of $m failed; it will be skipped."
    done
fi
if [ "$PULL_ONLY" -eq 1 ]; then
    echo "Pull-only run finished. Unplug the network, then: ./krull bakeoff --skip-pull"
    exit 0
fi

# From here on the stack is changed, so record originals and arm the restore.
mkdir -p "$RUN_DIR/raw" || { echo "ERROR: cannot create $RUN_DIR"; exit 1; }
HAS_MODEL=0; env_has OLLAMA_MODEL && HAS_MODEL=1 && ORIG_MODEL="$(env_get OLLAMA_MODEL)"
HAS_CTX=0; env_has OLLAMA_NUM_CTX && HAS_CTX=1 && ORIG_CTX="$(env_get OLLAMA_NUM_CTX)"
HAS_COMPACT=0; env_has CONTEXT_COMPACT_LIMIT && HAS_COMPACT=1 && ORIG_COMPACT="$(env_get CONTEXT_COMPACT_LIMIT)"
printf 'OLLAMA_MODEL=%s\nOLLAMA_NUM_CTX=%s\nCONTEXT_COMPACT_LIMIT=%s\n' \
    "$([ $HAS_MODEL -eq 1 ] && echo "$ORIG_MODEL" || echo '<absent>')" \
    "$([ $HAS_CTX -eq 1 ] && echo "$ORIG_CTX" || echo '<absent>')" \
    "$([ $HAS_COMPACT -eq 1 ] && echo "$ORIG_COMPACT" || echo '<absent>')" > "$RUN_DIR/env.before"
CONFIG_BACKUP="$RUN_DIR/litellm-config.yaml.before"
cp -p "$LITELLM_CONFIG" "$CONFIG_BACKUP" || { echo "ERROR: cannot back up litellm/config.yaml. Nothing was changed."; exit 1; }
CONFIG_SHA="$(sha256sum "$LITELLM_CONFIG" | cut -d' ' -f1)"
mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}"
WORK_DIR="$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/krull-bakeoff.XXXXXX")" \
    || { echo "ERROR: cannot create a temp dir. Nothing was changed."; exit 1; }
PRIVATE_DIR="$WORK_DIR/.private"
SANDBOX_RUN="$WORK_DIR/sandbox-run"
# .env holds keys: the snapshot goes in the 0700 private dir, never the run dir.
read -r ENV_MODE ENV_UID ENV_GID < <(stat -c '%a %u %g' "$ENV_FILE")
ENV_SNAPSHOT="$PRIVATE_DIR/env.orig"
if ! { mkdir -m 700 "$PRIVATE_DIR" && ( umask 077 && cp "$ENV_FILE" "$ENV_SNAPSHOT" ) && chmod 600 "$ENV_SNAPSHOT"; }; then
    rm -rf -- "${WORK_DIR:?}"; echo "ERROR: cannot snapshot .env. Nothing was changed."; exit 1
fi
if [ -f "$SESSION_FILE" ]; then
    SESSION_BACKUP="$PRIVATE_DIR/krull-session"; cp -p "$SESSION_FILE" "$SESSION_BACKUP"
fi
ORIG_ENV_OLLAMA="$(container_env krull-ollama)"
ORIG_ENV_PROXY="$(container_env krull-sse-proxy)"
# The runner manifest sits beside the task dirs, never in one: the sandbox
# and the model's Edit can reach only their own task dir.
RUNNER_MANIFEST="$WORK_DIR/runners.sha256"
{ write_sandbox_run "$SANDBOX_RUN" && write_bash_gate "$WORK_DIR/bash-gate" && : > "$RUNNER_MANIFEST"; } \
    || { rm -rf -- "${WORK_DIR:?}"; echo "ERROR: cannot write sandbox-run / bash-gate. Nothing was changed."; exit 1; }
set_claude_task_flags "$WORK_DIR/bash-gate" "$RUNNER_MANIFEST"
RESULTS_TSV="$RUN_DIR/results.tsv"
printf 'model\ttag\tnative_ctx\tmax_ctx\ttest_ctx\tvram_8k\tgpu_8k\ttps_8k\tvram_max\tgpu_max\ttps_max\tload\twebui\tbasic\tread\tedit\tbash\tloop_n\tloop\tnear\tnotes\n' > "$RESULTS_TSV"
RUN_STARTED=1
trap 'finish "$?"' EXIT
trap 'log "Interrupted."; exit 130' INT TERM HUP

echo ""
echo "================================================================"
echo " Checklist: run with the network DISCONNECTED. This script never"
echo " touches network settings — unplug or disable it yourself now."
echo "================================================================"
if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    read -r -p "Press Enter when the network is disconnected (Ctrl-C aborts and restores)... " _
fi
DNS="$(dns_state)"
[ "$DNS" = "ONLINE" ] && log "WARNING: external DNS still resolves — results are recorded as an ONLINE run."
{
    echo "started=$TS"
    echo "gpu=$(timeout 10 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)"
    echo "ram=$(awk '/^MemTotal:/{printf "%.1f", $2/1048576}' /proc/meminfo)"
    echo "dns=$DNS"
    echo "orig_model=$ORIG_MODEL"
    echo "orig_ctx=$ORIG_CTX"
    echo "orig_compact=$ORIG_COMPACT"
} > "$RUN_DIR/meta.txt"
log "Run dir: $RUN_DIR"

for m in "${MODELS[@]}"; do
    run_model "$m"
done

# Model sizes feed the quality tiebreak in the decision rule.
python3 -I - "$RESULTS_TSV" "$(ollama_get /api/tags)" <<'PY'
import json, sys
path, tags = sys.argv[1], sys.argv[2]
try:
    sizes = {m["name"]: m.get("size", 0) / 1e9 for m in json.loads(tags).get("models") or []}
except Exception:
    sizes = {}
lines = open(path).read().splitlines()
hdr = lines[0].split("\t")
out = ["\t".join(hdr + ["size_gb"])]
for l in lines[1:]:
    if l:
        tag = dict(zip(hdr, l.split("\t"))).get("tag", "")
        out.append(l + "\t%.1f" % sizes.get(tag, 0))
open(path, "w").write("\n".join(out) + "\n")
PY

exit 0
