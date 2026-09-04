import { ClientViewSwitch } from "@/components/ClientViewSwitch";

// AEGIS Dual-Persona Operations Console:
// Switches dynamically between the Non-Technical Monitoring Dashboard (City Operations Center)
// and the Technical Console (Cybersecurity Engineers & Judges) via ClientViewSwitch.
// Both views share the same underlying WebSocket stream, topology, and live connection providers.
export default function Home() {
  return <ClientViewSwitch />;
}
