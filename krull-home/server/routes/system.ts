import { Router } from "express";
import { isRestartable, restartContainer } from "../lib/docker.js";
import { detectHardware } from "../lib/hardware.js";

const router = Router();

/**
 * Host hardware probe — RAM + GPU. Powers the HardwarePill in the top
 * nav so the user can see at a glance whether inference will run on
 * GPU or CPU and how much memory they have left. Cached for ~10 s
 * inside detectHardware() to keep nvidia-smi probes cheap.
 */
router.get("/system/hardware", async (_req, res) => {
  try {
    const hardware = await detectHardware();
    res.json({ hardware });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    res.status(500).json({ error: message });
  }
});

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
