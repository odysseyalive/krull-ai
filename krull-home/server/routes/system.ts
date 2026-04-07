import { Router } from "express";
import { isRestartable, restartContainer } from "../lib/docker.js";

const router = Router();

router.post("/restart/:container", async (req, res) => {
  const name = req.params.container;
  if (!isRestartable(name)) {
    res.status(403).json({ error: `container not in restart whitelist: ${name}` });
    return;
  }
  try {
    await restartContainer(name);
    res.json({ ok: true, container: name });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    res.status(500).json({ error: message });
  }
});

export default router;
