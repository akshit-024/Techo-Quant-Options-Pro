import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import {
  DEMO_EXECUTION_MODES,
  DEMO_GUARDRAILS,
  type ExecutionModeOption,
  type GuardrailSetting,
} from "../../data/operationsDemo";
import { DemoBoundary, OperationsHeading } from "./OperationsChrome";
import "./operations.css";

export interface SettingsViewProps {
  modes?: readonly ExecutionModeOption[];
  guardrails?: readonly GuardrailSetting[];
  decisionInputsConfigured?: boolean;
  marketDataState?: string | null;
}

const DECISION_PROFILE_FIELDS = [
  ["Account capital", "TECO_ACCOUNT_CAPITAL", false],
  ["Risk per trade", "TECO_RISK_PER_TRADE", false],
  ["Maximum premium allocation", "TECO_MAX_PREMIUM_ALLOCATION", false],
  ["Event risk", "TECO_EVENT_RISK_ACTIVE", false],
  ["Expected holding hours", "TECO_EXPECTED_HOLDING_HOURS", false],
  ["Trading style", "TECO_TRADING_STYLE", false],
  ["Operating mode", "TECO_OPERATING_MODE", false],
  ["Price-action confirmation", "TECO_PRICE_ACTION_CONFIRMED", true],
] as const;

export function SettingsView({
  modes = DEMO_EXECUTION_MODES,
  guardrails = DEMO_GUARDRAILS,
  decisionInputsConfigured = false,
  marketDataState,
}: SettingsViewProps) {
  const profileState = decisionInputsConfigured ? "CONFIGURED" : "REQUIRED";
  const displayedMarketDataState = marketDataState?.trim() || "UNKNOWN";

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Safety configuration"
        title="Settings and guardrails"
        description="A read-only view of execution modes and server-owned risk controls."
        meta={<Badge tone="info"><Icon name="shield" size={11} /> Backend authority</Badge>}
      />

      <DemoBoundary tone="CAUTION">
        Controls on this page are deliberately disabled. Configuration changes require authenticated backend administration and an auditable deployment process.
      </DemoBoundary>

      <section
        className="panel ops-decision-profile"
        aria-labelledby="operator-decision-profile-title"
      >
        <SectionHeader
          id="operator-decision-profile-title"
          eyebrow="Server-owned inputs"
          title="Operator decision profile"
          description="The backend reports whether its complete decision profile is available. This browser receives status only, never the configured values."
          action={(
            <Badge tone={decisionInputsConfigured ? "positive" : "warning"}>
              <Icon name={decisionInputsConfigured ? "shield" : "lock"} size={11} />
              {profileState}
            </Badge>
          )}
        />

        <div className="ops-decision-profile__status" aria-label="Decision profile status">
          <div>
            <span>Decision inputs</span>
            <strong>{profileState}</strong>
          </div>
          <div>
            <span>Market data state</span>
            <strong>{displayedMarketDataState}</strong>
          </div>
          <p>
            Edit these settings only in <code>backend/.env</code>, then restart the backend.
            They cannot be changed or persisted by this browser.
          </p>
        </div>

        <dl className="ops-decision-profile__fields">
          {DECISION_PROFILE_FIELDS.map(([label, environmentName, optional]) => (
            <div key={environmentName}>
              <dt>{label}</dt>
              <dd>
                <code>{environmentName}</code>
                <span
                  className={
                    optional
                      ? "is-optional"
                      : decisionInputsConfigured
                        ? "is-configured"
                        : "is-required"
                  }
                >
                  {optional ? "OPTIONAL" : profileState}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="panel ops-mode-panel">
        <SectionHeader
          eyebrow="Execution posture"
          title="Mode selection"
          description="The selected value is informational. This browser cannot activate paper, approval, or live execution."
          action={<Badge tone="neutral"><Icon name="lock" size={11} /> Read only</Badge>}
        />
        <div className="ops-mode-grid" role="radiogroup" aria-label="Read-only execution modes" aria-readonly="true">
          {modes.map((mode) => {
            const live = mode.id === "LIVE_AUTOMATIC";
            return (
              <label className={`ops-mode-card${mode.selected ? " is-selected" : ""}${live ? " is-live" : ""}`} key={mode.id}>
                <input checked={mode.selected} disabled name="execution-mode-demo" readOnly type="radio" />
                <span className="ops-mode-card__selector" aria-hidden="true" />
                <span className="ops-mode-card__copy">
                  <span>
                    <strong>{mode.label}</strong>
                    {live ? <Badge tone="danger"><Icon name="lock" size={10} /> Disabled</Badge> : mode.selected ? <Badge tone="info">Observed</Badge> : null}
                  </span>
                  <small>{mode.description}</small>
                  <code>{mode.id}</code>
                </span>
              </label>
            );
          })}
        </div>
        <div className="ops-live-lock">
          <Icon name="lock" size={19} />
          <div><strong>LIVE_AUTOMATIC remains locked</strong><p>No client-side action, URL flag, or browser storage value can enable live order submission.</p></div>
        </div>
      </section>

      <section className="panel ops-guardrail-panel">
        <SectionHeader
          eyebrow="Fail-closed limits"
          title="Backend guardrails"
          description="Displayed values are examples of limits the trusted execution controller must enforce again at submission time."
          action={<Badge tone="warning">Demo configuration</Badge>}
        />
        <dl className="ops-guardrail-grid">
          {guardrails.map((guardrail) => (
            <div key={guardrail.id}>
              <dt><span>{guardrail.label}</span>{guardrail.locked ? <Icon name="lock" size={12} /> : null}</dt>
              <dd>{guardrail.value}</dd>
              <p>{guardrail.description}</p>
            </div>
          ))}
        </dl>
      </section>

      <section className="ops-secret-boundary" aria-labelledby="browser-secret-title">
        <div className="ops-secret-boundary__icon"><Icon name="shield" size={26} /></div>
        <div>
          <p className="eyebrow">Credential boundary</p>
          <h2 id="browser-secret-title">No browser mutation secret</h2>
          <p>Broker keys, API mutation keys, session tokens, and approval credentials must never be bundled into frontend code, exposed through <code>VITE_*</code>, or stored in localStorage.</p>
        </div>
        <ul>
          <li><span>01</span> Browser requests a safe view</li>
          <li><span>02</span> Trusted backend authenticates and revalidates</li>
          <li><span>03</span> Backend owns reconciliation and mutation</li>
        </ul>
      </section>
    </div>
  );
}
