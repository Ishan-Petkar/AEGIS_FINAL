/**
 * src/lib/types.ts — TypeScript mirrors of the Ticket #8 Pydantic response
 * models (`backend/schemas.py`). Field names are copied verbatim from that
 * file; do not rename or reshape them here.
 *
 * IMPORTANT — `since` on GET /api/events is an EVENT ID, not a timestamp.
 * Two separate HIGH-severity bugs in Ticket #8 came from treating it as a
 * timestamp. `EventsResponse.has_more` tells the caller whether it is
 * behind and should poll again with an advanced `since` (the highest `id`
 * seen so far) — see the docstring on `EventsResponse` in
 * `backend/schemas.py` for the full rationale. Ticket #10 (live feed)
 * depends on this contract; encode it correctly here even though this
 * ticket does not wire /api/events for real.
 */

// ---------------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: "ok" | "degraded";
  database: boolean;
  scorer_loaded: boolean;
  scorer_load_error: string | null;
  replay_running: boolean;
  uptime_sec: number;
}

// ---------------------------------------------------------------------------
// GET /api/topology
// ---------------------------------------------------------------------------

export interface TopologyNode {
  name: string;
  criticality: number;
  type: string | null;
  purdue_level: number | null;
  is_gateway: boolean;
}

export interface TopologyEdge {
  source: string;
  target: string;
  edge_type: string;
  prob: number;
  is_gateway_edge: boolean;
}

export interface TopologyResponse {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

// ---------------------------------------------------------------------------
// GET /api/events
// ---------------------------------------------------------------------------

export interface EventOut {
  id: number;
  ts: string;
  observed_at: string | null;
  ingested_at: string;
  source_id: string | null;
  destination_id: string | null;
  source_asset: string | null;
  destination_asset: string | null;
  protocol: string | null;
  bytes: number | null;
  packets: number | null;
  duration_sec: number | null;
  signal_type: string;
  source_dataset: string | null;
  timing_provenance: string;
  replay_session_id: string;
  source_row_id: string;
  raw: Record<string, unknown> | null;
}

export interface EventsResponse {
  events: EventOut[];
  /**
   * `true` when the query matched more rows than `limit` allowed through —
   * the caller is behind and should poll again with an advanced `since`
   * (an event id, never a timestamp). Never infer this from
   * `events.length === limit`.
   */
  has_more: boolean;
}

// ---------------------------------------------------------------------------
// GET /api/alerts, POST /api/alerts/{id}/ack
// ---------------------------------------------------------------------------

export interface AlertOut {
  id: number;
  ts: string;
  severity: string;
  asset: string;
  title: string;
  detail: string | null;
  explanation: Record<string, unknown> | null;
  cii_snapshot_id: number | null;
  acknowledged: boolean;
  acknowledged_at: string | null;
}

export interface AlertsResponse {
  alerts: AlertOut[];
}

// ---------------------------------------------------------------------------
// GET /api/cii/{asset}
// ---------------------------------------------------------------------------

export interface CiiResponse {
  origin_asset: string;
  anomaly_score: number;
  cii_median: number;
  cii_p5: number;
  cii_p95: number;
  impacted_assets: string[];
  hop_details: Record<string, Record<string, number>>;
}

// ---------------------------------------------------------------------------
// POST /api/replay/start | stop | speed
// ---------------------------------------------------------------------------

export interface ReplayStartRequest {
  dataset?: string | null;
  speed?: number | null;
  start_at?: string | null;
  limit?: number | null;
}

export interface ReplaySpeedRequest {
  multiplier: number;
}

export interface ReplayStatusResponse {
  running: boolean;
  day: string | null;
  speed: number | null;
  replay_session_id: string | null;
  emitted_count: number;
  total_for_day: number;
  current_virtual_position: string | null;
  lag_seconds: number;
  batches_emitted: number;
  consumer_error_count: number;
  consumer_failed_flow_count: number;
}

// ---------------------------------------------------------------------------
// WS /ws/stream envelopes (Ticket #4/#9) — field names copied verbatim from
// `backend/ingest.py`'s `_broadcast_batch` (event) and `_handle_anomalies`
// (alert, cii). `ENVELOPE_EVENT`/`ENVELOPE_ALERT`/`ENVELOPE_CII` there are
// the literal strings "event" | "alert" | "cii" used as the `type`
// discriminant below. Do not rename or reshape these fields — Ticket #12
// swaps the mock for the real endpoint and depends on exact parity.
// ---------------------------------------------------------------------------

export interface EventEnvelopeData {
  id: number;
  ts: string;
  observed_at: string;
  source_ip: string;
  destination_ip: string;
  source_asset: string;
  destination_asset: string;
  protocol: string;
  bytes: number;
  packets: number;
  duration_sec: number;
  raw_score: number;
  calibrated_score: number;
  is_anomaly: boolean;
  tripwire_fired: boolean;
  confidence: number;
  replay_session_id: string;
  batch_index: number;
}

export interface AlertEnvelopeData {
  id: number;
  ts: string;
  severity: string;
  asset: string;
  title: string;
  detail: string | null;
  explanation: Record<string, unknown> | null;
  cii_snapshot_id: number | null;
  acknowledged: boolean;
}

export interface CiiEnvelopeData {
  snapshot_id: number | null;
  origin_asset: string;
  cii_median: number;
  cii_p5: number;
  cii_p95: number;
  impacted: unknown;
  trigger_event_id: number | null;
}

export interface EventEnvelope {
  type: "event";
  data: EventEnvelopeData;
}

export interface AlertEnvelope {
  type: "alert";
  data: AlertEnvelopeData;
}

export interface CiiEnvelope {
  type: "cii";
  data: CiiEnvelopeData;
}

export type StreamEnvelope = EventEnvelope | AlertEnvelope | CiiEnvelope;
