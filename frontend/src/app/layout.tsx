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
      className={`${inter.variable} ${jetbrainsMono.variable} h-full`}
    >
      <body className="min-h-full flex flex-col antialiased">
        <ConnectionProvider>
          <StreamProvider>{children}</StreamProvider>
        </ConnectionProvider>
      </body>
    </html>
  );
}
