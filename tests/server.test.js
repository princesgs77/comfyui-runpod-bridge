process.env.NODE_ENV = "test";
process.env.APP_ACCESS_TOKEN = "test_secret_token_123";

import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { app, server, sanitizeError, promptStore } from "../server.js";

const TEST_PORT = 19999;
let testServer;
const BASE_URL = `http://127.0.0.1:${TEST_PORT}`;

before((t, done) => {
  testServer = http.createServer(app);
  testServer.listen(TEST_PORT, "127.0.0.1", done);
});

after((t, done) => {
  if (testServer) {
    testServer.close(() => {
      done();
    });
  } else {
    done();
  }
});

describe("Render Bridge & ComfyUI Compatibility Adapter Test Suite", () => {

  test("Authentication failure on protected /api/run without token", async () => {
    const res = await fetch(`${BASE_URL}/api/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: { "1": {} } }),
    });
    assert.equal(res.status, 401);
    const data = await res.json();
    assert.match(data.error, /Unauthorized/);
  });

  test("Authentication failure on protected /prompt with invalid token", async () => {
    const res = await fetch(`${BASE_URL}/prompt`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer wrong_token_xyz",
      },
      body: JSON.stringify({ prompt: { "1": {} } }),
    });
    assert.equal(res.status, 401);
  });

  test("Health check /api/health is publicly accessible and returns bridge status", async () => {
    const res = await fetch(`${BASE_URL}/api/health`);
    assert.equal(res.status, 200);
    const data = await res.json();
    assert.equal(data.bridge, "ok");
    assert.ok(data.timestamp);
    assert.equal(typeof data.configured, "object");
  });

  test("Public model manifest /api/manifest returns allowlisted entries", async () => {
    const res = await fetch(`${BASE_URL}/api/manifest`);
    assert.equal(res.status, 200);
    const data = await res.json();
    assert.ok(Array.isArray(data.models));
    assert.ok(data.models.length > 0);
    const first = data.models[0];
    assert.ok(first.model_id);
    assert.ok(first.category);
    assert.ok(first.filename);
    // Ensure no private drive IDs or keys leaked in public manifest
    assert.equal(first.gdrive_file_id, undefined);
  });

  test("Malformed workflow payload rejected by /api/run", async () => {
    const badPayloads = [
      {},
      { workflow: null },
      { workflow: "not_an_object" },
      { workflow: [] },
      { workflow: {} },
    ];

    for (const body of badPayloads) {
      const res = await fetch(`${BASE_URL}/api/run`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer test_secret_token_123",
        },
        body: JSON.stringify(body),
      });
      assert.equal(res.status, 400);
      const data = await res.json();
      assert.match(data.error, /Provide a valid non-empty API-format workflow/);
    }
  });

  test("Malformed prompt graph rejected by ComfyUI /prompt endpoint", async () => {
    const res = await fetch(`${BASE_URL}/prompt`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer test_secret_token_123",
      },
      body: JSON.stringify({ prompt: {} }),
    });
    assert.equal(res.status, 400);
    const data = await res.json();
    assert.ok(data.error);
    assert.equal(typeof data.node_errors, "object");
  });

  test("Invalid job ID format rejected by /api/status/:jobId", async () => {
    const res = await fetch(`${BASE_URL}/api/status/../../etc/passwd`, {
      headers: { Authorization: "Bearer test_secret_token_123" },
    });
    // Path traversal or invalid characters should fail validation
    assert.ok(res.status === 400 || res.status === 404);
  });

  test("ComfyUI /object_info returns cached node schemas", async () => {
    const res = await fetch(`${BASE_URL}/object_info`);
    assert.equal(res.status, 200);
    const data = await res.json();
    assert.ok(data.KSampler, "KSampler should be registered in object_info");
    assert.ok(data.CheckpointLoaderSimple, "CheckpointLoaderSimple should be registered in object_info");
    assert.ok(data.EmptyLatentImage, "EmptyLatentImage should be registered in object_info");
  });

  test("ComfyUI /system_stats returns simulated worker environment", async () => {
    const res = await fetch(`${BASE_URL}/system_stats`);
    assert.equal(res.status, 200);
    const data = await res.json();
    assert.ok(data.system);
    assert.ok(Array.isArray(data.devices));
    assert.equal(data.devices[0].type, "cuda");
  });

  test("ComfyUI /queue returns running and pending queues", async () => {
    const res = await fetch(`${BASE_URL}/queue`);
    assert.equal(res.status, 200);
    const data = await res.json();
    assert.ok(Array.isArray(data.queue_running));
    assert.ok(Array.isArray(data.queue_pending));
  });

  test("Credential redaction helper sanitizes sensitive tokens and keys", () => {
    const raw = "Request failed for Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and api_key: runpod_secret_xyz123";
    const cleaned = sanitizeError(raw);
    assert.doesNotMatch(cleaned, /eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9/);
    assert.doesNotMatch(cleaned, /runpod_secret_xyz123/);
    assert.match(cleaned, /Bearer \[REDACTED\]/);
    assert.match(cleaned, /\[REDACTED\]/);
  });

});
