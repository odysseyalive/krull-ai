@@
   {
     key: "CONTEXT_COMPACT_LIMIT",
     label: "Auto-compact threshold",
     description:
       "Token count at which conversation history is auto-compacted. Should be roughly 75% of OLLAMA_NUM_CTX.",
     kind: "number",
     default: "98304",
     group: "Context management",
     affects: ["krull-sse-proxy"],
   },
+  {
+    key: "CONTEXT_COMPACT_ENABLED",
+    label: "Enable async compaction",
+    description:
+      "Enable the SSE proxy's asynchronous context compaction pipeline (job queue + background summarizer).",
+    kind: "text",
+    default: "true",
+    group: "Context management",
+    affects: ["krull-sse-proxy"],
+  },
+  {
+    key: "CONTEXT_SUMMARY_MODEL",
+    label: "Summary model",
+    description:
+      "Small model used for abstractive summarization of old history (empty = same model mapping).",
+    kind: "text",
+    default: "claude-haiku-4-5",
+    group: "Context management",
+    affects: ["krull-sse-proxy", "krull-litellm"],
+  },
+  {
+    key: "AGENT_MAX_PARALLEL",
+    label: "Agent parallelism",
+    description:
+      "Maximum number of parallel agent workers. Limits concurrency to avoid saturating GPU/CPU.",
+    kind: "number",
+    default: "3",
+    group: "Agents",
+    affects: ["krull-sse-proxy"],
+  },
+  {
+    key: "KRULL_AGENT_TOKEN_BUDGET",
+    label: "Per-agent token budget",
+    description: "Token budget enforced per spawned agent (helps prevent runaway generations).",
+    kind: "number",
+    default: "4096",
+    group: "Agents",
+    affects: ["krull-sse-proxy"],
+  },
+  {
+    key: "KRULL_AGENT_TIMEOUT_SECONDS",
+    label: "Per-agent timeout (s)",
+    description: "Maximum wall-clock time allowed per agent worker before cancellation.",
+    kind: "number",
+    default: "120",
+    group: "Agents",
+    affects: ["krull-sse-proxy"],
+  },
@@
 export function affectedContainersFor(changedKeys: string[]): string[] {
