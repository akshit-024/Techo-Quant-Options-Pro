import type { LatestSignal } from "../../api/contracts";
import type { MarketSnapshot } from "../../domain/types";
import type {
  BackendConnection,
  BackendStatusState,
} from "../../hooks/useBackendStatus";
import "./ApiStatusView.css";

const EXPECTED_STRIKES = ["ATM−2", "ATM−1", "ATM", "ATM+1", "ATM+2"] as const;

export interface ApiStatusViewProps {
  snapshot: MarketSnapshot;
  state: BackendStatusState;
  /** Deterministic clock override for rendering/tests. */
  now?: Date;
}

interface SignalMetadata {
  generatedAt: string | null;
  dataAt: string | null;
  securityId: string | null;
  expiry: string | null;
}

export function ApiStatusView({
  snapshot,
  state,
  now = new Date(),
}: ApiStatusViewProps) {
  const payload = state.payload;
  const signal = signalMetadata(payload?.latestSignal.signal ?? null);
  const missingStrikes = EXPECTED_STRIKES.filter(
    (label) => !snapshot.chain.some((strike) => strike.moneyness === label),
  );
  const openPositions =
    payload?.paperPositions.positions.filter((position) => position.state === "OPEN")
      .length ?? 0;
  const marketData = payload?.status.market_data;
  const feed = marketData?.feed;
  const connectionClass = state.connection.toLowerCase().replace("_", "-");

  return (
    <section
      aria-labelledby="api-status-title"
      className={`api-status api-status--${connectionClass}`}
    >
      <header className="api-status__header">
        <div>
          <p className="api-status__eyebrow">Read-only backend</p>
          <h2 id="api-status-title">API and data status</h2>
          <p className="api-status__description">
            Observability only. This interface sends GET requests and holds no API key.
          </p>
        </div>
        <div className="api-status__actions">
          <span
            aria-live="polite"
            className="api-status__connection"
            role="status"
          >
            <i aria-hidden="true" />
            {connectionLabel(state.connection)}
          </span>
          {state.connection !== "NOT_CONFIGURED" ? (
            <button
              className="api-status__refresh"
              disabled={state.isLoading}
              onClick={state.refresh}
              type="button"
            >
              {state.isLoading ? "Checking…" : "Refresh status"}
            </button>
          ) : null}
        </div>
      </header>

      {state.connection === "NOT_CONFIGURED" ? (
        <p className="api-status__notice">
          Set <code>VITE_API_BASE_URL</code> to enable read-only backend checks. No
          network request has been made.
        </p>
      ) : null}

      {snapshot.dataMode === "DEMO" ? (
        <p className="api-status__notice api-status__notice--demo">
          The analytical workspace is showing deterministic demo data, not broker market
          data.
        </p>
      ) : null}

      <dl className="api-status__grid">
        <StatusItem label="Connection">
          {connectionLabel(state.connection)}
        </StatusItem>
        <StatusItem label="Last backend success">
          <Timestamp value={state.lastSuccessAt} />
        </StatusItem>
        <StatusItem label="Backend status age">
          {formatAge(state.lastSuccessAt, now)}
        </StatusItem>
        <StatusItem label="Workspace snapshot">
          <Timestamp value={snapshot.capturedAt} />
        </StatusItem>
        <StatusItem label="Workspace data age">
          {formatAge(snapshot.capturedAt, now)}
        </StatusItem>
        <StatusItem label="Latest signal">
          <Timestamp value={signal.generatedAt} />
        </StatusItem>
        <StatusItem label="Signal data time">
          <Timestamp value={signal.dataAt} />
        </StatusItem>
        <StatusItem label="Data source">
          {snapshot.dataMode === "DEMO"
            ? "DEMO — synthetic"
            : `${snapshot.dataMode} — ${snapshot.backendAuthority?.source ?? "backend"}`}
        </StatusItem>
        <StatusItem label="Market-data state">
          {feed?.state ?? "Unknown"}
        </StatusItem>
        <StatusItem label="Transport health">
          {feed === undefined
            ? "Unknown"
            : feed.transport_healthy === true
              ? "Healthy"
              : "Not ready"}
        </StatusItem>
        <StatusItem label="Validated data health">
          {feed === undefined
            ? "Unknown"
            : feed.data_healthy === true
              ? "Healthy"
              : "Not ready"}
        </StatusItem>
        <StatusItem label="Decision profile">
          {feed === undefined
            ? "Unknown"
            : feed.decision_inputs_configured === true
              ? "Configured"
              : "Required"}
        </StatusItem>
        <StatusItem label="Backend actionability">
          {feed === undefined
            ? "Unknown"
            : feed.actionable_ready === true
              ? "Ready"
              : "Locked"}
        </StatusItem>
        <StatusItem label="Market revision">
          {marketData?.revision ?? "Unknown"}
        </StatusItem>
        <StatusItem label="Broker gateway">
          {payload === null
            ? "Unknown"
            : payload.status.live_gateway_configured
              ? "Configured"
              : "Not configured"}
        </StatusItem>
        <StatusItem label="Execution security">
          {executionGuard(state)}
        </StatusItem>
        <StatusItem label="Security ID">
          {signal.securityId ?? "No latest contract"}
        </StatusItem>
        <StatusItem label="Expiries">
          <span className="api-status__stacked-value">
            <span>Workspace: {snapshot.selection.expiry}</span>
            <span>Backend: {signal.expiry ?? "No trade plan"}</span>
          </span>
        </StatusItem>
        <StatusItem label="Missing strikes">
          {missingStrikes.length === 0 ? "None" : missingStrikes.join(", ")}
        </StatusItem>
        <StatusItem label="Paper positions">
          {payload === null
            ? "Unknown"
            : `${openPositions} open / ${payload.paperPositions.positions.length} total`}
        </StatusItem>
        <StatusItem label="Journal summary">
          {payload === null
            ? "Unknown"
            : `${payload.journalSummary.journal_entries} entries · ${payload.journalSummary.orders} orders`}
        </StatusItem>
      </dl>

      {state.error !== null ? (
        <div className="api-status__error" role="alert">
          <strong>Backend error</strong>
          <span>{state.error}</span>
          {state.payload !== null ? (
            <small>The last successful read-only payload remains visible.</small>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function StatusItem({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="api-status__item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Timestamp({ value }: { value: string | null }) {
  if (value === null) return <>Never</>;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return <>Invalid timestamp</>;
  return (
    <time dateTime={value} title={value}>
      {new Intl.DateTimeFormat("en-IN", {
        dateStyle: "medium",
        timeStyle: "medium",
        timeZone: "Asia/Kolkata",
      }).format(parsed)}
    </time>
  );
}

function formatAge(value: string | null, now: Date): string {
  if (value === null) return "Unknown";
  const timestamp = new Date(value).valueOf();
  if (Number.isNaN(timestamp)) return "Unknown";
  const seconds = Math.max(0, Math.floor((now.valueOf() - timestamp) / 1_000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function connectionLabel(connection: BackendConnection): string {
  if (connection === "NOT_CONFIGURED") return "Not configured";
  return connection.charAt(0) + connection.slice(1).toLowerCase();
}

function executionGuard(state: BackendStatusState): string {
  const payload = state.payload;
  if (payload === null) return "Unknown";
  const lock = payload.health.live_locked ? "Live locked" : "Live enabled";
  const killSwitch = payload.status.kill_switch.active
    ? "kill switch active"
    : "kill switch clear";
  return `${lock} · ${killSwitch}`;
}

function signalMetadata(signal: LatestSignal | null): SignalMetadata {
  if (signal === null) {
    return { generatedAt: null, dataAt: null, securityId: null, expiry: null };
  }
  if ("ranked_strikes" in signal) {
    const plan = signal.trade_plan;
    return {
      generatedAt: signal.generated_at,
      // The analysis-history response does not expose its source-data timestamp.
      // Never substitute generation time and label it as market-data time.
      dataAt: null,
      securityId: plan?.security_id ?? null,
      expiry: plan?.expiry ?? null,
    };
  }
  return {
    generatedAt: signal.received_at,
    dataAt: signal.plan.data_time,
    securityId: signal.security_id,
    expiry: signal.plan.contract_expiry,
  };
}
