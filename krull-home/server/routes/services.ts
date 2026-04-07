import { Router } from "express";
import { getContainerState, type ServiceStatus } from "../lib/docker.js";

const router = Router();

interface ServiceDef {
  name: string;
  container: string;
  url: string;
}

const SERVICES: ServiceDef[] = [
  {
    name: "AI Chat",
    container: "krull-webui",
    url: "http://localhost:3000",
  },
  {
    name: "Maps",
    container: "krull-map-viewer",
    url: "http://localhost:8070",
  },
  {
    name: "Knowledge",
    container: "krull-kiwix",
    url: "http://localhost:8090",
  },
];

router.get("/services", async (_req, res) => {
  const out: ServiceStatus[] = await Promise.all(
    SERVICES.map(async (svc) => ({
      ...svc,
      state: await getContainerState(svc.container),
    })),
  );
  res.json({ services: out });
});

export default router;
