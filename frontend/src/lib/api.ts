/**
 * src/lib/api.ts — thin typed fetch wrapper for the AEGIS backend
 * (Phase 5 Ticket #8's nine REST routes, `backend/routes.py`).
 *
 * Base URL comes from `NEXT_PUBLIC_API_BASE_URL`, defaulting to
 * `http://127.0.0.1:8000` (see `frontend/.env.local.example`).
 *
 * Every non-2xx response throws `ApiError`, which carries the HTTP status
 * code. Panels must branch on `status`, not flatten every failure into one
 * generic "error" state — Ticket #8 deliberately distinguishes "backend
 * unreachable" (network failure, no status) from e.g. a 404 for an unknown
 * asset on `/api/cii/{asset}`.
 */

import type {
  AlertOut,
  AlertsResponse,
  CiiResponse,
  EventsResponse,
  HealthResponse,
  InjectRequest,
  InjectResponse,
  ReplayStatusResponse,
  ScenariosResponse,
  StatsResponse,
  TopologyResponse,
} from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

/**
 * Phase B improvement pass: sent as `Authorization: Bearer <token>` on
 * every request when set — a no-op header the backend ignores while
 * `AEGIS_API_TOKEN` is unset there too (see that setting's docstring in
 * `backend/config.py`). Read honestly: this is a build-time env var
 * baked into the shipped JS bundle, not a runtime secret — anyone who
 * opens devtools on this page can read it. It stops an unrelated page or
 * opportunistic scanner from finding an open control surface; it is not
 * a defense against someone reading this bundle.
 */
const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN;

/**
 * Thrown for any non-2xx response. `status` is the HTTP status code so
 * callers can distinguish e.g. a 404 (unknown asset) from a 503 (replay
 * engine not ready) from a 500. Network failures (backend unreachable,
 * DNS, CORS) surface as `ApiNetworkError` instead, since there is no HTTP
 * status to attach.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Thrown when the request never reached the server (backend down, CORS
 * failure, DNS failure, etc.) — there is no HTTP status to report. */
export class ApiNetworkError extends Error {
  constructor(cause: unknown) {
    super(
      `AEGIS backend unreachable at ${API_BASE_URL}: ${
        cause instanceof Error ? cause.message : String(cause)
      }`
    );
    this.name = "ApiNetworkError";
  }
}

async function apiFetch<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
        ...init?.headers,
      },
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiNetworkError(cause);
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

/** GET /api/health */
export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/api/health");
}

/** GET /api/topology */
export function getTopology(): Promise<TopologyResponse> {
  return apiFetch<TopologyResponse>("/api/topology");
}

/**
 * GET /api/events?since=&limit= — `since` is an EVENT ID, never a
 * timestamp (see `EventsResponse`'s docstring in `./types`; two separate
 * HIGH-severity backend bugs came from treating it as one). Omitting
 * `since` returns the newest `limit` events; supplying it returns
 * everything after that id, oldest-first, gapless by construction. Used
 * by `useEventStream`'s reconnect handler to backfill events missed
 * during a disconnect (Phase A improvement pass).
 */
export function getEvents(params?: {
  since?: number;
  limit?: number;
}): Promise<EventsResponse> {
  const search = new URLSearchParams();
  if (params?.since !== undefined) search.set("since", String(params.since));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return apiFetch<EventsResponse>(`/api/events${qs ? `?${qs}` : ""}`);
}

/**
 * GET /api/alerts?acknowledged=&limit= (Ticket #15, D15-1).
 *
 * `limit` must always be passed explicitly and bounded by the caller —
 * the backend's own default (`BACKEND_SETTINGS.api_alerts_default_limit`)
 * is generous but this wrapper does not assume it; a console showing
 * "recent alerts" should say what "recent" means.
 */
export function getAlerts(params?: {
  acknowledged?: boolean;
  limit?: number;
}): Promise<AlertsResponse> {
  const search = new URLSearchParams();
  if (params?.acknowledged !== undefined) {
    search.set("acknowledged", String(params.acknowledged));
  }
  if (params?.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  const qs = search.toString();
  return apiFetch<AlertsResponse>(`/api/alerts${qs ? `?${qs}` : ""}`);
}

/**
 * POST /api/alerts/{id}/ack (Ticket #15, D15-3). Idempotent on the
 * backend — acking an already-acked alert returns it unchanged
 * (`acknowledged_at` preserved from the first ack).
 */
export function ackAlert(id: number): Promise<AlertOut> {
  return apiFetch<AlertOut>(`/api/alerts/${id}/ack`, { method: "POST" });
}

/**
 * GET /api/cii/{asset} — on-demand blast radius for the card's expandable
 * section (Ticket #15). 404s (asset not a node in the dependency graph)
 * surface as `ApiError` with `status === 404`; callers must render that
 * as "not in the dependency graph", never as an empty impacted list.
 */
export function getCii(asset: string, anomalyScore?: number): Promise<CiiResponse> {
  const qs = anomalyScore !== undefined ? `?anomaly_score=${anomalyScore}` : "";
  return apiFetch<CiiResponse>(`/api/cii/${encodeURIComponent(asset)}${qs}`);
}

/**
 * GET /api/stats (Ticket #16) — the header counters. 503s (scorer never
 * loaded, no replay engine) surface as `ApiError` with `status === 503`;
 * callers must render that as "no basis to compute" (e.g. `—`), never as
 * zeros — see `StatsResponse`'s docstring in `./types` for why zero and
 * "no basis" are different, real states.
 */
export function getStats(): Promise<StatsResponse> {
  return apiFetch<StatsResponse>("/api/stats");
}

/**
 * POST /api/replay/speed — change the live replay pace. 409 (surfaced as
 * `ApiError` with `status === 409`) when no replay is currently running;
 * callers must not treat that as a generic failure — see
 * `AppHeader`'s speed control for how it's rendered.
 */
export function setReplaySpeed(multiplier: number): Promise<ReplayStatusResponse> {
  return apiFetch<ReplayStatusResponse>("/api/replay/speed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ multiplier }),
  });
}

/**
 * GET /api/inject/scenarios (Ticket #13) — the real-attack scenario
 * registry, so the UI lists scenarios rather than hardcoding them.
 */
export function getInjectScenarios(): Promise<ScenariosResponse> {
  return apiFetch<ScenariosResponse>("/api/inject/scenarios");
}

/**
 * POST /api/inject (Ticket #13) — replay REAL captured attack flows,
 * re-targeted at an operator-chosen curated asset. 409 when no replay is
 * running (the backend refuses to silently queue flows a stopped engine
 * would wipe on the next start); 422 for an unknown scenario or a
 * `target_asset` that isn't a curated asset with a real static IP.
 */
export function injectScenario(body: InjectRequest): Promise<InjectResponse> {
  return apiFetch<InjectResponse>("/api/inject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export { API_BASE_URL };
