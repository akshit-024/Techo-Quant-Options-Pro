import { buildDemoSnapshot } from "../data/demoSnapshot";
import { MARKET_DEFINITIONS } from "../data/marketDefinitions";
import { evaluateOperationalGate } from "./gate";

const selection = {
  market: "NIFTY" as const,
  symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
  expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
};

describe("operational data gate", () => {
  it("never allows a demo snapshot to become an operational signal", () => {
    const gate = evaluateOperationalGate(buildDemoSnapshot(selection), {
      connectionState: "CONNECTED",
      now: new Date("2026-08-21T06:12:20Z"),
    });
    expect(gate).toMatchObject({
      code: "DEMO_DATA",
      decision: "NO TRADE",
      signalingAllowed: false,
    });
  });

  it("blocks stale live data even when its analytical score says buy", () => {
    const snapshot = {
      ...buildDemoSnapshot(selection),
      dataMode: "LIVE" as const,
      capturedAt: "2026-08-21T06:12:08Z",
    };
    const gate = evaluateOperationalGate(snapshot, {
      connectionState: "CONNECTED",
      now: new Date("2026-08-21T06:13:00Z"),
      staleAfterSeconds: 30,
    });
    expect(gate).toMatchObject({ code: "STALE_DATA", decision: "NO TRADE", signalingAllowed: false });
  });

  it("uses insufficient data when any of the five strike rows is missing", () => {
    const snapshot = buildDemoSnapshot(selection);
    const gate = evaluateOperationalGate({ ...snapshot, chain: snapshot.chain.slice(0, 4) });
    expect(gate).toMatchObject({
      code: "MISSING_STRIKES",
      decision: "INSUFFICIENT DATA",
      signalingAllowed: false,
    });
  });

  it("blocks an expired contract and an active event-risk override", () => {
    const baseline = buildDemoSnapshot(selection);
    const expired = evaluateOperationalGate({
      ...baseline,
      selection: { ...baseline.selection, expiry: "2026-08-20" },
    });
    expect(expired.code).toBe("INVALID_EXPIRY");

    const eventRisk = evaluateOperationalGate(buildDemoSnapshot(selection, { event_risk: "ACTIVE" }));
    expect(eventRisk).toMatchObject({ code: "EVENT_RISK", decision: "NO TRADE" });
  });

  it("allows only a fresh connected live snapshot with an actionable result", () => {
    const snapshot = {
      ...buildDemoSnapshot(selection),
      dataMode: "LIVE" as const,
      capturedAt: "2026-08-21T06:12:08Z",
    };
    const gate = evaluateOperationalGate(snapshot, {
      connectionState: "CONNECTED",
      now: new Date("2026-08-21T06:12:20Z"),
    });
    expect(gate).toMatchObject({ code: "READY", decision: "BUY CALL", signalingAllowed: true });
  });

  it("accepts a backend ISO expiry and preserves the backend actionability lock", () => {
    const snapshot = {
      ...buildDemoSnapshot({
        ...selection,
        expiry: "2026-08-27T15:30:00+05:30",
      }),
      dataMode: "LIVE" as const,
      capturedAt: "2026-08-21T06:12:08Z",
      backendAuthority: {
        snapshotId: "snapshot-live-1",
        contractKey: "contract-live-1",
        source: "DHAN_REST",
        receivedAt: "2026-08-21T06:12:09Z",
        complete: true,
        fresh: true,
        actionable: false,
        blockers: ["OPERATOR_PROFILE_REQUIRED"],
        warnings: [],
        validationAccepted: true,
      },
    };

    const gate = evaluateOperationalGate(snapshot, {
      connectionState: "CONNECTED",
      now: new Date("2026-08-21T06:12:20Z"),
    });

    expect(gate).toMatchObject({
      code: "BACKEND_BLOCKED",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "OPERATOR_PROFILE_REQUIRED",
    });
  });
});
