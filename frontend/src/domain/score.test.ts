import { decisionFromScores, scoreBand } from "./score";

describe("score policy", () => {
  it("maps exact score thresholds into the canonical bands", () => {
    expect(scoreBand(64.99)).toBe("NO TRADE");
    expect(scoreBand(65)).toBe("WATCHLIST");
    expect(scoreBand(74.99)).toBe("WATCHLIST");
    expect(scoreBand(75)).toBe("TRADABLE");
    expect(scoreBand(84.99)).toBe("TRADABLE");
    expect(scoreBand(85)).toBe("STRONG");
  });

  it("fails closed below threshold or when the directional score gap is insufficient", () => {
    expect(decisionFromScores(64.9, 40).decision).toBe("NO TRADE");
    expect(decisionFromScores(80, 73).decision).toBe("WAIT");
    expect(decisionFromScores(70, 50).decision).toBe("WAIT");
  });

  it("emits directional decisions only for a tradable leader with an eight-point gap", () => {
    expect(decisionFromScores(75, 67)).toMatchObject({ decision: "BUY CALL" });
    expect(decisionFromScores(67, 75)).toMatchObject({ decision: "BUY PUT" });
    expect(decisionFromScores(75, 67).reason).toMatch(/at least 8 points/);
  });
});
