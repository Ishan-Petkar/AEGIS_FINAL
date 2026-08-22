# DESIGN.md — AEGIS Cyber Intelligence Platform

## Visual Theme
Dark, high-contrast, elite cyber operations center. Inspired by CrowdStrike Falcon, Splunk Dark Mode, and SentinelOne consoles. Minimal chrome, maximum data density. Feels like a military-grade threat operations center, not a SaaS dashboard. No rounded corners on primary containers. Sharp, angular, precise.

## Color Palette
- **Background (Deep Space)** #080c14 (not pure black, slight blue undertone)
- **Surface (Command Center)** #0f1724 (dark blue-gray)
- **Surface Elevated** #162032
- **Border Default** #1e2d45
- **Border Accent** #2a4a7f
- **Text Primary** #e8edf5
- **Text Secondary** #8899b4
- **Text Muted** #5a6e8a
- **Accent Primary** #00d4ff (electric cyan — use sparingly for critical elements only)
- **Accent Primary Hover** #33ddff
- **Accent Danger** #ff3355 (threat indicators, critical alerts)
- **Accent Danger Hover** #ff5577
- **Accent Warning** #f59e0b (amber — financial alerts, medium severity)
- **Accent Warning Hover** #f7b32b
- **Accent Success** #0aff8e (operational normal)
- **Accent Financial** #d4a843 (gold — financial nodes only)

## Typography
- **Primary Font**: Inter (weights: 400 regular, 500 medium, 600 semibold, 700 bold)
- **Monospace Font**: JetBrains Mono (weights: 400, 600) — for metrics, IPs, technical data
- **Heading Scale**: H1 28px/700, H2 22px/600, H3 18px/600
- **Body**: 15px/1.5 Inter Regular
- **Small/Caption**: 12px/1.4 Inter Regular, uppercase tracking 0.5px
- **All headings use letter-spacing: -0.02em**

## Spacing System
- Use 8px baseline grid: 8, 16, 24, 32, 48, 64, 96
- Section padding: 24px
- Card padding: 20px
- Gap between grid items: 16px
- No arbitrary pixel values — only 8px multiples

## Component Patterns
- **Cards**: bg Surface, 1px solid Border Default, 2px border-radius (sharp, not rounded), subtle inner shadow
- **Buttons**: 2px border-radius, font Inter Medium 14px, uppercase tracking 0.5px. Primary: Accent Primary bg, dark text. Danger: Accent Danger bg, white text. All have hover states with +10% lightness
- **Metric Displays**: JetBrains Mono, bold, large size (24px+), with subtle text-shadow glow matching the metric color
- **Data Tables**: Striped rows (alternating Surface/Surface Elevated), 1px Border Default between rows, header row uppercase 12px
- **Alerts/Callouts**: 2px left border accent (Danger red for critical, Warning amber for medium), bg slightly lighter than Surface
- **Badges**: 2px border-radius, 10px horizontal padding, uppercase 11px, tight tracking
- **All interactive elements**: Must have visible hover state, focus-visible ring (2px Accent Primary), and cursor:pointer

## Iconography
- Use simple geometric indicators (circles, diamonds, triangles) for status
- No emoji icons in production UI (use SVG or Unicode geometric shapes)
- Status dot colors: Green #0aff8e, Yellow #f59e0b, Red #ff3355

## Motion
- Transitions: 150ms ease-out (fast, professional, no bouncy animations)
- Hover lifts: transform translateY(-1px) with subtle shadow increase
- No fade-in on page load (instant render)
- Alert pulses: Subtle 2s infinite ease-in-out opacity pulse on critical threat indicators
