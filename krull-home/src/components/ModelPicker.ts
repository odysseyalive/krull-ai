import {
  fetchModels,
  pullModel,
  selectModel,
  deleteModel,
  streamJob,
  formatBytes,
  type ModelsPayload,
  type RecommendedModel,
} from "../lib/api";
import { toast } from "./Toast";

/**
 * Renders the recommended-model picker. Three cards (4B / 9B / 27B).
 * Click an installed card → set as active.
 * Click an uninstalled card → pull, then set as active.
 */
export function ModelPicker(): HTMLElement {
  const wrap = document.createElement("section");
  wrap.className = "model-picker";

  const head = document.createElement("div");
  head.className = "model-picker__head";
  const eyebrow = document.createElement("p");
  eyebrow.className = "section__eyebrow";
  eyebrow.textContent = "Recommended models";
  const title = document.createElement("h2");
  title.className = "section__title";
  title.textContent = "Pick a brain";
  const sub = document.createElement("p");
  sub.className = "model-picker__sub";
  sub.textContent =
    "Qwen 3.5 in four flavors: three dense Instruct tiers (4B/9B/27B) and a Mixture-of-Experts hybrid (36B-A3B) for big-context reasoning. Pick the one that fits your GPU and Krull will pull it and wire it into the LiteLLM gateway.";
  head.append(eyebrow, title, sub);
  wrap.append(head);

  const grid = document.createElement("div");
  grid.className = "model-grid";
  wrap.append(grid);

  // Initial loading state
  const loading = document.createElement("p");
  loading.className = "form-status";
  loading.textContent = "Loading recommended models…";
  grid.append(loading);

  void refresh();

  async function refresh() {
    try {
      const data = await fetchModels();
      renderGrid(data);
    } catch (err) {
      grid.replaceChildren();
      const msg = document.createElement("p");
      msg.className = "form-status";
      msg.textContent = `Failed to load models: ${(err as Error).message}`;
      grid.append(msg);
    }
  }

  function renderGrid(data: ModelsPayload) {
    grid.replaceChildren();
    for (const model of data.recommended) {
      grid.append(renderCard(model));
    }
  }

  function renderCard(model: RecommendedModel): HTMLElement {
    const card = document.createElement("article");
    const stateClass = model.active
      ? "model-card--active"
      : model.installed
        ? "model-card--installed"
        : "model-card--available";
    card.className = `model-card ${stateClass}`;
    card.dataset.key = model.key;

    const top = document.createElement("div");
    top.className = "model-card__top";

    const labelRow = document.createElement("div");
    labelRow.className = "model-card__label-row";
    const label = document.createElement("h3");
    label.className = "model-card__label";
    label.textContent = model.label;
    const vram = document.createElement("span");
    vram.className = "model-card__vram";
    vram.textContent = model.vram;
    labelRow.append(label, vram);

    const desc = document.createElement("p");
    desc.className = "model-card__desc";
    desc.textContent = model.description;

    const bestFor = document.createElement("p");
    bestFor.className = "model-card__best-for";
    bestFor.textContent = model.bestFor;

    top.append(labelRow, desc, bestFor);

    const status = document.createElement("div");
    status.className = "model-card__status";
    if (model.active) {
      const badge = document.createElement("span");
      badge.className = "model-card__badge model-card__badge--active";
      badge.textContent = "Active";
      status.append(badge);
    } else if (model.installed) {
      const badge = document.createElement("span");
      badge.className = "model-card__badge";
      badge.textContent = "Installed";
      status.append(badge);
    }

    // Progress strip — appears during a pull. Indeterminate, because
    // ollama pull's progress format is opaque enough that an honest
    // "something is happening" stripe is better than a fake percentage.
    const progress = document.createElement("div");
    progress.className = "krull-progress model-card__progress";
    const fill = document.createElement("div");
    fill.className = "krull-progress__fill";
    const pLabel = document.createElement("div");
    pLabel.className = "krull-progress__label";
    progress.append(fill, pLabel);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "btn btn--sm";
    if (model.active) {
      action.className += " btn--ghost";
      action.textContent = "Active";
      action.disabled = true;
    } else if (model.installed) {
      action.className += " btn--primary";
      action.textContent = "Set as active";
      action.addEventListener("click", () => handleSelect(model, action));
    } else {
      action.className += " btn--primary";
      action.textContent = "Pull & activate";
      action.addEventListener("click", () => handlePull(model, action));
    }

    const bottom = document.createElement("div");
    bottom.className = "model-card__bottom";
    bottom.append(status, action);

    // Removal: only offered for installed-but-not-active cards. The
    // active model is protected — switch to a different brain first.
    // Two-click confirmation: first click arms, second click commits.
    // Avoids a modal but also avoids deleting 24 GB on a stray click.
    if (model.installed && !model.active) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn--ghost btn--sm model-card__remove";
      remove.textContent = "Remove";
      remove.title = `Delete ${model.label} from local Ollama storage.`;
      let armed = false;
      let armTimer: number | undefined;
      remove.addEventListener("click", () => {
        if (!armed) {
          armed = true;
          remove.textContent = "Click again to confirm";
          remove.classList.add("model-card__remove--armed");
          armTimer = window.setTimeout(() => {
            armed = false;
            remove.textContent = "Remove";
            remove.classList.remove("model-card__remove--armed");
          }, 4000);
          return;
        }
        if (armTimer) window.clearTimeout(armTimer);
        void handleDelete(model, remove);
      });
      bottom.append(remove);
    }

    card.append(top, progress, bottom);
    return card;
  }

  async function handleDelete(model: RecommendedModel, button: HTMLButtonElement) {
    button.disabled = true;
    button.textContent = "Removing…";
    try {
      await deleteModel(model.key);
      toast(`${model.label} removed.`, "success");
      await refresh();
    } catch (err) {
      toast(`Remove failed: ${(err as Error).message}`, "error", 6000);
      button.disabled = false;
      button.textContent = "Remove";
      button.classList.remove("model-card__remove--armed");
    }
  }

  async function handleSelect(model: RecommendedModel, button: HTMLButtonElement) {
    button.disabled = true;
    button.textContent = "Activating…";
    try {
      await selectModel(model.key);
      // Notify the surrounding page so any OLLAMA_MODEL form input
      // can stay in sync. Without this, saving the env form afterwards
      // would silently overwrite the picker's change.
      window.dispatchEvent(
        new CustomEvent("krull:model-changed", { detail: { key: model.key } }),
      );
      toast(`${model.label} is now active. Restarting LiteLLM…`, "success");
      await refresh();
    } catch (err) {
      toast(`Activate failed: ${(err as Error).message}`, "error", 6000);
      button.disabled = false;
      button.textContent = "Set as active";
    }
  }

  async function handlePull(model: RecommendedModel, button: HTMLButtonElement) {
    const card = button.closest(".model-card") as HTMLElement | null;
    const pLabel = card?.querySelector(".krull-progress__label") as HTMLElement | null;
    const pFill = card?.querySelector(".krull-progress__fill") as HTMLElement | null;
    card?.classList.add("model-card--pulling", "model-card--pulling-determinate");
    if (pLabel) pLabel.textContent = "Starting…";
    if (pFill) pFill.style.width = "0%";
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      const { jobId } = await pullModel(model.key);
      const stop = streamJob(jobId, async (ev) => {
        if (ev.phase === "downloading") {
          // Real per-byte progress when ollama gives us total + completed,
          // otherwise fall back to whatever status string the daemon sent
          // (e.g. "pulling manifest", "verifying sha256 digest", "writing
          // manifest", "Applying tuned parameters…").
          if (typeof ev.percent === "number" && typeof ev.total === "number" && ev.total > 0) {
            if (pFill) pFill.style.width = `${ev.percent}%`;
            const completed = ev.bytes ?? 0;
            if (pLabel) pLabel.textContent = `${ev.percent}%  ·  ${formatBytes(completed)} / ${formatBytes(ev.total)}`;
            button.textContent = `Pulling ${ev.percent}%`;
          } else if (ev.message) {
            if (pLabel) pLabel.textContent = ev.message;
            button.textContent = "Pulling…";
          }
        } else if (ev.phase === "done") {
          stop();
          card?.classList.remove("model-card--pulling-determinate");
          if (pFill) pFill.style.width = "100%";
          button.textContent = "Activating…";
          try {
            await selectModel(model.key);
            window.dispatchEvent(
              new CustomEvent("krull:model-changed", { detail: { key: model.key } }),
            );
            toast(`${model.label} pulled and activated.`, "success");
          } catch (err) {
            toast(`Activate failed: ${(err as Error).message}`, "error", 6000);
          }
          await refresh();
        } else if (ev.phase === "failed") {
          stop();
          card?.classList.remove("model-card--pulling-determinate");
          toast(`Pull failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          button.disabled = false;
          button.textContent = "Pull & activate";
        }
      });
    } catch (err) {
      card?.classList.remove("model-card--pulling-determinate");
      toast((err as Error).message, "error", 6000);
      button.disabled = false;
      button.textContent = "Pull & activate";
    }
  }

  return wrap;
}
