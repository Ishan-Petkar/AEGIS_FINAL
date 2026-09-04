/**
 * lib/asset-icons.ts — Hand-authored 24x24 SVG paths for graph nodes.
 *
 * Phase 3 — Node Icons.
 */

// 16-glyph map of 24x24 stroke-only SVG paths.
export const ICON_PATHS: Record<string, string> = {
  sensor: "M5 8h14M5 16h14M12 4v16M8 4v16M16 4v16M4 4h16v16H4z", // Grid/sensor
  database: "M12 6c-4.418 0-8 1.343-8 3s3.582 3 8 3 8-1.343 8-3-3.582-3-8-3z M4 9v6c0 1.657 3.582 3 8 3s8-1.343 8-3V9", // Standard cylinder
  plant: "M12 22V8M12 8L8 12M12 8l4 4M6 22V12l-3 3M18 22V12l3 3", // Plant/factory
  controller: "M4 6h16v12H4z M8 12h2M14 12h2M12 10v4", // Logic controller
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z", // Gateway/Shield
  bank: "M4 10h16M2 20h20M12 2L2 10h20L12 2z M6 10v10M10 10v10M14 10v10M18 10v10", // Bank/financial
  globe: "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M2.5 12h19M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z", // Globe/External
  mesh: "M 6 6 H 18 V 18 H 6 Z M 6 12 H 18 M 12 6 V 18 M 3 3 L 6 6 M 21 3 L 18 6 M 3 21 L 6 18 M 21 21 L 18 18", // Mesh/Network
  router: "M4 6h16v12H4z M8 12h2M14 12h2M4 12h2", // Router
  building: "M6 22V2h12v20 M10 6h4M10 10h4M10 14h4M10 18h4", // Building
  cross: "M12 2v20M2 12h20", // Cross/Health/General
  ballot: "M6 4h12v16H6z M10 8h4M10 12h4M10 16h4", // Document/Form
  exchange: "M4 8h12l-4-4M20 16H8l4 4", // Exchange/Arrows
  gateway: "M4 12h16M16 8l4 4-4 4M8 4v16", // Chokepoint
  layers: "M 12 2 L 2 7 L 12 12 L 22 7 Z M 2 12 L 12 17 L 22 12 M 2 17 L 12 22 L 22 17", // Layers/Aggregate
  subnet: "M12 2v20M4 6h16M4 18h16", // Subnet
};

// Lazy initialization cache for Path2D objects
const pathCache = new Map<string, Path2D>();

export function iconPath2D(key: string): Path2D | null {
  if (typeof Path2D === "undefined") return null; // SSR safety
  if (!pathCache.has(key)) {
    const d = ICON_PATHS[key];
    if (d) {
      pathCache.set(key, new Path2D(d));
    }
  }
  return pathCache.get(key) ?? null;
}

export function drawIcon(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
  key: string,
  color: string
) {
  const p = iconPath2D(key);
  if (!p) return;
  ctx.save();
  ctx.translate(x, y);
  // Scale from the 24x24 authored box (centered at 12,12) down to the radius r
  const scale = (r * 0.85) / 12; // 0.85 tunes the glyph to perfectly inscribe inside the ring
  ctx.scale(scale, scale);
  ctx.translate(-12, -12); // Authored center is 12,12
  
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5 / scale;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke(p);
  ctx.restore();
}

const FINANCIAL_TYPES = new Set([
  "Payment_Gateway",
  "Interbank_Network",
  "Social_Welfare_System",
  "Tax_Collection",
  "Bank_API",
]);

export function iconKeyFor(node: { type: string | null; isGateway: boolean; isAggregate: boolean; id: string }): string {
  if (node.isAggregate) return "layers";
  if (node.isGateway) return "shield";
  if (node.id === "External_Network" || node.id === "__external__") return "globe";
  if (node.id === "City_Operations_Center") return "mesh";

  if (node.id === "Social_Welfare_System") return "building";
  if (node.id === "Municipal_Bond_Platform") return "bank";
  if (node.id === "Payroll_System") return "bank";
  if (node.id === "Tax_Collection_System") return "bank";
  if (node.id === "City_Payment_Gateway") return "bank";
  if (node.id === "Bank_Partner_API") return "bank";

  if (node.type && FINANCIAL_TYPES.has(node.type)) return "bank";
  if (node.type === "SCADA_Controller" || node.type === "PLC") return "controller";
  if (node.type === "Pump_Station" || node.type === "Substation" || node.type === "Treatment_Plant") return "plant";
  if (node.type === "Sensor_Array" || node.type === "Smart_Meter") return "sensor";
  if (node.type === "Citizen_Portal") return "building";
  
  // Database / standard IT
  if (node.type?.includes("DB") || node.type?.includes("Database")) return "database";
  
  return "cross"; // Fallback
}
