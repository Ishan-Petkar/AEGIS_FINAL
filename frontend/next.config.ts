import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next.js 16 blocks cross-origin requests to dev-only assets/endpoints
  // by default, allowing only the exact hostname the dev server was
  // started with ("localhost"). This project's own convention uses
  // 127.0.0.1 everywhere (AEGIS_API_HOST, NEXT_PUBLIC_API_BASE_URL,
  // docs/SETUP.md's own quick-start commands) — without this, opening the
  // console at http://127.0.0.1:3000 instead of http://localhost:3000
  // silently fails every _next/static chunk and the HMR websocket with a
  // 403, which reads as the console being permanently stuck on
  // "Connecting" with no visible error explaining why. See
  // https://nextjs.org/docs/app/api-reference/config/next-config-js/allowedDevOrigins
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;
