import { MARKET_DEFINITIONS } from "../data/marketDefinitions";
import { buildDemoSnapshot } from "../data/demoSnapshot";
import {
  buildRiskTradePlan,
  calculatePositionSize,
  rankedLegKey,
  resolveRankedLeg,
  snapshotRiskDefaults,
} from "./risk";
import type { MarketSnapshot } from "./types";

function niftySnapshot(): MarketSnapshot {
  return buildDemoSnapshot({
    market: "NIFTY",
    symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
    expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
  });
}

describe("position sizing", () => {
  it("includes round-trip charges in both risk and allocation limits", () => {
    const result = calculatePositionSize({
      capital: 100_000,
      riskPercent: 1,
      allocationPercent: 10,
      entryAsk: 100,
      stop: 90,
      lotSize: 50,
      charges: {
        fixedPerOrder: 10,
        entryNotionalRate: 0,
        exitNotionalRate: 0,
      },
    });

    expect(result.valid).toBe(true);
    if (!result.valid) return;
    expect(result.maximumRisk).toBe(1_000);
    expect(result.maximumAllocation).toBe(10_000);
    expect(result.oneLot).toMatchObject({
      quantity: 50,
      entryPremium: 5_000,
      estimatedCharges: 20,
      capitalRequired: 5_020,
      estimatedRisk: 520,
    });
    expect(result.lotsByRisk).toBe(1);
    expect(result.lotsByAllocation).toBe(1);
    expect(result.recommendedLots).toBe(1);
    expect(result.recommended.estimatedRisk).toBeLessThanOrEqual(
      result.maximumRisk,
    );
    expect(result.recommended.capitalRequired).toBeLessThanOrEqual(
      result.maximumAllocation,
    );
  });

  it("enforces the two-percent cap and validates a long-option stop", () => {
    const excessiveRisk = calculatePositionSize({
      capital: 100_000,
      riskPercent: 2.01,
      allocationPercent: 25,
      entryAsk: 100,
      stop: 80,
      lotSize: 50,
    });
    const invalidStop = calculatePositionSize({
      capital: 100_000,
      riskPercent: 2,
      allocationPercent: 25,
      entryAsk: 100,
      stop: 100,
      lotSize: 50,
    });

    expect(excessiveRisk.valid).toBe(false);
    expect(excessiveRisk.errors).toContain(
      "Risk per trade cannot exceed the 2.00% hard cap.",
    );
    expect(invalidStop.valid).toBe(false);
    expect(invalidStop.errors).toContain(
      "A long-option stop must be below the ask entry price.",
    );
  });

  it("returns an exact zero-lot affordability explanation", () => {
    const result = calculatePositionSize({
      capital: 10_000,
      riskPercent: 1,
      allocationPercent: 10,
      entryAsk: 100,
      stop: 90,
      lotSize: 50,
      charges: {
        fixedPerOrder: 10,
        entryNotionalRate: 0,
        exitNotionalRate: 0,
      },
    });

    expect(result.valid).toBe(true);
    if (!result.valid) return;
    expect(result.recommendedLots).toBe(0);
    expect(result.affordabilityMessage).toBe(
      [
        "0 lots: one lot requires INR 5,020.00 allocation",
        "and INR 520.00 risk capacity; available INR 1,000.00 allocation",
        "and INR 100.00 risk.",
      ].join(" "),
    );
  });

  it("calculates one, two, and three-R premium targets from ask and stop", () => {
    const result = calculatePositionSize({
      capital: 500_000,
      riskPercent: 1,
      allocationPercent: 25,
      entryAsk: 100,
      stop: 80,
      lotSize: 10,
      charges: {
        fixedPerOrder: 0,
        entryNotionalRate: 0,
        exitNotionalRate: 0,
      },
    });

    expect(result.valid).toBe(true);
    if (!result.valid) return;
    expect(result.riskPerUnit).toBe(20);
    expect(result.targets).toEqual([120, 140, 160]);
  });
});

describe("risk trade-plan gates", () => {
  it("uses the selected ranked leg ask and verified lot size", () => {
    const snapshot = niftySnapshot();
    const selectedKey = rankedLegKey(snapshot.ranking[0]);
    const selected = resolveRankedLeg(snapshot, selectedKey);
    const defaults = snapshotRiskDefaults(snapshot, selectedKey);
    const evaluation = buildRiskTradePlan(snapshot, selectedKey, defaults);

    expect(selected).not.toBeNull();
    expect(evaluation.sizing?.valid).toBe(true);
    if (evaluation.sizing?.valid !== true || selected === null) return;
    expect(evaluation.sizing.request.entryAsk).toBe(selected.ranking.askEntry);
    expect(evaluation.sizing.request.lotSize).toBe(snapshot.definition.lotSize);
    expect(evaluation.blockers).toContain(
      "Live market data is required; current data mode is DEMO.",
    );
    expect(evaluation.actionable).toBe(false);
  });

  it(
    "becomes actionable only when live data, event, decision, and leg gates are clear",
    () => {
      const demo = niftySnapshot();
      const selected = demo.ranking[0];
      const selectedKey = rankedLegKey(selected);
      const live: MarketSnapshot = {
        ...demo,
        dataMode: "LIVE",
        analytics: {
          ...demo.analytics,
          decision: selected.side === "CE" ? "BUY CALL" : "BUY PUT",
        },
      };
      const defaults = snapshotRiskDefaults(live, selectedKey);
      const ready = buildRiskTradePlan(live, selectedKey, defaults);
      const stale = buildRiskTradePlan(
        { ...live, dataMode: "STALE" },
        selectedKey,
        defaults,
      );
      const conflicting = buildRiskTradePlan(
        {
          ...live,
          analytics: { ...live.analytics, decision: "WAIT" },
        },
        selectedKey,
        defaults,
      );
      const eventSnapshot = buildDemoSnapshot(live.selection, {
        event_risk: "ACTIVE",
      });
      const eventBlocked = buildRiskTradePlan(
        {
          ...eventSnapshot,
          dataMode: "LIVE",
          analytics: live.analytics,
        },
        selectedKey,
        defaults,
      );

      expect(ready.actionable).toBe(true);
      expect(ready.blockers).toEqual([]);
      expect(stale.actionable).toBe(false);
      expect(stale.blockers.join(" ")).toMatch(/current data mode is STALE/);
      expect(conflicting.actionable).toBe(false);
      expect(conflicting.blockers.join(" ")).toMatch(/does not authorize/);
      expect(eventBlocked.actionable).toBe(false);
      expect(eventBlocked.blockers.join(" ")).toMatch(
        /Event risk must be explicitly CLEAR/,
      );
    },
  );

  it("keeps rejected legs and unknown selections non-actionable", () => {
    const demo = niftySnapshot();
    const rejected = [...demo.ranking]
      .reverse()
      .find((entry) => entry.rejectionReasons.length > 0);
    expect(rejected).toBeDefined();
    if (rejected === undefined) return;
    const selectedKey = rankedLegKey(rejected);
    const live: MarketSnapshot = {
      ...demo,
      dataMode: "LIVE",
      analytics: {
        ...demo.analytics,
        decision: rejected.side === "CE" ? "BUY CALL" : "BUY PUT",
      },
    };
    const defaults = snapshotRiskDefaults(live, selectedKey);

    const rejectedPlan = buildRiskTradePlan(live, selectedKey, defaults);
    const missingPlan = buildRiskTradePlan(live, "missing:CE", defaults);

    expect(rejectedPlan.actionable).toBe(false);
    expect(rejectedPlan.blockers.join(" ")).toMatch(/Selected contract is rejected/);
    expect(missingPlan.actionable).toBe(false);
    expect(missingPlan.blockers).toEqual([
      "Select a ranked contract before sizing a position.",
    ]);
  });
});
