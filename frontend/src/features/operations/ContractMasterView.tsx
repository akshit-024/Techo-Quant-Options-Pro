import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";
import { formatNumber } from "../../lib/format";
import {
  DEMO_CONTRACT_MASTER,
  type ContractMasterRow,
} from "../../data/operationsDemo";
import { DemoBoundary, OperationsHeading } from "./OperationsChrome";
import "./operations.css";

export interface ContractMasterViewProps {
  contracts?: readonly ContractMasterRow[];
}

export function ContractMasterView({ contracts = DEMO_CONTRACT_MASTER }: ContractMasterViewProps) {
  const reviewCount = contracts.filter((contract) => contract.status === "EXPIRY_REVIEW").length;

  return (
    <div className="page-stack ops-page">
      <OperationsHeading
        eyebrow="Reference data"
        title="Contract master"
        description="Canonical instrument attributes kept visible beside pricing and execution assumptions."
        meta={<span className="ops-heading__timestamp">{contracts.length} demo definitions</span>}
      />

      <DemoBoundary>
        Security IDs, expiries, and lot sizes are demonstration values. They must be replaced by freshly attested broker instrument-master records before any live use.
      </DemoBoundary>

      <section className="ops-stat-grid" aria-label="Contract master summary">
        <article className="ops-stat-card"><span>Definitions</span><strong>{contracts.length}</strong><small>Across supported markets</small></article>
        <article className="ops-stat-card"><span>Exchanges</span><strong>{new Set(contracts.map((contract) => contract.exchange)).size}</strong><small>NSE, BSE, and MCX demo scope</small></article>
        <article className="ops-stat-card ops-stat-card--warning"><span>Review required</span><strong>{reviewCount}</strong><small>Expiry or provenance review</small></article>
        <article className="ops-stat-card"><span>Production IDs</span><strong>0</strong><small>Demo prefix enforced</small></article>
      </section>

      <section className="panel ops-table-panel">
        <SectionHeader
          eyebrow="Demonstration registry"
          title="Instrument definitions"
          description="Class, segment, expiry, lot, interval, tick, and pricing model remain explicit for review."
          action={<Badge tone="neutral"><Icon name="database" size={11} /> Read only</Badge>}
        />
        <div className="ops-table-wrap">
          <table className="ops-table ops-table--contracts">
            <caption className="ops-sr-only">Demo contract master definitions</caption>
            <thead>
              <tr><th scope="col">Market / symbol</th><th scope="col">Exchange / segment</th><th scope="col">Security ID</th><th scope="col">Class</th><th scope="col">Expiry</th><th scope="col">Lot</th><th scope="col">Strike interval</th><th scope="col">Tick</th><th scope="col">Pricing</th><th scope="col">Status</th></tr>
            </thead>
            <tbody>
              {contracts.map((contract) => (
                <tr key={`${contract.exchange}:${contract.securityId}`}>
                  <td><strong>{contract.symbol}</strong><small>{contract.market}</small></td>
                  <td><strong>{contract.exchange}</strong><small>{contract.segment}</small></td>
                  <td><code className="ops-contract-id">{contract.securityId}</code></td>
                  <td>{contract.instrumentClass}</td>
                  <td className="ops-mono">{contract.expiry}</td>
                  <td className="ops-mono">{contract.lotSize.toLocaleString("en-IN")}</td>
                  <td className="ops-mono">{formatNumber(contract.strikeInterval, 2)}</td>
                  <td className="ops-mono">{formatNumber(contract.tickSize, 2)}</td>
                  <td><Badge tone={contract.pricingModel === "BLACK_76" ? "purple" : "info"}>{contract.pricingModel.replace("_", " ")}</Badge></td>
                  <td><Badge dot tone={contract.status === "VERIFIED_DEMO" ? "positive" : "warning"}>{contract.status.replace("_", " ")}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="ops-table-footnote"><Icon name="info" size={14} /> DEMO-prefixed identifiers are intentionally non-routable and must never be submitted to a broker.</div>
      </section>
    </div>
  );
}
