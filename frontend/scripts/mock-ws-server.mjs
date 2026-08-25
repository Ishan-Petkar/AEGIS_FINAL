#!/usr/bin/env node
/**
 * mock-ws-server.mjs — Ticket #4: standalone WebSocket server standing in
 * for the real `WS /ws/stream` (Ticket #9) so frontend work never blocks
 * on backend readiness (PHASE5_BUILD_PLAN.md §9).
 *
 * Envelope shape and field names are copied verbatim from
 * `backend/ingest.py` (`_broadcast_batch` for `event`, `_handle_anomalies`
 * for `alert`/`cii`) — see docs/PHASE5_TICKET4_PLAN.md §3 (D4-1). This is
 * a drop-in for the real endpoint: same path (`/ws/stream`), same envelope
 * `{"type": "event"|"alert"|"cii", "data": {...}}`. Ticket #12's job is a
 * URL change and nothing else.
 *
 * Usage:
 *   node scripts/mock-ws-server.mjs [--port 8001] [--rate 8] [--seed 42]
 *
 * Dev-only tool. Never imported by application code; must never reach a
 * production bundle (see package.json's `ws` devDependency).
 */

import { WebSocketServer } from "ws";

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { port: 8001, rate: 8, seed: 42 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--port") args.port = Number(argv[++i]);
    else if (a === "--rate") args.rate = Number(argv[++i]);
    else if (a === "--seed") args.seed = Number(argv[++i]);
  }
  return args;
}

const { port: PORT, rate: RATE_PER_SEC, seed: SEED } = parseArgs(
  process.argv.slice(2)
);

const PATH = "/ws/stream";
const BACKEND_HTTP_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

// ---------------------------------------------------------------------------
// Deterministic PRNG (mulberry32) so --seed is reproducible.
// ---------------------------------------------------------------------------

function mulberry32(seed) {
  let a = seed >>> 0;
  return function rand() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rand = mulberry32(SEED);
const randInt = (min, max) => Math.floor(rand() * (max - min + 1)) + min;
const choice = (arr) => arr[randInt(0, arr.length - 1)];

// ---------------------------------------------------------------------------
// D4-2: realistic asset names. Fetch the real topology on startup; fall
// back to a small hardcoded list (drawn from src/config.py's
// SMART_CITY_ASSETS / DEPENDENCY_GRAPH) so the mock runs standalone.
// ---------------------------------------------------------------------------

const FALLBACK_ASSETS = [
  "Traffic_Cam_1",
  "Traffic_Cam_2",
  "Traffic_Controller",
  "Power_Substation",
  "City_Grid",
  "Emergency_Route_System",
  "Citizen_Portal",
  "SCADA_Historian",
  "City_Payment_Gateway",
  "Bank_Partner_API",
  "Municipal_Bond_Platform",
  "Social_Welfare_System",
];

const PROTOCOLS = ["TCP", "UDP", "ICMP", "HTTPS"];

async function loadAssetNames() {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2000);
    const res = await fetch(`${BACKEND_HTTP_URL}/api/topology`, {
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    const names = Array.isArray(body?.nodes)
      ? body.nodes.map((n) => n.name).filter(Boolean)
      : [];
    if (names.length > 0) {
      console.log(
        `[mock-ws] loaded ${names.length} asset names from ${BACKEND_HTTP_URL}/api/topology`
      );
      return names;
    }
    throw new Error("empty topology response");
  } catch (err) {
    console.log(
      `[mock-ws] backend unreachable at ${BACKEND_HTTP_URL} (${err.message}); ` +
        `falling back to ${FALLBACK_ASSETS.length} hardcoded asset names`
    );
    return FALLBACK_ASSETS;
  }
}

// ---------------------------------------------------------------------------
// IP generation — D4-2: mostly 192.168.10.x and 10.0.1.x, occasional
// external address.
// ---------------------------------------------------------------------------

function randomIp() {
  const roll = rand();
  if (roll < 0.5) return `192.168.10.${randInt(2, 250)}`;
  if (roll < 0.9) return `10.0.1.${randInt(2, 250)}`;
  return `203.0.113.${randInt(2, 250)}`; // TEST-NET-3, plausible "external"
}

// ---------------------------------------------------------------------------
// Envelope builders — field-for-field parity with backend/ingest.py
// ---------------------------------------------------------------------------

const ENVELOPE_EVENT = "event";
const ENVELOPE_ALERT = "alert";
const ENVELOPE_CII = "cii";

let nextEventId = 1;
let nextAlertId = 1;
let nextSnapshotId = 1;
let batchIndex = 0;
const REPLAY_SESSION_ID = `mock-${SEED}-${Date.now()}`;

function buildEvent(assetNames, isAnomaly) {
  const id = nextEventId++;
  const now = new Date().toISOString();
  const rawScore = isAnomaly ? rand() * 0.4 + 0.6 : rand() * 0.5;
  const calibratedScore = isAnomaly ? rand() * 0.3 + 0.7 : rand() * 0.4;
  const tripwireFired = isAnomaly && rand() < 0.15;

  return {
    type: ENVELOPE_EVENT,
    data: {
      id,
      ts: now,
      observed_at: now,
      source_ip: randomIp(),
      destination_ip: randomIp(),
      source_asset: choice(assetNames),
      destination_asset: choice(assetNames),
      protocol: choice(PROTOCOLS),
      bytes: randInt(64, 1_500_000),
      packets: randInt(1, 2000),
      duration_sec: Number((rand() * 30).toFixed(3)),
      raw_score: Number(rawScore.toFixed(4)),
      calibrated_score: Number(calibratedScore.toFixed(4)),
      is_anomaly: isAnomaly,
      tripwire_fired: tripwireFired,
      confidence: Number((0.5 + rand() * 0.5).toFixed(3)),
      replay_session_id: REPLAY_SESSION_ID,
      batch_index: batchIndex,
    },
  };
}

const ALERT_TITLES_TRIPWIRE = "Honeytoken credential used";
const ALERT_TITLES_VOLUMETRIC = "Volumetric anomaly";

function buildAlert(originAsset, isTripwire, snapshotId) {
  const id = nextAlertId++;
  const now = new Date().toISOString();
  const severity = isTripwire || rand() < 0.85 ? "critical" : "warning";
  const title = isTripwire ? ALERT_TITLES_TRIPWIRE : ALERT_TITLES_VOLUMETRIC;
  const detail = isTripwire
    ? `A honeytoken credential was used in traffic involving ${originAsset}. ` +
      `Honeytokens have no legitimate use anywhere in the system, so this is ` +
      `unambiguous compromise, not a statistical inference.`
    : `Volumetric anomaly on ${originAsset} (calibrated score ${(
        0.7 + rand() * 0.29
      ).toFixed(3)}). Unsupervised channel — see docs/DETECTION_STUDY.md for its measured precision.`;

  return {
    type: ENVELOPE_ALERT,
    data: {
      id,
      ts: now,
      severity,
      asset: originAsset,
      title,
      detail,
      explanation: { top_feature: "bytes", mock: true },
      cii_snapshot_id: snapshotId,
      acknowledged: false,
    },
  };
}

function buildCii(originAsset, assetNames, triggerEventId) {
  const snapshotId = nextSnapshotId++;
  const impactedCount = randInt(1, Math.min(5, assetNames.length));
  const impacted = [];
  const pool = assetNames.filter((n) => n !== originAsset);
  for (let i = 0; i < impactedCount && pool.length > 0; i++) {
    impacted.push(pool[randInt(0, pool.length - 1)]);
  }
  const median = Number((rand() * 40 + 10).toFixed(2));
  return {
    snapshotId,
    envelope: {
      type: ENVELOPE_CII,
      data: {
        snapshot_id: snapshotId,
        origin_asset: originAsset,
        cii_median: median,
        cii_p5: Number((median * 0.6).toFixed(2)),
        cii_p95: Number((median * 1.4).toFixed(2)),
        impacted: { assets: impacted, count: impacted.length },
        trigger_event_id: triggerEventId,
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

async function main() {
  const assetNames = await loadAssetNames();

  const wss = new WebSocketServer({ port: PORT, path: PATH });
  const clients = new Set();

  wss.on("connection", (ws, req) => {
    clients.add(ws);
    console.log(
      `[mock-ws] client connected (${clients.size} total) from ${req.socket.remoteAddress}`
    );
    ws.on("close", () => {
      clients.delete(ws);
      console.log(`[mock-ws] client disconnected (${clients.size} total)`);
    });
    ws.on("error", (err) => {
      console.warn(`[mock-ws] client socket error: ${err.message}`);
    });
  });

  function broadcast(envelope) {
    const payload = JSON.stringify(envelope);
    for (const client of clients) {
      if (client.readyState === client.OPEN) {
        client.send(payload);
      }
    }
  }

  const intervalMs = Math.max(1, Math.round(1000 / RATE_PER_SEC));
  const timer = setInterval(() => {
    batchIndex += 1;
    // D4-2: ~2% anomaly rate.
    const isAnomaly = rand() < 0.02;
    const event = buildEvent(assetNames, isAnomaly);
    broadcast(event);

    if (!isAnomaly) return;

    // Alert policy P5-15 mirrored: alerts are rare, mostly tripwire
    // (critical). Roughly 1 in 4 anomalies produces an alert at all,
    // matching "volumetric anomalies suppressed by default".
    if (rand() < 0.25) {
      const isTripwire = rand() < 0.7;
      const originAsset = event.data.source_asset;
      const { snapshotId, envelope: ciiEnvelope } = buildCii(
        originAsset,
        assetNames,
        event.data.id
      );
      const alert = buildAlert(originAsset, isTripwire, snapshotId);
      broadcast(alert);
      // A cii envelope follows an alert, as it does in the real pipeline.
      broadcast(ciiEnvelope);
    }
  }, intervalMs);

  wss.on("listening", () => {
    console.log("");
    console.log("========================================================");
    console.log(" AEGIS mock WebSocket server (Ticket #4)");
    console.log(`  path:  ${PATH}`);
    console.log(`  port:  ${PORT}`);
    console.log(`  rate:  ~${RATE_PER_SEC} events/sec`);
    console.log(`  seed:  ${SEED}`);
    console.log(`  assets: ${assetNames.length} names loaded`);
    console.log(`  url:   ws://127.0.0.1:${PORT}${PATH}`);
    console.log("========================================================");
    console.log("");
  });

  function shutdown() {
    console.log("\n[mock-ws] shutting down...");
    clearInterval(timer);
    for (const client of clients) {
      client.close(1001, "server shutting down");
    }
    wss.close(() => {
      console.log("[mock-ws] closed.");
      process.exit(0);
    });
    // Force-exit if close hangs (e.g. a client refuses to close).
    setTimeout(() => process.exit(0), 2000).unref();
  }

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((err) => {
  console.error("[mock-ws] fatal error:", err);
  process.exit(1);
});
