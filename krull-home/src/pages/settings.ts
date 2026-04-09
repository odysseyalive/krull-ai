import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { toast } from "../components/Toast";
import { ModelPicker } from "../components/ModelPicker";
import {
  fetchEnv,
  fetchModels,
  restartContainer,
  saveEnv,
  streamJob,
  type ContextSuggestion,
  type EnvField,
  type EnvPayload,
} from "../lib/api";

export async function SettingsPage(): Promise<HTMLElement> {
  const root = document.createElement("div");
  root.className = "page page--settings";

  root.append(Nav("/settings"));
  root.append(
    Header({
      image: "/images/headers/settings.webp",
      eyebrow: "Settings",
      title: "Tune the engines.",
      subtitle:
        "Edit environment variables and restart services without touching a terminal.",
    }),
  );

  // Model picker sits above the form — picking a model is the most
  // common reason someone visits this page.
  const modelSection = document.createElement("section");
  modelSection.className = "section section--models";
  modelSection.append(ModelPicker());
  root.append(modelSection);

  // When the picker activates a new model it dispatches this event so the
  // OLLAMA_MODEL form input below stays in sync, AND so the per-model
  // context-window suggestion blocks under OLLAMA_NUM_CTX and
  // CONTEXT_COMPACT_LIMIT can re-render against the new brain's payload.
  const onModelChanged = (e: Event) => {
    const detail = (e as CustomEvent<{ key: string }>).detail;
    const input = inputs.get("OLLAMA_MODEL");
    if (input && detail?.key) input.value = detail.key;
    void refreshSuggestions();
  };
  window.addEventListener("krull:model-changed", onModelChanged);
  // Best-effort cleanup when the page is replaced.
  window.addEventListener(
    "popstate",
    () => window.removeEventListener("krull:model-changed", onModelChanged),
    { once: true },
  );

  const section = document.createElement("section");
  section.className = "section section--form";
  root.append(section);

  const status = document.createElement("p");
  status.className = "form-status";
  status.textContent = "Loading…";
  section.append(status);

  let payload: EnvPayload;
  try {
    payload = await fetchEnv();
  } catch (err) {
    status.textContent = `Failed to load .env: ${(err as Error).message}`;
    return root;
  }
  status.remove();

  // Group fields by category from the schema, with extras at the end.
  const groups = new Map<string, EnvField[]>();
  for (const field of payload.schema) {
    const list = groups.get(field.group) ?? [];
    list.push(field);
    groups.set(field.group, list);
  }

  const form = document.createElement("form");
  form.className = "env-form";
  form.noValidate = true;
  form.addEventListener("submit", (e) => e.preventDefault());

  const inputs = new Map<string, HTMLInputElement>();
  // Container for the per-field suggestion block — keyed by env var name.
  // refreshSuggestions() reads/writes these slots whenever the active
  // brain changes (initial load + krull:model-changed).
  const suggestionSlots = new Map<string, HTMLDivElement>();

  for (const [groupName, fields] of groups) {
    const grp = document.createElement("fieldset");
    grp.className = "env-group";
    const legend = document.createElement("legend");
    legend.className = "env-group__legend";
    legend.textContent = groupName;
    grp.append(legend);

    for (const field of fields) {
      grp.append(
        renderField(field, payload.values[field.key] ?? "", inputs, suggestionSlots),
      );
    }
    form.append(grp);
  }

  // Active-brain context suggestion: rendered into the slot under
  // OLLAMA_NUM_CTX and CONTEXT_COMPACT_LIMIT. Re-runs on krull:model-changed.
  async function refreshSuggestions(): Promise<void> {
    let activeKey = "";
    let suggestion: ContextSuggestion | undefined;
    let modelLabel: string | undefined;
    try {
      const data = await fetchModels();
      activeKey = data.active;
      const active = data.recommended.find((m) => m.key === activeKey);
      suggestion = active?.contextSuggestion;
      modelLabel = active?.label;
    } catch {
      // Hide all hints rather than showing stale suggestions on a fetch error.
    }
    renderSuggestion("OLLAMA_NUM_CTX", "numCtx", suggestion, activeKey, modelLabel);
    renderSuggestion("CONTEXT_COMPACT_LIMIT", "compactLimit", suggestion, activeKey, modelLabel);
  }

  /**
   * Friendly human-readable name for the active brain. Falls back to the
   * raw ollama key if the active model isn't in the blessed list.
   */
  function brainDisplayName(activeKey: string, modelLabel: string | undefined): string {
    return modelLabel ?? activeKey;
  }

  function renderSuggestion(
    fieldKey: string,
    valueKey: "numCtx" | "compactLimit",
    suggestion: ContextSuggestion | undefined,
    activeKey: string,
    modelLabel?: string,
  ): void {
    const slot = suggestionSlots.get(fieldKey);
    if (!slot) return;
    slot.replaceChildren();
    if (!suggestion) return;

    const value = suggestion[valueKey];
    const isCompact = valueKey === "compactLimit";
    const fmtNumber = (n: number) => n.toLocaleString("en-US");

    const wrap = document.createElement("div");
    wrap.className = "env-field__suggestion";
    if (isCompact) wrap.classList.add("env-field__suggestion--compact");

    // Eyebrow ─── RECOMMENDED FOR <BRAIN NAME> ──────────────────────
    const eyebrow = document.createElement("p");
    eyebrow.className = "env-field__suggestion-eyebrow";
    const eyebrowText = document.createElement("span");
    eyebrowText.textContent = isCompact ? "Auto-compact pairs with" : "Recommended for";
    const eyebrowBrain = document.createElement("span");
    eyebrowBrain.className = "env-field__suggestion-eyebrow-brain";
    eyebrowBrain.textContent = brainDisplayName(activeKey, modelLabel);
    eyebrow.append(eyebrowText, eyebrowBrain);

    // Big number with serif italic unit
    const figure = document.createElement("p");
    figure.className = "env-field__suggestion-figure";
    const numberEl = document.createElement("span");
    numberEl.className = "env-field__suggestion-number";
    numberEl.textContent = fmtNumber(value);
    const unitEl = document.createElement("span");
    unitEl.className = "env-field__suggestion-unit";
    unitEl.textContent = "tokens";
    figure.append(numberEl, unitEl);

    // Meta column — different copy for the two variants
    const meta = document.createElement("div");
    meta.className = "env-field__suggestion-meta";
    if (isCompact) {
      const line = document.createElement("p");
      line.className = "env-field__suggestion-meta-line";
      const pct = Math.round((suggestion.compactLimit / suggestion.numCtx) * 100);
      const strong = document.createElement("strong");
      strong.textContent = `${pct}%`;
      line.append(
        strong,
        document.createTextNode(
          " of the suggested context window — auto-compact fires here so the model never sees a hard wall",
        ),
      );
      meta.append(line);
    } else {
      const line1 = document.createElement("p");
      line1.className = "env-field__suggestion-meta-line";
      const k = Math.round(suggestion.numCtx / 1024);
      const strong = document.createElement("strong");
      strong.textContent = `${k}k`;
      line1.append(strong, document.createTextNode(" tokens of working memory"));

      const line2 = document.createElement("p");
      line2.className = "env-field__suggestion-meta-line";
      line2.textContent = `Pairs with auto-compact at ${fmtNumber(suggestion.compactLimit)}`;
      meta.append(line1, line2);
    }

    // Apply button — gold "Apply →" CTA, flips to green "Applied ✓"
    // when the input value already matches the suggestion. State is
    // reactive: if the user edits the input afterward, the button reverts
    // to its gold form so they can re-apply with one click.
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "env-field__suggestion-apply";
    const expected = String(value);

    /** Flip the button between Apply→Applied based on input.value. */
    const syncButtonState = (justApplied: boolean) => {
      const input = inputs.get(fieldKey);
      const matches = input?.value === expected;
      if (matches) {
        apply.classList.add("env-field__suggestion-apply--applied");
        apply.textContent = "Applied";
        apply.title = `${fieldKey} matches the suggested value (${fmtNumber(value)})`;
        if (justApplied) {
          // Snap-in micro-animation only on the actual click transition,
          // not on initial render. animationend cleanup handler removes
          // the class so re-clicks always re-trigger.
          apply.classList.add("env-field__suggestion-apply--just-applied");
          apply.addEventListener(
            "animationend",
            () => apply.classList.remove("env-field__suggestion-apply--just-applied"),
            { once: true },
          );
        }
      } else {
        apply.classList.remove("env-field__suggestion-apply--applied");
        apply.classList.remove("env-field__suggestion-apply--just-applied");
        apply.textContent = "Apply";
        apply.title = `Set ${fieldKey} to ${fmtNumber(value)}`;
      }
    };

    apply.addEventListener("click", () => {
      const input = inputs.get(fieldKey);
      if (!input) return;
      // No-op if the value already matches — the button is already in
      // its applied state and clicking it again would be a lie.
      if (input.value === expected) return;
      input.value = expected;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      // Pulse the input briefly so the user sees exactly which field got
      // the new value. The class auto-cleans on animationend so repeated
      // applies always re-trigger the animation.
      input.classList.add("env-field__input--pulse");
      input.addEventListener(
        "animationend",
        () => input.classList.remove("env-field__input--pulse"),
        { once: true },
      );
      syncButtonState(true);
      toast(
        `${fieldKey} set to ${fmtNumber(value)}. Click "Save changes" to write.`,
        "info",
      );
    });

    // Reactive sync: when the user edits the input directly, the button
    // should reflect whether the current value still matches the suggestion.
    const input = inputs.get(fieldKey);
    if (input) {
      input.addEventListener("input", () => syncButtonState(false));
    }
    // Initial state — runs after the form is built so input.value is set.
    syncButtonState(false);

    wrap.append(eyebrow, figure, meta, apply);

    // Rationale (only on the primary OLLAMA_NUM_CTX card — the compact
    // variant inherits the reasoning from its sibling and doesn't repeat).
    if (!isCompact) {
      const why = document.createElement("p");
      why.className = "env-field__suggestion-why";
      why.textContent = suggestion.rationale;
      wrap.append(why);
    }

    slot.append(wrap);
  }

  // Initial render — runs once after the form is built. Don't await; the
  // suggestion is non-critical UI and we don't want to block the page on
  // a second API call.
  void refreshSuggestions();

  // Render extras (keys present in .env but not in schema)
  if (payload.extras.length) {
    const extras = document.createElement("fieldset");
    extras.className = "env-group";
    const legend = document.createElement("legend");
    legend.className = "env-group__legend";
    legend.textContent = "Custom";
    extras.append(legend);
    for (const key of payload.extras) {
      const synthetic: EnvField = {
        key,
        label: key,
        description: "Custom variable not in the Krull schema.",
        kind: "text",
        group: "Custom",
        affects: [],
      };
      extras.append(
        renderField(synthetic, payload.values[key] ?? "", inputs, suggestionSlots),
      );
    }
    form.append(extras);
  }

  // Footer with Save + Restart actions
  const footer = document.createElement("div");
  footer.className = "env-form__footer";

  const path = document.createElement("p");
  path.className = "env-form__path";
  path.textContent = payload.path;

  const actions = document.createElement("div");
  actions.className = "env-form__actions";

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn--primary";
  saveBtn.textContent = "Save changes";

  const restartBtn = document.createElement("button");
  restartBtn.type = "button";
  restartBtn.className = "btn btn--ghost";
  restartBtn.textContent = "Restart affected services";
  restartBtn.disabled = true;

  let lastAffected: string[] = [];

  saveBtn.addEventListener("click", async () => {
    const values: Record<string, string> = {};
    for (const [k, input] of inputs) values[k] = input.value;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const result = await saveEnv(values);
      if (result.changed.length === 0) {
        toast("No changes to save.", "info");
      } else {
        toast(`Saved ${result.changed.length} change${result.changed.length === 1 ? "" : "s"}.`, "success");
      }
      lastAffected = result.affects;
      restartBtn.disabled = lastAffected.length === 0;
      if (lastAffected.length > 0) {
        restartBtn.textContent = `Restart ${lastAffected.length} service${lastAffected.length === 1 ? "" : "s"}`;
      }
      // If a model re-tune was kicked off (because temperature/top_p/etc
      // changed), stream its progress so the user knows their parameter
      // change is actually being applied to every installed model.
      if (result.retuneJobId) {
        toast("Re-tuning installed models with new parameters…", "info");
        const stop = streamJob(result.retuneJobId, (ev) => {
          if (ev.phase === "done") {
            stop();
            toast(ev.message ?? "Models re-tuned.", "success");
          } else if (ev.phase === "failed") {
            stop();
            toast(`Re-tune failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          }
        });
      }
    } catch (err) {
      toast(`Save failed: ${(err as Error).message}`, "error", 6000);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save changes";
    }
  });

  restartBtn.addEventListener("click", async () => {
    if (lastAffected.length === 0) return;
    restartBtn.disabled = true;
    restartBtn.textContent = "Restarting…";
    let okCount = 0;
    for (const container of lastAffected) {
      try {
        await restartContainer(container);
        okCount++;
      } catch (err) {
        toast(`Failed to restart ${container}: ${(err as Error).message}`, "error", 6000);
      }
    }
    if (okCount > 0) {
      toast(`Restarted ${okCount} service${okCount === 1 ? "" : "s"}.`, "success");
    }
    restartBtn.textContent = "Restart affected services";
    lastAffected = [];
  });

  actions.append(saveBtn, restartBtn);
  footer.append(path, actions);
  form.append(footer);
  section.append(form);

  return root;
}

function renderField(
  field: EnvField,
  value: string,
  bag: Map<string, HTMLInputElement>,
  suggestionSlots: Map<string, HTMLDivElement>,
): HTMLElement {
  const row = document.createElement("div");
  row.className = "env-field";

  const labelRow = document.createElement("div");
  labelRow.className = "env-field__label-row";

  const label = document.createElement("label");
  label.className = "env-field__label";
  label.htmlFor = `env-${field.key}`;
  label.textContent = field.label;

  const key = document.createElement("code");
  key.className = "env-field__key";
  key.textContent = field.key;

  labelRow.append(label, key);

  const desc = document.createElement("p");
  desc.className = "env-field__desc";
  desc.textContent = field.description;

  const inputWrap = document.createElement("div");
  inputWrap.className = "env-field__input-wrap";

  const input = document.createElement("input");
  input.id = `env-${field.key}`;
  input.name = field.key;
  input.value = value;
  input.className = "env-field__input";
  if (field.kind === "number") input.inputMode = "decimal";
  if (field.kind === "secret") {
    input.type = "password";
    input.autocomplete = "off";
    const reveal = document.createElement("button");
    reveal.type = "button";
    reveal.className = "env-field__reveal";
    reveal.textContent = "Show";
    reveal.setAttribute("aria-label", `Reveal ${field.label}`);
    reveal.addEventListener("click", () => {
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      reveal.textContent = hidden ? "Hide" : "Show";
    });
    inputWrap.append(input, reveal);
  } else {
    input.type = "text";
    inputWrap.append(input);
  }

  bag.set(field.key, input);

  row.append(labelRow, desc, inputWrap);

  // Reserve an empty slot under the two context-management fields. The
  // settings page populates these via refreshSuggestions() based on the
  // active brain's contextSuggestion payload — there's nothing to render
  // here at field-build time, just an attachment point.
  if (field.key === "OLLAMA_NUM_CTX" || field.key === "CONTEXT_COMPACT_LIMIT") {
    const slot = document.createElement("div");
    slot.className = "env-field__suggestion-slot";
    suggestionSlots.set(field.key, slot);
    row.append(slot);
  }

  return row;
}
