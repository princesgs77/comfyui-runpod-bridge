import express from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";

const app = express();
const port = Number(process.env.PORT || 10000);

const apiKey = process.env.RUNPOD_API_KEY;
const endpointId = process.env.RUNPOD_ENDPOINT_ID;
const accessToken = process.env.APP_ACCESS_TOKEN;

const __dirname = path.dirname(fileURLToPath(import.meta.url));

if (!apiKey || !endpointId || !accessToken) {
  throw new Error(
    "Set RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID and APP_ACCESS_TOKEN"
  );
}

const runpodBase = `https://api.runpod.ai/v2/${endpointId}`;

app.use(express.json({ limit: "2mb" }));

// Protect the bridge. Use the same token from your frontend.
app.use("/api", (req, res, next) => {
  const token = req.get("authorization");

  if (token !== `Bearer ${accessToken}`) {
    return res.status(401).json({ error: "Unauthorized" });
  }

  next();
});

async function runpodRequest(endpoint, options = {}) {
  const response = await fetch(`${runpodBase}${endpoint}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      ...options.headers
    },
    signal: AbortSignal.timeout(20000)
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(
      `RunPod returned ${response.status}: ${JSON.stringify(data)}`
    );
  }

  return data;
}

app.get("/api/health", async (req, res) => {
  try {
    const data = await runpodRequest("/health");
    res.json({ bridge: "ok", runpod: data });
  } catch (error) {
    res.status(502).json({ error: error.message });
  }
});

// Submit an API-format ComfyUI workflow.
app.post("/api/run", async (req, res) => {
  const workflow = req.body?.workflow;

  if (
    !workflow ||
    typeof workflow !== "object" ||
    Array.isArray(workflow) ||
    Object.keys(workflow).length === 0
  ) {
    return res.status(400).json({
      error: "Provide a non-empty API-format workflow object"
    });
  }

  try {
    const data = await runpodRequest("/run", {
      method: "POST",
      body: JSON.stringify({
        input: { workflow }
      })
    });

    res.status(202).json(data);
  } catch (error) {
    console.error("RunPod submission failed:", error.message);
    res.status(502).json({ error: "Could not submit the workflow" });
  }
});

// Check the status of a submitted job.
app.get("/api/status/:jobId", async (req, res) => {
  const jobId = req.params.jobId;

  if (!/^[\w-]+$/.test(jobId)) {
    return res.status(400).json({ error: "Invalid job ID" });
  }

  try {
    const data = await runpodRequest(
      `/status/${encodeURIComponent(jobId)}`
    );

    res.json(data);
  } catch (error) {
    console.error("RunPod status failed:", error.message);
    res.status(502).json({ error: "Could not retrieve job status" });
  }
});

// Serve the lightweight test UI.
app.use(express.static(path.join(__dirname, "public")));

app.listen(port, "0.0.0.0", () => {
  console.log(`Bridge listening on port ${port}`);
});
