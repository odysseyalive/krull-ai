#!/bin/bash
# Krull updater loop. Watches /workspace/data/.update-requested for a
# trigger file. When found, fetches the latest main from GitHub over
# HTTPS, fast-forwards the working tree, rebuilds every service except
# itself, and re-runs setup.sh.
#
# State files written under /workspace/data/:
#   .update-requested  — sentinel written by krull-home, deleted here
#   .update-status     — JSON {phase, timestamp, message} read by krull-home
#   .update-log        — full stdout/stderr of the most recent attempt
#
# This script is the only place that should call `git pull` against the
# repo at runtime. krull-home itself never writes to /workspace/.git.

set -u

REPO=/workspace
DATA="$REPO/data"
SENTINEL="$DATA/.update-requested"
STATUS="$DATA/.update-status"
LOG="$DATA/.update-log"
REMOTE_URL="https://github.com/odysseyalive/krull-ai.git"
BRANCH=main

mkdir -p "$DATA"

write_status() {
    local phase="$1"
    local message="${2:-}"
    # Escape double quotes in message for the JSON literal.
    local escaped="${message//\"/\\\"}"
    printf '{"phase":"%s","timestamp":"%s","message":"%s"}\n' \
        "$phase" "$(date -Iseconds)" "$escaped" > "$STATUS"
}

run_update() {
    : > "$LOG"
    write_status "running" "Starting update"

    {
        echo "[$(date -Iseconds)] Update requested"
        cd "$REPO" || {
            write_status "failed" "Cannot cd to $REPO"
            return 1
        }

        # Sanity-check git can talk to the repo at all. If this fails the
        # most likely cause is the safe.directory whitelist not being set
        # in the container image — see Dockerfile.
        if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            write_status "failed" "Cannot read git repository at /workspace (try rebuilding krull-updater)"
            return 1
        fi

        echo "[$(date -Iseconds)] Fetching $BRANCH from $REMOTE_URL"
        if ! git fetch "$REMOTE_URL" "$BRANCH"; then
            write_status "failed" "git fetch failed (no network or repo unreachable)"
            return 1
        fi

        # "Is there even anything new?" — check this BEFORE the dirty
        # check. If FETCH_HEAD is an ancestor of HEAD (or equal to it),
        # we already have everything the remote has, so there's nothing
        # to apply and the user's local working-tree state doesn't
        # matter at all. This avoids reporting "uncommitted changes"
        # as a failure when in reality nothing needs to update.
        if git merge-base --is-ancestor FETCH_HEAD HEAD; then
            write_status "complete" "Already up to date — no changes to apply"
            echo "[$(date -Iseconds)] Local HEAD already contains FETCH_HEAD. Nothing to do."
            return 0
        fi

        # There ARE new commits to apply. Now the dirty check matters
        # because a fast-forward merge would have to touch the working
        # tree, which we refuse to do over uncommitted edits.
        if ! git diff --quiet || ! git diff --cached --quiet; then
            write_status "failed" "New commits are available, but the local repo has uncommitted changes. Stash or commit them, then try again."
            return 1
        fi

        local before
        before=$(git rev-parse HEAD)

        echo "[$(date -Iseconds)] Fast-forward merging FETCH_HEAD"
        if ! git merge --ff-only FETCH_HEAD; then
            write_status "failed" "git merge --ff-only failed (local branch has diverged from origin/$BRANCH)"
            return 1
        fi

        local after
        after=$(git rev-parse HEAD)
        echo "[$(date -Iseconds)] HEAD: $before -> $after"
        write_status "running" "Rebuilding services"

        # Rebuild every service EXCEPT this one. If the updater rebuilds
        # itself it'll kill the process running this script and the
        # update will hang. Listing services explicitly is safer than
        # excluding by name in case future compose files reorder things.
        local services
        services=$(docker compose config --services | grep -vx 'krull-updater' | tr '\n' ' ')
        if [ -z "$services" ]; then
            write_status "failed" "docker compose returned no services"
            return 1
        fi

        echo "[$(date -Iseconds)] Rebuilding: $services"
        # shellcheck disable=SC2086
        if ! docker compose up -d --build $services; then
            write_status "failed" "docker compose up failed"
            return 1
        fi

        write_status "running" "Running setup"
        echo "[$(date -Iseconds)] Running setup.sh"
        if ! bash "$REPO/scripts/setup.sh"; then
            write_status "failed" "setup.sh failed"
            return 1
        fi

        write_status "complete" "Update complete (HEAD: $after)"
        echo "[$(date -Iseconds)] Done"
        return 0
    } >> "$LOG" 2>&1
}

echo "[krull-updater] watching $SENTINEL"

while true; do
    if [ -f "$SENTINEL" ]; then
        rm -f "$SENTINEL"
        run_update || true
    fi
    sleep 3
done
