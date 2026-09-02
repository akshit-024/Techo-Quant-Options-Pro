import type { ViewId } from "../../domain/types";
import { Icon, type IconName } from "../../components/ui/Icon";

interface StartViewProps {
  onNavigate: (view: ViewId) => void;
}

const workflow: readonly { number: string; title: string; copy: string; icon: IconName; view: ViewId }[] = [
  { number: "01", title: "Select the market", copy: "Choose market, symbol and expiry once. Every analytical view follows that central selection.", icon: "grid", view: "dashboard" },
  { number: "02", title: "Verify the inputs", copy: "Compare imported, manual override and effective values before reading any output.", icon: "calculator", view: "calculator" },
  { number: "03", title: "Read the evidence", copy: "Use trend, OI, volatility and expected movement as a combined evidence stack.", icon: "chart", view: "dashboard" },
  { number: "04", title: "Inspect the strike", copy: "Review independent liquidity, Greeks, ask entry, bid exit and every rejection reason.", icon: "ranking", view: "ranking" },
];

export function StartView({ onNavigate }: StartViewProps) {
  return (
    <div className="start-page">
      <section className="start-hero">
        <div className="start-hero__content">
          <p className="eyebrow">TECO QUANT PRO · SPRINT 4</p>
          <h1>Evidence first.<br /><em>Every time.</em></h1>
          <p>A transparent options-intelligence workspace for India’s index, stock and commodity markets.</p>
          <div className="start-hero__actions">
            <button className="primary-button" onClick={() => onNavigate("dashboard")} type="button">Open dashboard <Icon name="chevron" size={15} /></button>
            <button className="secondary-button" onClick={() => onNavigate("calculator")} type="button">Review calculator</button>
          </div>
          <div className="start-hero__guardrail"><Icon name="lock" size={15} /> Analysis-only interface · live execution disabled</div>
        </div>
        <div className="start-visual" aria-hidden="true">
          <div className="orbit orbit--one" />
          <div className="orbit orbit--two" />
          <div className="orbit orbit--three" />
          <div className="visual-core"><div className="brand-mark brand-mark--large"><span /><span /><span /></div><strong>TQ</strong></div>
          <span className="visual-node visual-node--trend">TREND <strong>20</strong></span>
          <span className="visual-node visual-node--oi">OI <strong>15</strong></span>
          <span className="visual-node visual-node--greeks">GREEKS <strong>15</strong></span>
          <span className="visual-node visual-node--liquidity">LIQUIDITY <strong>15</strong></span>
        </div>
      </section>

      <section className="workflow-section">
        <div className="workflow-heading">
          <div><p className="eyebrow">WORKFLOW</p><h2>From raw inputs to a ranked contract</h2></div>
          <p>The interface keeps data provenance visible and never substitutes LTP for an executable price.</p>
        </div>
        <div className="workflow-grid">
          {workflow.map((item) => (
            <button className="workflow-card" key={item.number} onClick={() => onNavigate(item.view)} type="button">
              <div className="workflow-card__top"><span>{item.number}</span><Icon name={item.icon} /></div>
              <h3>{item.title}</h3>
              <p>{item.copy}</p>
              <span className="workflow-card__link">Explore <Icon name="chevron" size={13} /></span>
            </button>
          ))}
        </div>
      </section>

      <section className="principles-strip">
        <div><Icon name="database" /><span><strong>Source-aware</strong>Imported and manual values remain separate</span></div>
        <div><Icon name="shield" /><span><strong>Fail-closed</strong>Only approved decision vocabulary is shown</span></div>
        <div><Icon name="target" /><span><strong>Executable pricing</strong>Ask for entry, bid for immediate exit</span></div>
        <div><Icon name="lock" /><span><strong>Safety first</strong>No browser credentials or direct broker actions</span></div>
      </section>
    </div>
  );
}
