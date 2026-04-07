import {
  fetchModels,
  pullModel,
  selectModel,
  streamJob,
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
    "Three sizes of the same Qwen 3.5 Instruct model. Same architecture, same tool calling, three VRAM tiers. Pick the one that fits your GPU and Krull will pull it and wire it into the LiteLLM gateway.";
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

    card.append(top, progress, bottom);
    return card;
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
    card?.classList.add("model-card--pulling");
    if (pLabel) pLabel.textContent = "Pulling…";
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      const { jobId } = await pullModel(model.key);
      const stop = streamJob(jobId, async (ev) => {
        if (ev.phase === "downloading") {
          button.textContent = "Pulling…";
          if (pLabel) pLabel.textContent = "Pulling model from registry…";
        } else if (ev.phase === "done") {
          stop();
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
          toast(`Pull failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          button.disabled = false;
          button.textContent = "Pull & activate";
        }
      });
    } catch (err) {
      toast((err as Error).message, "error", 6000);
      button.disabled = false;
      button.textContent = "Pull & activate";
    }
  }

  return wrap;
}
