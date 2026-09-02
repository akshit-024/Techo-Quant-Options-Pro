import type { Decision, MarketSnapshot } from "./types";

export type BackendConnectionState =
  | "CONNECTED"
  | "DISCONNECTED"
  | "STALE"
  | "NOT_CONFIGURED";

export type GateCode =
  | "READY"
  | "DEMO_DATA"
  | "STALE_DATA"
  | "BACKEND_UNAVAILABLE"
  | "MISSING_STRIKES"
  | "INVALID_EXPIRY"
  | "EVENT_RISK"
  | "BACKEND_BLOCKED"
  | "NO_ELIGIBLE_STRIKE"
  | "NON_ACTIONABLE_DECISION";

export interface OperationalGate {
  code: GateCode;
  decision: Decision;
  signalingAllowed: boolean;
  reason: string;
  dataAgeSeconds: number;
}

interface GateOptions {
  connectionState?: BackendConnectionState;
  now?: Date;
  staleAfterSeconds?: number;
}

function eventRiskIsActive(snapshot: MarketSnapshot): boolean {
  const value = snapshot.inputs
    .find((item) => item.id === "event_risk")
    ?.effectiveValue.trim()
    .toUpperCase();
  return value !== undefined && !["CLEAR", "NO", "FALSE", "0"].includes(value);
}

function optionExpiryInstant(value: string): Date {
  const normalized = value.trim();
  return new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(normalized)
      ? `${normalized}T23:59:59+05:30`
      : normalized,
  );
}

export function evaluateOperationalGate(
  snapshot: MarketSnapshot,
  options: GateOptions = {},
): OperationalGate {
  const now = options.now ?? new Date();
  const staleAfterSeconds = options.staleAfterSeconds ?? 30;
  const capturedAt = new Date(snapshot.capturedAt);
  const dataAgeSeconds = Number.isFinite(capturedAt.getTime())
    ? Math.max(0, Math.floor((now.getTime() - capturedAt.getTime()) / 1000))
    : Number.POSITIVE_INFINITY;

  if (snapshot.chain.length !== 5 || snapshot.chain.some((row) => !row.call || !row.put)) {
    return {
      code: "MISSING_STRIKES",
      decision: "INSUFFICIENT DATA",
      signalingAllowed: false,
      reason: "The complete ATM−2 through ATM+2 chain is unavailable.",
      dataAgeSeconds,
    };
  }

  const expiry = optionExpiryInstant(snapshot.selection.expiry);
  if (!Number.isFinite(expiry.getTime()) || expiry <= capturedAt) {
    return {
      code: "INVALID_EXPIRY",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "The selected option expiry is invalid or already expired at the snapshot time.",
      dataAgeSeconds,
    };
  }

  if (eventRiskIsActive(snapshot)) {
    return {
      code: "EVENT_RISK",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "The event-risk gate is active.",
      dataAgeSeconds,
    };
  }

  if (snapshot.dataMode === "DEMO") {
    return {
      code: "DEMO_DATA",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "Demo data cannot generate an operational signal.",
      dataAgeSeconds,
    };
  }

  if (snapshot.dataMode === "STALE" || dataAgeSeconds > staleAfterSeconds) {
    return {
      code: "STALE_DATA",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: `Market data is older than the ${staleAfterSeconds}-second limit.`,
      dataAgeSeconds,
    };
  }

  if (options.connectionState !== undefined && options.connectionState !== "CONNECTED") {
    return {
      code: "BACKEND_UNAVAILABLE",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "The validated backend read service is unavailable.",
      dataAgeSeconds,
    };
  }

  const authority = snapshot.backendAuthority;
  if (
    authority !== undefined &&
    (!authority.complete ||
      !authority.fresh ||
      !authority.validationAccepted ||
      !authority.actionable)
  ) {
    return {
      code: "BACKEND_BLOCKED",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason:
        authority.blockers[0] ??
        "The backend has not authorized this analytical snapshot as actionable.",
      dataAgeSeconds,
    };
  }

  if (!snapshot.ranking.some((entry) => entry.rejectionReasons.length === 0)) {
    return {
      code: "NO_ELIGIBLE_STRIKE",
      decision: "NO TRADE",
      signalingAllowed: false,
      reason: "Every candidate strike has at least one rejection reason.",
      dataAgeSeconds,
    };
  }

  const signalingAllowed = snapshot.analytics.decision === "BUY CALL" || snapshot.analytics.decision === "BUY PUT";
  return {
    code: signalingAllowed ? "READY" : "NON_ACTIONABLE_DECISION",
    decision: snapshot.analytics.decision,
    signalingAllowed,
    reason: signalingAllowed
      ? "Validated data and the analytical decision are operationally eligible."
      : "The analytical result is not an actionable directional decision.",
    dataAgeSeconds,
  };
}
