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

import type { HealthResponse, TopologyResponse } from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

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

export { API_BASE_URL };
