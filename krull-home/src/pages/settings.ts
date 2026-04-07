import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { toast } from "../components/Toast";
import { ModelPicker } from "../components/ModelPicker";
import {
  fetchEnv,
  restartContainer,
  saveEnv,
  streamJob,
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
  // OLLAMA_MODEL form input below stays in sync. Otherwise saving the env
  // form afterwards would silently overwrite the picker's change.
  const onModelChanged = (e: Event) => {
    const detail = (e as CustomEvent<{ key: string }>).detail;
    const input = inputs.get("OLLAMA_MODEL");
    if (input && detail?.key) input.value = detail.key;
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

  for (const [groupName, fields] of groups) {
    const grp = document.createElement("fieldset");
    grp.className = "env-group";
    const legend = document.createElement("legend");
    legend.className = "env-group__legend";
    legend.textContent = groupName;
    grp.append(legend);

    for (const field of fields) {
      grp.append(renderField(field, payload.values[field.key] ?? "", inputs));
    }
    form.append(grp);
  }

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
      extras.append(renderField(synthetic, payload.values[key] ?? "", inputs));
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
  return row;
}
