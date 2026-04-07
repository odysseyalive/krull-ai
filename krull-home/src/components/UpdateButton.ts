/**
 * Update button + modal that lives in the persistent nav.
 *
 * Click → confirmation modal showing the current commit → Update Krull
 * → POST /api/update → frontend polls /api/version while the rebuild
 * runs. Mid-update krull-home itself is recreated, so the API drops
 * out from under us; we tolerate that and just keep polling. When
 * /api/version returns a new commit hash, we reload the page on the
 * fresh build.
 */
import {
  fetchVersion,
  fetchUpdateStatus,
  triggerUpdate,
  type VersionInfo,
  type UpdateStatus,
} from "../lib/api";
import { toast } from "./Toast";

export function UpdateButton(): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "krull-update";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "krull-update__btn";
  btn.title = "Update Krull from GitHub";
  btn.setAttribute("aria-label", "Update Krull from GitHub");

  const icon = document.createElement("span");
  icon.className = "krull-update__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⟳";

  const label = document.createElement("span");
  label.className = "krull-update__label";
  label.textContent = "Update";

  btn.append(icon, label);
  wrap.append(btn);

  let currentVersion: VersionInfo | null = null;
  void loadVersion();

  async function loadVersion() {
    try {
      currentVersion = await fetchVersion();
    } catch {
      /* leave as null — modal will show "unknown" */
    }
  }

  btn.addEventListener("click", () => openModal());

  function openModal() {
    const overlay = document.createElement("div");
    overlay.className = "krull-update-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    const card = document.createElement("div");
    card.className = "krull-update-modal";

    const heading = document.createElement("h2");
    heading.className = "krull-update-modal__title";
    heading.textContent = "Update Krull";

    const body = document.createElement("div");
    body.className = "krull-update-modal__body";

    const blurb = document.createElement("p");
    blurb.textContent =
      "This pulls the latest code from GitHub, rebuilds every service, and re-runs setup. Your data, models, maps, and downloaded knowledge are not touched.";

    const versionRow = document.createElement("div");
    versionRow.className = "krull-update-modal__version";
    if (currentVersion) {
      const branch = currentVersion.branch ?? "(detached)";
      const commit = document.createElement("code");
      commit.textContent = `${branch} @ ${currentVersion.shortCommit}`;
      const versionLabel = document.createElement("span");
      versionLabel.textContent = "Current version: ";
      versionRow.append(versionLabel, commit);
    } else {
      versionRow.textContent = "Current version: (unable to read)";
    }

    body.append(blurb, versionRow);

    const phaseLine = document.createElement("p");
    phaseLine.className = "krull-update-modal__phase";

    const actions = document.createElement("div");
    actions.className = "krull-update-modal__actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn--ghost";
    cancelBtn.textContent = "Cancel";

    const goBtn = document.createElement("button");
    goBtn.type = "button";
    goBtn.className = "btn btn--primary";
    goBtn.textContent = "Update Krull";

    actions.append(cancelBtn, goBtn);
    card.append(heading, body, phaseLine, actions);
    overlay.append(card);
    document.body.append(overlay);

    let stopPolling: (() => void) | null = null;

    const close = () => {
      stopPolling?.();
      overlay.remove();
    };
    cancelBtn.addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });

    goBtn.addEventListener("click", async () => {
      goBtn.disabled = true;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Close";
      cancelBtn.disabled = false;
      cancelBtn.style.display = "none"; // hide during the update
      goBtn.textContent = "Updating…";
      phaseLine.textContent = "Requesting update…";

      try {
        await triggerUpdate();
      } catch (err) {
        phaseLine.textContent = `Failed to start update: ${(err as Error).message}`;
        goBtn.disabled = false;
        goBtn.textContent = "Update Krull";
        cancelBtn.style.display = "";
        return;
      }

      stopPolling = startPolling(currentVersion?.commit ?? null, (state) => {
        phaseLine.textContent = state.message;
        if (state.kind === "done") {
          goBtn.textContent = "Reloading…";
          toast("Update complete. Reloading…", "success");
          window.setTimeout(() => window.location.reload(), 1200);
        } else if (state.kind === "failed") {
          goBtn.textContent = "Update failed";
          goBtn.disabled = false;
          goBtn.className = "btn btn--ghost";
          cancelBtn.style.display = "";
        }
      });
    });
  }

  return wrap;
}

type UiState =
  | { kind: "running"; message: string }
  | { kind: "done"; message: string }
  | { kind: "failed"; message: string };

/**
 * Poll /api/version and /api/update/status while an update is in flight.
 * The API will become unreachable mid-update (krull-home is rebuilding
 * itself); we treat fetch errors as "still rebuilding" and keep going.
 *
 * Termination conditions:
 *   - /api/update/status returns phase="failed"  → done(failed)
 *   - /api/version returns a different commit    → done(success)
 *   - 5 minutes elapse without resolution        → done(failed, timeout)
 */
function startPolling(
  startingCommit: string | null,
  onState: (state: UiState) => void,
): () => void {
  const startedAt = Date.now();
  const TIMEOUT_MS = 5 * 60 * 1000;
  let cancelled = false;

  let lastPhaseMessage = "Pulling latest from GitHub…";
  onState({ kind: "running", message: lastPhaseMessage });

  const tick = async () => {
    if (cancelled) return;

    if (Date.now() - startedAt > TIMEOUT_MS) {
      onState({ kind: "failed", message: "Update timed out after 5 minutes." });
      return;
    }

    // Check status (may fail during krull-home rebuild — that's fine)
    let status: UpdateStatus | null = null;
    try {
      status = await fetchUpdateStatus();
    } catch {
      lastPhaseMessage = "Krull is restarting…";
    }

    if (status) {
      if (status.phase === "failed") {
        onState({
          kind: "failed",
          message: status.message ?? "Update failed",
        });
        return;
      }
      if (status.phase === "running" && status.message) {
        lastPhaseMessage = status.message + "…";
      }
      if (status.phase === "complete") {
        // Don't declare done yet — wait for the new commit hash to land
        // (krull-home may still be rebuilding to pick up the new code).
        lastPhaseMessage = "Finishing up…";
      }
    }

    // Check version — if it differs from what we started with, we're done.
    try {
      const v = await fetchVersion();
      if (startingCommit && v.commit !== startingCommit) {
        onState({
          kind: "done",
          message: `Updated to ${v.shortCommit}`,
        });
        return;
      }
      // Same commit — if status says complete, it means there was nothing
      // new to pull. Honour that as a "done with no changes" outcome.
      if (status?.phase === "complete") {
        onState({
          kind: "done",
          message: status.message ?? "Already up to date",
        });
        return;
      }
    } catch {
      lastPhaseMessage = "Krull is restarting…";
    }

    onState({ kind: "running", message: lastPhaseMessage });
    window.setTimeout(tick, 2000);
  };

  void tick();
  return () => {
    cancelled = true;
  };
}
