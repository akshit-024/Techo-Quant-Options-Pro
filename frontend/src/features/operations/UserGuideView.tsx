import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import {
  DEMO_GUIDE_SECTIONS,
  type GuideSection,
} from "../../data/operationsDemo";
import { DemoBoundary, OperationsHeading } from "./OperationsChrome";
import "./operations.css";

export interface UserGuideViewProps {
  sections?: readonly GuideSection[];
  onNavigate?: (destination: string) => void;
}

export function UserGuideView({
  sections = DEMO_GUIDE_SECTIONS,
  onNavigate,
}: UserGuideViewProps) {
  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Reference workflow"
        title="User guide"
        description="A compact operating sequence for moving from contract context to evidence without crossing the execution boundary."
        meta={<Badge tone="info"><Icon name="book" size={11} /> Five-step review</Badge>}
      />

      <DemoBoundary>
        The guide describes the interface-development workflow. It is not a trading recommendation, authorization procedure, or substitute for production runbooks.
      </DemoBoundary>

      <section className="panel ops-guide-overview">
        <SectionHeader
          eyebrow="Before interpreting a signal"
          title="Keep one evidence chain"
          description="Selection, imported inputs, analytics, ranking, and the execution boundary must refer to the same instrument and observation window."
          action={<Badge tone="neutral"><Icon name="lock" size={11} /> Reference only</Badge>}
        />
        <div className="ops-guide-flow" aria-label="Evidence review flow">
          {sections.map((section, index) => (
            <div key={section.id}>
              <span>{section.step}</span>
              <strong>{section.title}</strong>
              {index < sections.length - 1 ? <Icon name="chevron" size={16} /> : null}
            </div>
          ))}
        </div>
      </section>

      <section className="ops-guide-grid" aria-label="User guide steps">
        {sections.map((section) => (
          <article className="ops-guide-card" key={section.id}>
            <header>
              <span>{section.step}</span>
              <div><p className="eyebrow">Operating step</p><h2>{section.title}</h2></div>
            </header>
            <p>{section.description}</p>
            <ul>
              {section.checks.map((check) => <li key={check}><Icon name="shield" size={13} /> {check}</li>)}
            </ul>
            <button
              disabled={!onNavigate}
              onClick={() => onNavigate?.(section.destination)}
              title={onNavigate ? `Open ${section.destination}` : "Navigation wiring is pending"}
              type="button"
            >
              {onNavigate ? `Open ${section.destination}` : `${section.destination} · route pending`}
              <Icon name={onNavigate ? "chevron" : "lock"} size={14} />
            </button>
          </article>
        ))}
      </section>

      <section className="ops-guide-safety">
        <Icon name="shield" size={24} />
        <div>
          <p className="eyebrow">Stop conditions</p>
          <h2>Pause whenever identity, freshness, or provenance is uncertain.</h2>
          <p>Never infer a missing security ID, silently repair a stale timestamp, or treat a demo score as executable evidence.</p>
        </div>
        <ul>
          <li>Unexpected expiry or interval</li>
          <li>Crossed or incomplete quotes</li>
          <li>Unexplained manual override</li>
          <li>Backend health unavailable</li>
        </ul>
      </section>
    </div>
  );
}
