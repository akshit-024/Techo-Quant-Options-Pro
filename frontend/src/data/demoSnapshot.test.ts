import { MARKET_DEFINITIONS, MARKET_ORDER } from "./marketDefinitions";
import { buildDemoSnapshot } from "./demoSnapshot";

describe("buildDemoSnapshot", () => {
  it.each(MARKET_ORDER)(
    "builds a five-strike, ten-leg deterministic universe for %s",
    (market) => {
      const definition = MARKET_DEFINITIONS[market];
      const snapshot = buildDemoSnapshot({
        market,
        symbol: definition.symbols[0],
        expiry: definition.expiries[0],
      });

      expect(snapshot.chain).toHaveLength(5);
      expect(snapshot.ranking).toHaveLength(10);
      expect(snapshot.selection.market).toBe(market);
      expect(snapshot.selection.symbol).toBe(definition.symbols[0]);
      expect(snapshot.selection.expiry).toBe(definition.expiries[0]);
      expect(
        snapshot.chain.slice(1).every(
          (row, index) =>
            row.strike - snapshot.chain[index].strike ===
            snapshot.definition.strikeStep,
        ),
      ).toBe(true);

      const securityIds = snapshot.chain.flatMap((row) => [
        row.call.securityId,
        row.put.securityId,
      ]);
      expect(new Set(securityIds).size).toBe(10);
    },
  );

  it("retains imported values while exposing manual and effective override values", () => {
    const selection = {
      market: "NIFTY" as const,
      symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
      expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
    };
    const baseline = buildDemoSnapshot(selection);
    const overridden = buildDemoSnapshot(selection, {
      spot: "25000.50",
      event_risk: "ACTIVE",
      atm: "99999",
    });
    const originalSpot = baseline.inputs.find((input) => input.id === "spot");
    const spot = overridden.inputs.find((input) => input.id === "spot");
    const eventRisk = overridden.inputs.find(
      (input) => input.id === "event_risk",
    );
    const computedAtm = overridden.inputs.find((input) => input.id === "atm");

    expect(spot?.importedValue).toBe(originalSpot?.importedValue);
    expect(spot?.manualOverride).toBe("25000.50");
    expect(spot?.effectiveValue).toBe("25000.50");
    expect(eventRisk).toMatchObject({
      importedValue: "CLEAR",
      manualOverride: "ACTIVE",
      effectiveValue: "ACTIVE",
    });
    expect(computedAtm?.manualOverride).toBeUndefined();
    expect(computedAtm?.effectiveValue).not.toBe("99999");
  });

  it("ranks all eligible contracts before rejected contracts and labels best and second", () => {
    const snapshot = buildDemoSnapshot({
      market: "NIFTY",
      symbol: MARKET_DEFINITIONS.NIFTY.symbols[0],
      expiry: MARKET_DEFINITIONS.NIFTY.expiries[0],
    });

    expect(snapshot.ranking.map((entry) => entry.rank)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ]);
    expect(snapshot.ranking[0].rejectionReasons).toHaveLength(0);
    expect(snapshot.ranking[1].rejectionReasons).toHaveLength(0);

    for (let index = 1; index < snapshot.ranking.length; index += 1) {
      const previous = snapshot.ranking[index - 1];
      const current = snapshot.ranking[index];
      const previousRejected = previous.rejectionReasons.length > 0;
      const currentRejected = current.rejectionReasons.length > 0;
      expect(Number(previousRejected)).toBeLessThanOrEqual(Number(currentRejected));
      if (previousRejected === currentRejected) {
        expect(previous.score).toBeGreaterThanOrEqual(current.score);
      }
    }
  });

  it("applies symbol-specific contract profiles without changing the market definition", () => {
    const tcs = buildDemoSnapshot({
      market: "STOCK_FNO",
      symbol: "TCS",
      expiry: MARKET_DEFINITIONS.STOCK_FNO.expiries[1],
    });
    const silver = buildDemoSnapshot({
      market: "MCX",
      symbol: "SILVER",
      expiry: MARKET_DEFINITIONS.MCX.expiries[2],
    });

    expect(tcs.definition).toMatchObject({
      id: "STOCK_FNO",
      strikeStep: 50,
      lotSize: 175,
    });
    expect(tcs.selection.expiry).toBe(MARKET_DEFINITIONS.STOCK_FNO.expiries[1]);
    expect(silver.definition).toMatchObject({
      id: "MCX",
      strikeStep: 1_000,
      lotSize: 30,
      marketKind: "COMMODITY",
    });
    expect(silver.selection.expiry).toBe(MARKET_DEFINITIONS.MCX.expiries[2]);
  });
});
