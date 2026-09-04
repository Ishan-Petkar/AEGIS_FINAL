/**
 * src/lib/monitoring-translator.ts — Non-technical translation and sanitization
 * layer for municipal city operators (AEGIS Monitoring View).
 *
 * Translates technical cybersecurity/ML concepts (raw IPs, Isolation Forest scores,
 * Purdue levels, honeypots, sigma deviations) into human-readable, municipal-grade
 * status descriptions, consolidated city services, and intuitive status labels.
 */

export interface MonitoringSectorDef {
  id: string;
  name: string;
  backendSector: string;
}

export const MONITORING_SECTORS: MonitoringSectorDef[] = [
  { id: "energy", name: "Energy", backendSector: "energy" },
  { id: "water", name: "Water", backendSector: "water" },
  { id: "transport", name: "Transport", backendSector: "transport" },
  { id: "public_safety", name: "Public Safety", backendSector: "public_safety" },
  { id: "health", name: "Health", backendSector: "health" },
  { id: "telecom", name: "Telecom/IT", backendSector: "telecom" },
  { id: "finance", name: "Finance", backendSector: "finance" },
  { id: "civic", name: "Civic", backendSector: "civic" },
  { id: "environment", name: "Environment", backendSector: "environment" },
  { id: "monitoring", name: "Monitoring", backendSector: "monitoring" },
  { id: "infrastructure", name: "Infrastructure", backendSector: "core" },
];

/**
 * Maps a backend sector identifier (e.g. 'public_safety', 'civic', 'finance')
 * to its human-friendly municipal sector name.
 */
export function backendSectorToServiceName(sector: string | null | undefined): string {
  if (!sector) return "Infrastructure";
  const s = sector.toLowerCase();
  for (const item of MONITORING_SECTORS) {
    if (item.backendSector === s || item.id === s) {
      return item.name;
    }
  }
  if (s === "operations") return "Operations Center";
  return "Infrastructure";
}

/**
 * Translates technical alert titles (like HONEYTOKEN trips or sigma deviations)
 * into plain English municipal descriptions.
 */
export function translateAlertTitle(title: string | null | undefined): string {
  if (!title) return "Suspicious operational activity detected";
  const lower = title.toLowerCase();

  if (lower.includes("honeytoken") || lower.includes("tripwire")) {
    return "Unauthorized credential access detected";
  }
  if (lower.includes("sigma above baseline") || lower.includes("duration")) {
    return "Unusual activity duration observed";
  }
  if (lower.includes("beaconing") || lower.includes("c2") || lower.includes("command and control")) {
    return "Suspicious periodic communication pattern";
  }
  if (lower.includes("burst") || lower.includes("flow rate") || lower.includes("volume")) {
    return "Sudden spike in network traffic volume";
  }
  if (lower.includes("brute") || lower.includes("failed login") || lower.includes("auth")) {
    return "Multiple unauthorized access attempts";
  }
  if (lower.includes("scan") || lower.includes("sweep") || lower.includes("discovery")) {
    return "Unauthorized system scanning activity";
  }
  if (lower.includes("exfiltration") || lower.includes("leak") || lower.includes("transfer")) {
    return "Unusual outbound data transfer";
  }
  if (lower.includes("anomaly") || lower.includes("anomalous") || lower.includes("isolation")) {
    return "Abnormal system activity pattern";
  }

  // If already user-readable, clean technical tokens
  const sanitized = title
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "")
    .replace(/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g, "external node")
    .replace(/_+/g, " ")
    .trim();

  return sanitized || "Suspicious operational activity detected";
}

/**
 * Humanizes asset identifiers by removing internal underscores,
 * obfuscating raw IP addresses, and capitalizing service names.
 * e.g. "City_Payment_Gateway" -> "City Payment Gateway"
 * e.g. "10.0.4.12" or "External_Src_23" -> "External Traffic Source"
 */
export function humanizeAssetName(raw: string | null | undefined): string {
  if (!raw) return "Central Network";

  // Check for raw IP address
  if (/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(raw.trim())) {
    return "External Traffic Source";
  }

  if (raw.startsWith("External_") || raw.startsWith("EXT_")) {
    return "External Source";
  }

  // Handle common smart city assets cleanly
  let cleaned = raw
    .replace(/_+/g, " ")
    .replace(/RTU/g, "RTU")
    .replace(/SCADA/g, "SCADA")
    .replace(/CCTV/g, "CCTV")
    .replace(/IoT/g, "IoT")
    .trim();

  // Ensure title case for each word
  cleaned = cleaned
    .split(" ")
    .map((word) => {
      if (["RTU", "SCADA", "CCTV", "IoT", "IT", "HVAC", "GPS"].includes(word.toUpperCase())) {
        return word.toUpperCase();
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");

  return cleaned;
}

/**
 * Maps technical severities ('critical', 'warning', 'normal')
 * to non-technical display labels ('High', 'Medium', 'Low').
 */
export function mapSeverityToLabel(severity: string | null | undefined): "High" | "Medium" | "Low" {
  if (severity === "critical") return "High";
  if (severity === "warning") return "Medium";
  return "Low";
}

export type NonTechnicalAlertStatus = "Open" | "Investigating" | "Resolved";

/**
 * Derives the operational status of an alert.
 * Alerts without acknowledgement default to 'Open' (if critical) or 'Investigating' (if warning/normal).
 * Acknowledged alerts are marked 'Resolved'.
 */
export function deriveAlertStatus(alert: {
  acknowledged: boolean;
  severity: string;
}): NonTechnicalAlertStatus {
  if (alert.acknowledged) return "Resolved";
  if (alert.severity === "critical") return "Open";
  return "Investigating";
}

/**
 * Formats timestamps into a clean local operational time string (e.g. "08:42 PM").
 */
export function formatAlertTime(ts: string | null | undefined): string {
  if (!ts) return "Just now";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "Just now";

  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(d);
}

/**
 * Formats a live header timestamp (e.g. "Sep 4, 2026 · 8:52 PM").
 */
export function formatHeaderTime(date: Date): string {
  const dateStr = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);

  const timeStr = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(date);

  return `${dateStr} · ${timeStr}`;
}
