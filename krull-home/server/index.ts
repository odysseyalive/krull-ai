import express from "express";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import servicesRouter from "./routes/services.js";
import envRouter from "./routes/env.js";
import systemRouter from "./routes/system.js";
import libraryRouter from "./routes/library.js";
import modelsRouter from "./routes/models.js";
import updateRouter from "./routes/update.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT ?? 8000);
const REPO = process.env.KRULL_REPO ?? "/workspace";

const app = express();
app.use(express.json({ limit: "256kb" }));

app.get("/api/health", (_req, res) => {
  res.json({ ok: true, repo: REPO, repoExists: fs.existsSync(REPO) });
});

app.use("/api", servicesRouter);
app.use("/api", envRouter);
app.use("/api", systemRouter);
app.use("/api", libraryRouter);
app.use("/api", modelsRouter);
app.use("/api", updateRouter);

// Serve the Vite-built frontend.
// In dev (tsx watch), __dirname is .../server, so dist sits next to it.
// In prod (compiled to dist-server), __dirname is .../dist-server, dist is one level up.
const candidates = [
  path.resolve(__dirname, "../dist"),
  path.resolve(__dirname, "../../dist"),
];
const distDir = candidates.find((p) => fs.existsSync(p));

if (distDir) {
  app.use(express.static(distDir));
  app.get("*", (_req, res) => {
    res.sendFile(path.join(distDir, "index.html"));
  });
} else {
  app.get("*", (_req, res) => {
    res
      .status(503)
      .send(
        "<h1>Krull Home</h1><p>Frontend not built yet. Run <code>yarn build</code>.</p>",
      );
  });
}

app.listen(PORT, () => {
  console.log(`[krull-home] listening on :${PORT}`);
  console.log(`[krull-home] repo=${REPO} dist=${distDir ?? "(none)"}`);
});
