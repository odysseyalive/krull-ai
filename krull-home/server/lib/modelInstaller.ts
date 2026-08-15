@@
 const REPO = process.env.KRULL_REPO ?? "/workspace";
 const OLLAMA_URL = process.env.OLLAMA_URL ?? "http://krull-ollama:11434";
 const WEBUI_URL = process.env.WEBUI_INTERNAL_URL ?? "http://krull-webui:8080";
+
+// Optional preferred quantization suffix (e.g. q4_0, q8_0). When set, the
+// installer will try a pull of modelKey-<PREFERRED_QUANT> first and fall back
+// to the base model if that artifact isn't available in the Ollama registry.
+const PREFERRED_QUANT = process.env.OLLAMA_PREFERRED_QUANT ?? "";
@@
-  const res = await fetch(`${OLLAMA_URL}/api/pull`, {
-    method: "POST",
-    headers: { "content-type": "application/json" },
-    body: JSON.stringify({ model: modelKey, name: modelKey, stream: true }),
-  });
+  // Helper to pull a single candidate and return whether it succeeded.
+  async function pullCandidate(candidate: string): Promise<boolean> {
+    const res = await fetch(`${OLLAMA_URL}/api/pull`, {
+      method: "POST",
+      headers: { "content-type": "application/json" },
+      body: JSON.stringify({ model: candidate, name: candidate, stream: true }),
+    });
+    if (!res.ok || !res.body) {
+      const text = await res.text().catch(() => "");
+      pushEvent(job, {
+        phase: "downloading",
+        error: `ollama /api/pull failed (${res.status}): ${text.slice(-300)}`,
+        timestamp: Date.now(),
+      });
+      return false;
+    }
+
+    // Stream parser mirrors the existing code: return true if we saw a
+    // success event in the NDJSON stream.
+    const reader = res.body.getReader();
+    const decoder = new TextDecoder();
+    let buf = "";
+    let lastEmittedPercent = -1;
+    let lastEmitMs = 0;
+    let sawSuccess = false;
+
+    const handleLine = (line: string) => {
+      if (!line.trim()) return;
+      let ev: { status?: string; digest?: string; total?: number; completed?: number; error?: string };
+      try {
+        ev = JSON.parse(line);
+      } catch {
+        return;
+      }
+      if (ev.error) {
+        pushEvent(job, { phase: "failed", error: ev.error, timestamp: Date.now() });
+        return;
+      }
+      if (typeof ev.total === "number" && typeof ev.completed === "number" && ev.total > 0) {
+        const percent = Math.min(100, Math.floor((ev.completed / ev.total) * 100));
+        const now = Date.now();
+        if (percent !== lastEmittedPercent && now - lastEmitMs >= 200) {
+          lastEmittedPercent = percent;
+          lastEmitMs = now;
+          pushEvent(job, { phase: "downloading", percent, bytes: ev.completed, total: ev.total, message: ev.status ?? `Pulling ${modelKey}`, timestamp: now });
+        }
+      } else if (ev.status) {
+        pushEvent(job, { phase: "downloading", message: ev.status, timestamp: Date.now() });
+        if (ev.status === "success") sawSuccess = true;
+      }
+    };
+
+    try {
+      for (;;) {
+        const { value, done } = await reader.read();
+        if (done) break;
+        buf += decoder.decode(value, { stream: true });
+        let nl: number;
+        while ((nl = buf.indexOf("\n")) >= 0) {
+          const line = buf.slice(0, nl);
+          buf = buf.slice(nl + 1);
+          handleLine(line);
+        }
+      }
+      if (buf.length > 0) handleLine(buf);
+    } catch (err) {
+      pushEvent(job, { phase: "failed", error: `pull stream error: ${(err as Error).message}`, timestamp: Date.now() });
+      return false;
+    }
+
+    return sawSuccess;
+  }
+
+  // If a preferred quant is set, try it first, then fall back to the base model.
+  const candidates: string[] = [];
+  if (PREFERRED_QUANT) {
+    const qc = `${modelKey}-${PREFERRED_QUANT}`;
+    if (qc !== modelKey) candidates.push(qc);
+  }
+  candidates.push(modelKey);
+
+  let pulledSuccessfully = false;
+  let pulledName = modelKey;
+  for (const cand of candidates) {
+    pushEvent(job, { phase: "downloading", message: `Starting pull of ${cand}…`, timestamp: Date.now() });
+    const ok = await pullCandidate(cand);
+    if (ok) {
+      pulledSuccessfully = true;
+      pulledName = cand;
+      break;
+    }
+    // otherwise try next candidate
+  }
+  if (!pulledSuccessfully) {
+    pushEvent(job, { phase: "failed", error: "ollama /api/pull ended without a success event for all candidates", timestamp: Date.now() });
+    return;
+  }
@@
-  // Pull is done — bake in the tuned sampling params, matching what the
-  // pull-model.sh shell script would have done. This is local-only
-  // (calls /api/create against the same daemon) and typically completes
-  // in well under a second.
+  // Pull is done — bake in the tuned sampling params, matching what the
+  // pull-model.sh shell script would have done. This is local-only
+  // (calls /api/create against the same daemon) and typically completes
+  // in well under a second. Note: we retune the actual pulledName so
+  // quantized candidate tuning is applied to the correct model.
   pushEvent(job, {
     phase: "downloading",
     percent: 100,
     message: "Applying tuned parameters…",
     timestamp: Date.now(),
   });
   try {
     const params = await tuningParamsFromEnv();
-    await retuneModel(modelKey, params);
+    await retuneModel(pulledName, params);
   } catch (err) {
     pushEvent(job, {
       phase: "failed",
       error: `tuning failed: ${(err as Error).message}`,
       timestamp: Date.now(),
     });
     return;
   }
@@
-  pushEvent(job, {
-    phase: "done",
-    message: `Pulled ${modelKey}`,
-    timestamp: Date.now(),
-  });
+  pushEvent(job, {
+    phase: "done",
+    message: `Pulled ${pulledName}`,
+    timestamp: Date.now(),
+  });
 }
