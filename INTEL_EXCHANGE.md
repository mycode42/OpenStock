# Market Intel Exchange Protocol

## Overview
Lightweight research-sharing protocol between two swarm-trader instances (e.g., one for swing trading on machine A, one for day trading on machine B).
Share signals, not decisions. Each instance trades independently.

## Principles
1. **Research flows, decisions don't.** Share what you see, not what you're doing.
2. **No position data.** Never share portfolio holdings, sizes, or P&L.
3. **Async.** Intel drops happen on schedule, not blocking trade execution.
4. **Structured.** JSON format so the receiving instance can parse and integrate.

## Intel Packet Format
```json
{
  "from": "instance-a",
  "timestamp": "ISO-8601",
  "type": "daily-brief|anomaly|sector-signal|earnings-alert",
  "tickers_watching": ["NVDA", "AVGO", "TSM"],
  "signals": [
    {
      "ticker": "NVDA",
      "signal": "bullish|bearish|neutral",
      "confidence": 0.85,
      "reason": "One-line thesis",
      "catalyst": "earnings|insider|momentum|macro|technical"
    }
  ],
  "macro": {
    "sentiment": "risk-on|risk-off|mixed",
    "key_events": ["Fed minutes Wednesday", "CPI Thursday"],
    "sector_rotation": "into tech, out of energy"
  },
  "anomalies": [
    "Unusual volume on SMCI (3x avg)",
    "SEC 13F filing: Bridgewater added PLTR"
  ]
}
```

## Exchange Schedule
- **Morning brief:** After each instance's morning analysis, share intel packet
- **Anomaly alerts:** Real-time when something unusual surfaces
- **Evening debrief:** End-of-day signals and macro read

## Boundaries (hard rules)
- ❌ No sharing: positions, quantities, P&L, account equity, trade orders
- ❌ No copying: receiving instance must form independent thesis
- ❌ No instructions: "you should buy X" is forbidden
- ✅ OK to share: tickers of interest, directional signals, macro reads, anomalies, research

## Setup

Configure `PEER_A2A_URL` and `PEER_A2A_TOKEN` in your `.env` to point at the peer instance's A2A endpoint. Set `SWARM_AGENT_NAME` to identify this instance in outgoing packets.

This is entirely optional — swarm-trader works standalone without any peer sharing configured.
