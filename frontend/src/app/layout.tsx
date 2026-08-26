import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { ConnectionProvider } from "@/lib/connection-context";
import { StreamProvider } from "@/lib/stream-context";
import "./globals.css";

// DESIGN_CONSOLE.md §3: Inter for UI/body, JetBrains Mono for numerics,
// IPs, timestamps, IDs. Loaded from Google Fonts (the only font host the
// CSP admits) with real fallback stacks defined in globals.css.
const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
  weight: ["400", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "AEGIS Operations Console",
  description:
    "Cyber-physical risk detection and cascading blast-radius console for smart city infrastructure.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrainsMono.variable} xl:h-full`}
    >
      {/* The fixed-viewport clamp (`h-full` + `overflow-hidden`) only
          applies at the `xl` breakpoint, matching page.tsx's switch to
          the 3-column grid (`xl:flex-row`). That clamp is what gives the
          graph canvas a definite height to bottom out on and is what
          killed the runaway ResizeObserver feedback loop (see
          CityGraph.tsx) — but it clamps the *whole document*, so below
          `xl` (where panels stack) it must not apply: otherwise content
          below the fold is clipped with no scrollbar to reach it. Below
          `xl` the document scrolls normally and each stacked panel gets
          its own definite height instead (see page.tsx / GraphPanel.tsx). */}
      <body className="flex flex-col antialiased xl:h-full xl:overflow-hidden">
        <ConnectionProvider>
          <StreamProvider>{children}</StreamProvider>
        </ConnectionProvider>
      </body>
    </html>
  );
}
