import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { formatNumber } from "../../lib/format";
import {
  DEMO_AUTOMATION_TIMELINE,
  DEMO_SIGNAL_HISTORY,
  type AutomationEvent,
  type SignalHistoryRow,
} from "../../data/operationsDemo";
import {
  DemoBoundary,
  formatOperationsDate,
  OperationsHeading,
} from "./OperationsChrome";
import "./operations.css";

export interface SignalHistoryViewProps {
  signals?: readonly SignalHistoryRow[];
  timeline?: readonly AutomationEvent[];
}

function signalTone(state: SignalHistoryRow["state"]): "positive" | "warning" | "danger" {
  if (state === "ACTIONABLE") return "positive";
  if (state === "WAIT") return "warning";
  return "danger";
}

function timelineTone(state: AutomationEvent["state"]): "positive" | "warning" | "danger" {
  if (state === "SUCCESS") return "positive";
  if (state === "WARNING") return "warning";
  return "danger";
}

export function SignalHistoryView({
  signals = DEMO_SIGNAL_HISTORY,
  timeline = DEMO_AUTOMATION_TIMELINE,
}: SignalHistoryViewProps) {
  const actionable = signals.filter((signal) => signal.state === "ACTIONABLE").length;
  const blocked = signals.filter((signal) => signal.state === "BLOCKED").length;
  const latest = signals[0];

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Operational record"
        title="Signal history"
        description="A traceable read-only view of analytical decisions and the automation stages that produced them."
        meta={latest ? <span className="ops-heading__timestamp">Latest {formatOperationsDate(latest.capturedAt)}</span> : null}
      />

      <DemoBoundary>
        Signal identifiers and timestamps below are illustrative. No row proves that an order was approved, routed, or filled.
      </DemoBoundary>

      <section className="ops-stat-grid" aria-label="Signal summary">
        <article className="ops-stat-card">
          <span>Observed signals</span>
          <strong>{signals.length}</strong>
          <small>Current demo window</small>
        </article>
        <article className="ops-stat-card ops-stat-card--positive">
          <span>Actionable</span>
          <strong>{actionable}</strong>
          <small>Analytical state only</small>
        </article>
        <article className="ops-stat-card ops-stat-card--warning">
          <span>Wait / blocked</span>
          <strong>{signals.length - actionable}</strong>
          <small>{blocked} hard blocked</small>
        </article>
        <article className="ops-stat-card">
          <span>Broker mutations</span>
          <strong>0</strong>
          <small>Browser boundary held</small>
        </article>
      </section>

      <div className="ops-split ops-split--history">
        <section className="panel ops-table-panel">
          <SectionHeader
            eyebrow="Decision ledger"
            title="Recent analytical signals"
            description="Every decision keeps its score evidence, selected contract, and explicit hold reason."
            action={<Badge tone="neutral">{signals.length} demo rows</Badge>}
          />
          <div className="ops-table-wrap">
            <table className="ops-table ops-table--signals">
              <caption className="ops-sr-only">Recent demo analytical signals</caption>
              <thead>
                <tr>
                  <th scope="col">Captured</th>
                  <th scope="col">Context</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Call / put</th>
                  <th scope="col">Contract</th>
                  <th scope="col">State and evidence</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((signal) => (
                  <tr key={signal.id}>
                    <td>
                      <strong className="ops-mono">{formatOperationsDate(signal.capturedAt)}</strong>
                      <small>{signal.id}</small>
                    </td>
                    <td>
                      <strong>{signal.symbol}</strong>
                      <small>{signal.market} · {signal.expiry}</small>
                    </td>
                    <td><Badge tone={signalTone(signal.state)} dot>{signal.decision}</Badge></td>
                    <td className="ops-mono">{formatNumber(signal.callScore, 1)} / {formatNumber(signal.putScore, 1)}</td>
                    <td>{signal.selectedContract ?? <span className="ops-muted">Not selected</span>}</td>
                    <td className="ops-evidence-cell">
                      <Badge tone={signalTone(signal.state)}>{signal.state}</Badge>
                      <small>{signal.reason}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel ops-timeline-panel">
          <SectionHeader
            eyebrow="Automation trace"
            title="Latest evaluation"
            description="Stage status is visible even when the final boundary blocks execution."
          />
          <ol className="ops-timeline">
            {timeline.map((event, index) => (
              <li className={`ops-timeline__item ops-timeline__item--${event.state.toLowerCase()}`} key={event.id}>
                <div className="ops-timeline__rail" aria-hidden="true">
                  <span>{index + 1}</span>
                </div>
                <div className="ops-timeline__content">
                  <div>
                    <strong>{event.title}</strong>
                    <time>{event.occurredAt}</time>
                  </div>
                  <p>{event.detail}</p>
                  <Badge tone={timelineTone(event.state)}>{event.state}</Badge>
                </div>
              </li>
            ))}
          </ol>
          <div className="ops-readonly-callout">
            <Icon name="lock" size={15} />
            Timeline inspection is read only. Approval and broker mutation controls are intentionally absent.
          </div>
        </section>
      </div>
    </div>
  );
}
