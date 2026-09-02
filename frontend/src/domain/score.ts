import type { Decision, ScoreBand } from "./types";

export function scoreBand(score: number): ScoreBand {
  if (score >= 85) return "STRONG";
  if (score >= 75) return "TRADABLE";
  if (score >= 65) return "WATCHLIST";
  return "NO TRADE";
}

export function decisionFromScores(callScore: number, putScore: number): {
  decision: Decision;
  reason: string;
} {
  const leader = Math.max(callScore, putScore);
  const gap = Math.abs(callScore - putScore);

  if (leader < 65) {
    return {
      decision: "NO TRADE",
      reason: "Neither side clears the minimum watchlist threshold.",
    };
  }

  if (gap < 8) {
    return {
      decision: "WAIT",
      reason: "Call and put evidence is too close for a directional edge.",
    };
  }

  if (leader < 75) {
    return {
      decision: "WAIT",
      reason: "The leading side remains in the 65–74 watchlist band.",
    };
  }

  return callScore > putScore
    ? {
        decision: "BUY CALL",
        reason: "Call evidence leads by at least 8 points and is tradable.",
      }
    : {
        decision: "BUY PUT",
        reason: "Put evidence leads by at least 8 points and is tradable.",
      };
}
