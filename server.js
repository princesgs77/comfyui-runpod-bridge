import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import crypto from "node:crypto";
import express from "express";
import { WebSocketServer, WebSocket } from "ws";

const app = express();
const port = Number(process.env.PORT || 10000);

function getApiKey() {
  return process.env.RUNPOD_API_KEY || "";
}
function getEndpointId() {
  return process.env.RUNPOD_ENDPOINT_ID || "";
}
function getAccessToken() {
  return process.env.APP_ACCESS_TOKEN || "";
}
const runpodBaseUrl = process.env.RUNPOD_BASE_URL || "https://api.runpod.ai/v2";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = process.env.MODEL_MANIFEST_PATH || path.join(__dirname, "worker", "model_manifest.json");
const objectInfoPath = path.join(__dirname, "public", "object_info.json");

// In-memory state tracking (transient, no persistent disk dependency)
const promptStore = new Map(); // prompt_id -> { jobId, clientId, status, prompt, outputs, error, createdAt }
const jobToPrompt = new Map(); // jobId -> prompt_id
const imageCache = new Map();  // filename -> { buffer, mimeType, createdAt }
const uploadedImages = new Map(); // name -> base64 string

// Security helper: redact secrets from error messages
function sanitizeError(msg) {
  if (typeof msg !== "string") return msg;
  let text = msg
    .replace(/Bearer\s+[a-zA-Z0-9_\-\.]{15,}/gi, "Bearer [REDACTED]")
    .replace(/(api[_-]?key[:=]\s*)[a-zA-Z0-9_\-]{15,}/gi, "$1[REDACTED]");
  const key = getApiKey();
  if (key && key.length >= 8) {
    text = text.replaceAll(key, "[REDACTED_API_KEY]");
  }
  return text;
}

// Request size limits (10MB for large workflows/masks)
app.use(express.json({ limit: "10mb" }));
app.use(express.raw({ type: ["image/*", "application/octet-stream"], limit: "20mb" }));

// Helper: RunPod API Client with bounded retries and sanitized logging
async function runpodRequest(endpoint, options = {}) {
  const apiKey = getApiKey();
  const endpointId = getEndpointId();
  if (!apiKey || !endpointId) {
    throw new Error("RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID must be configured.");
  }

  const url = `${runpodBaseUrl}/${endpointId}${endpoint}`;
  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        ...options.headers,
      },
      signal: AbortSignal.timeout(25000),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(`RunPod HTTP ${response.status}: ${JSON.stringify(data)}`);
    }
    return data;
  } catch (err) {
    throw new Error(sanitizeError(err.message));
  }
}

// Authentication middleware
function checkAuth(req) {
  const token = getAccessToken();
  if (!token) return true; // Local development mode if token unconfigured
  const authHeader = req.get("authorization");
  if (authHeader && authHeader.trim() === `Bearer ${token}`) {
    return true;
  }
  // Allow query parameter for image loading tags and WebSockets
  const tokenQuery = req.query.token;
  if (tokenQuery && tokenQuery.trim() === token) {
    return true;
  }
  return false;
}

// Protect API and ComfyUI backend routes
const protectedPrefixes = ["/api/run", "/api/status", "/api/cancel", "/prompt", "/interrupt", "/upload"];

app.use((req, res, next) => {
  const isProtected = protectedPrefixes.some(p => req.path.startsWith(p));
  if (isProtected) {
    if (!checkAuth(req)) {
      return res.status(401).json({ error: "Unauthorized. Missing or invalid Bearer token." });
    }
  }
  next();
});

// ==============================================================================
// 1. Health & Manifest Endpoints
// ==============================================================================

app.get("/api/health", async (req, res) => {
  const healthData = {
    bridge: "ok",
    timestamp: new Date().toISOString(),
    configured: {
      runpodKey: Boolean(getApiKey()),
      endpointId: Boolean(getEndpointId()),
      authRequired: Boolean(getAccessToken()),
    },
    runpod: null,
  };

  if (!getApiKey() || !getEndpointId()) {
    healthData.runpod = { status: "unconfigured" };
    return res.json(healthData);
  }

  try {
    const runpodHealth = await runpodRequest("/health");
    healthData.runpod = runpodHealth;
    res.json(healthData);
  } catch (err) {
    healthData.runpod = { status: "unreachable", error: sanitizeError(err.message) };
    res.status(502).json(healthData);
  }
});

app.get("/api/manifest", (req, res) => {
  if (!fs.existsSync(manifestPath)) {
    return res.status(404).json({ error: "Model manifest not found on server." });
  }

  try {
    const raw = fs.readFileSync(manifestPath, "utf-8");
    const parsed = JSON.parse(raw);
    const publicModels = (parsed.models || []).map(m => ({
      model_id: m.model_id,
      category: m.category,
      filename: m.filename,
      file_size: m.file_size,
      description: m.description || "",
      has_checksum: Boolean(m.expected_sha256),
    }));
    res.json({ version: parsed.version || "1.0", models: publicModels });
  } catch (err) {
    res.status(500).json({ error: "Could not read model manifest." });
  }
});

// ==============================================================================
// 2. Direct Bridge Job API (/api/run, /api/status, /api/cancel)
// ==============================================================================

app.post("/api/run", async (req, res) => {
  const workflow = req.body?.workflow;
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow) || Object.keys(workflow).length === 0) {
    return res.status(400).json({ error: "Provide a valid non-empty API-format workflow object." });
  }

  const payload = {
    input: {
      workflow,
      images: req.body.images,
      models: req.body.models,
      prefetch_models: req.body.prefetch_models,
      comfy_org_api_key: req.body.comfy_org_api_key,
    },
  };

  try {
    const data = await runpodRequest("/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    res.status(202).json(data);
  } catch (err) {
    console.error("RunPod submission failed:", sanitizeError(err.message));
    res.status(502).json({ error: sanitizeError(err.message) });
  }
});

app.get("/api/status/:jobId", async (req, res) => {
  const jobId = req.params.jobId;
  if (!/^[\w-]+$/.test(jobId)) {
    return res.status(400).json({ error: "Invalid job ID format." });
  }

  try {
    const data = await runpodRequest(`/status/${encodeURIComponent(jobId)}`);
    res.json(data);
  } catch (err) {
    console.error("RunPod status error:", sanitizeError(err.message));
    res.status(502).json({ error: sanitizeError(err.message) });
  }
});

app.post("/api/cancel/:jobId", async (req, res) => {
  const jobId = req.params.jobId;
  if (!/^[\w-]+$/.test(jobId)) {
    return res.status(400).json({ error: "Invalid job ID format." });
  }

  try {
    const data = await runpodRequest(`/cancel/${encodeURIComponent(jobId)}`, { method: "POST" });
    res.json(data);
  } catch (err) {
    res.status(502).json({ error: sanitizeError(err.message) });
  }
});

// ==============================================================================
// 3. ComfyUI Editor Compatibility Adapter Layer
// ==============================================================================

// POST /prompt - Submit prompt from stock ComfyUI editor
app.post("/prompt", async (req, res) => {
  const prompt = req.body?.prompt;
  const clientId = req.body?.client_id || "default";

  if (!prompt || typeof prompt !== "object" || Object.keys(prompt).length === 0) {
    return res.status(400).json({
      error: { message: "Invalid prompt graph", details: "Graph is empty or not an object." },
      node_errors: {},
    });
  }

  const promptId = crypto.randomUUID();

  // Attach any uploaded images that were staged
  const images = [];
  for (const [name, b64] of uploadedImages.entries()) {
    images.push({ name, image: b64 });
  }

  try {
    const runpodRes = await runpodRequest("/run", {
      method: "POST",
      body: JSON.stringify({
        input: { workflow: prompt, images },
      }),
    });

    const jobId = runpodRes.id;
    promptStore.set(promptId, {
      jobId,
      clientId,
      status: "IN_QUEUE",
      prompt,
      outputs: {},
      error: null,
      createdAt: Date.now(),
    });
    jobToPrompt.set(jobId, promptId);

    // Notify connected WebSocket clients
    broadcastWsMessage(clientId, {
      type: "status",
      data: { status: { exec_info: { queue_remaining: 1 } } },
    });

    // Start background poller for this job
    pollRunpodJob(jobId, promptId, clientId);

    res.json({
      prompt_id: promptId,
      number: promptStore.size,
      node_errors: {},
    });
  } catch (err) {
    res.status(502).json({
      error: { message: "Could not submit workflow to RunPod Serverless", details: sanitizeError(err.message) },
      node_errors: {},
    });
  }
});

// GET /queue - Return active and pending jobs in ComfyUI format
app.get("/queue", (req, res) => {
  const running = [];
  const pending = [];

  for (const [pId, pData] of promptStore.entries()) {
    if (pData.status === "IN_PROGRESS") {
      running.push([1, pId, pData.prompt, {}]);
    } else if (pData.status === "IN_QUEUE") {
      pending.push([1, pId, pData.prompt, {}]);
    }
  }

  res.json({ queue_running: running, queue_pending: pending });
});

// GET /history & GET /history/:promptId - Return output history
app.get(["/history", "/history/:promptId"], (req, res) => {
  const reqPromptId = req.params.promptId;
  const history = {};

  if (reqPromptId) {
    const item = promptStore.get(reqPromptId);
    if (item && item.status === "COMPLETED") {
      history[reqPromptId] = {
        prompt: [1, reqPromptId, item.prompt, {}],
        outputs: item.outputs,
        status: { status_str: "success", completed: true, messages: [] },
      };
    }
    return res.json(history);
  }

  for (const [pId, item] of promptStore.entries()) {
    if (item.status === "COMPLETED") {
      history[pId] = {
        prompt: [1, pId, item.prompt, {}],
        outputs: item.outputs,
        status: { status_str: "success", completed: true, messages: [] },
      };
    }
  }

  res.json(history);
});

// GET /object_info - Node definitions for editor canvas
app.get("/object_info", (req, res) => {
  if (fs.existsSync(objectInfoPath)) {
    return res.sendFile(objectInfoPath);
  }
  res.json({});
});

// GET /system_stats - System statistics
app.get("/system_stats", (req, res) => {
  res.json({
    system: {
      os: process.platform,
      python_version: "3.12 (RunPod Serverless Worker)",
      embedded_python: false,
    },
    devices: [
      {
        name: "RunPod Serverless GPU (On-Demand)",
        type: "cuda",
        index: 0,
        vram_total: 25769803776,
        vram_free: 20000000000,
        torch_vram_total: 25769803776,
        torch_vram_free: 20000000000,
      },
    ],
  });
});

// GET /features - ComfyUI server features
app.get("/features", (req, res) => {
  res.json({
    supports_prompt: true,
    supports_history: true,
    supports_queue: true,
    bridge_mode: "runpod_serverless",
  });
});

// POST /interrupt - Cancel currently active execution
app.post("/interrupt", async (req, res) => {
  // Find running job
  for (const [pId, pData] of promptStore.entries()) {
    if (pData.status === "IN_PROGRESS" || pData.status === "IN_QUEUE") {
      try {
        await runpodRequest(`/cancel/${pData.jobId}`, { method: "POST" });
        pData.status = "CANCELLED";
      } catch (e) {
        console.error("Cancel failed:", e.message);
      }
    }
  }
  res.json({ success: true });
});

// POST /upload/image - Image upload for img2img / masks
app.post("/upload/image", (req, res) => {
  const filename = req.query.filename || `upload_${Date.now()}.png`;
  let base64Data = "";

  if (Buffer.isBuffer(req.body)) {
    base64Data = req.body.toString("base64");
  } else if (req.body?.image) {
    base64Data = req.body.image;
  }

  if (base64Data) {
    uploadedImages.set(filename, base64Data);
    imageCache.set(filename, {
      buffer: Buffer.from(base64Data, "base64"),
      mimeType: "image/png",
      createdAt: Date.now(),
    });
  }

  res.json({
    name: filename,
    subfolder: "",
    type: "input",
  });
});

// GET /view - Serve generated output image or video
app.get("/view", (req, res) => {
  const filename = req.query.filename;
  if (!filename) {
    return res.status(400).send("Filename required");
  }

  const cached = imageCache.get(filename);
  if (cached) {
    res.setHeader("Content-Type", cached.mimeType || "image/png");
    res.setHeader("Cache-Control", "public, max-age=86400");
    return res.send(cached.buffer);
  }

  res.status(404).send("File not found or still processing.");
});

// Serve frontend UI
app.use(express.static(path.join(__dirname, "public")));

// ==============================================================================
// 4. WebSocket Bridge (/ws) and RunPod Job Poller
// ==============================================================================

const server = http.createServer(app);
const wss = new WebSocketServer({ noServer: true });
const wsClients = new Map(); // clientId -> Set of WebSocket connections

server.on("upgrade", (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (url.pathname === "/ws") {
    // Check authentication
    const token = url.searchParams.get("token") || request.headers.authorization?.replace(/^Bearer\s+/i, "");
    const expected = getAccessToken();
    if (expected && token !== expected) {
      socket.write("HTTP/1.1 401 Unauthorized\r\n\r\n");
      socket.destroy();
      return;
    }

    const clientId = url.searchParams.get("clientId") || "default";
    wss.handleUpgrade(request, socket, head, ws => {
      wss.emit("connection", ws, request, clientId);
    });
  } else {
    socket.destroy();
  }
});

wss.on("connection", (ws, request, clientId) => {
  if (!wsClients.has(clientId)) {
    wsClients.set(clientId, new Set());
  }
  wsClients.get(clientId).add(ws);

  ws.send(JSON.stringify({
    type: "status",
    data: { status: { exec_info: { queue_remaining: 0 } } },
  }));

  ws.on("close", () => {
    const clientSet = wsClients.get(clientId);
    if (clientSet) {
      clientSet.delete(ws);
      if (clientSet.size === 0) wsClients.delete(clientId);
    }
  });
});

function broadcastWsMessage(clientId, payload) {
  const clientSet = wsClients.get(clientId);
  const msgStr = JSON.stringify(payload);
  if (clientSet) {
    for (const ws of clientSet) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(msgStr);
      }
    }
  }
}

// Background status poller for RunPod serverless jobs
async function pollRunpodJob(jobId, promptId, clientId) {
  let isDone = false;
  let lastStatus = "IN_QUEUE";
  const startTime = Date.now();
  const maxPollDuration = 30 * 60 * 1000; // 30 minutes

  while (!isDone && Date.now() - startTime < maxPollDuration) {
    await new Promise(r => setTimeout(r, 1500));

    try {
      const statusData = await runpodRequest(`/status/${jobId}`);
      const status = statusData.status;
      const promptData = promptStore.get(promptId);

      if (!promptData) break;

      if (status === "IN_PROGRESS" && lastStatus !== "IN_PROGRESS") {
        lastStatus = "IN_PROGRESS";
        promptData.status = "IN_PROGRESS";
        broadcastWsMessage(clientId, {
          type: "execution_start",
          data: { prompt_id: promptId },
        });
        broadcastWsMessage(clientId, {
          type: "executing",
          data: { node: "sampling", prompt_id: promptId },
        });
      }

      if (status === "COMPLETED") {
        isDone = true;
        promptData.status = "COMPLETED";

        const outputImages = statusData.output?.images || [];
        const comfyOutputs = {};

        for (const img of outputImages) {
          const fname = img.filename || `output_${Date.now()}.png`;
          const node = img.node_id || "9";
          if (!comfyOutputs[node]) comfyOutputs[node] = { images: [] };

          comfyOutputs[node].images.push({
            filename: fname,
            subfolder: img.subfolder || "",
            type: img.type || "output",
          });

          if (img.data) {
            imageCache.set(fname, {
              buffer: Buffer.from(img.data, "base64"),
              mimeType: img.format === "video" ? "video/mp4" : "image/png",
              createdAt: Date.now(),
            });
          }
        }

        promptData.outputs = comfyOutputs;

        // Broadcast ComfyUI executed event
        broadcastWsMessage(clientId, {
          type: "executed",
          data: { node: "save_image", prompt_id: promptId, output: comfyOutputs },
        });
        broadcastWsMessage(clientId, {
          type: "executing",
          data: { node: null, prompt_id: promptId },
        });
        broadcastWsMessage(clientId, {
          type: "status",
          data: { status: { exec_info: { queue_remaining: 0 } } },
        });
        break;
      }

      if (status === "FAILED" || status === "CANCELLED" || status === "TIMED_OUT") {
        isDone = true;
        promptData.status = status;
        promptData.error = statusData.error || `RunPod job ended with status ${status}`;

        broadcastWsMessage(clientId, {
          type: "execution_error",
          data: {
            prompt_id: promptId,
            exception_message: promptData.error,
            exception_type: status,
          },
        });
        break;
      }
    } catch (err) {
      console.warn("Poll status check error:", sanitizeError(err.message));
    }
  }
}

// Start Server if not imported in a test runner
const isTestEnv = process.env.NODE_ENV === "test" || Boolean(process.env.NODE_TEST_CONTEXT);
if (!isTestEnv) {
  server.listen(port, "0.0.0.0", () => {
    console.log(`ComfyUI Remote Bridge listening on port ${port}`);
  });
}

export { app, server, wss, runpodRequest, sanitizeError, promptStore, imageCache };
