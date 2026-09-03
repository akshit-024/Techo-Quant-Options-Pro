import type { MarketSnapshot } from "../../domain/types";
import { InputsPanel } from "./InputsPanel";
import { OptionChainTable } from "./OptionChainTable";

interface CalculatorViewProps {
  snapshot: MarketSnapshot;
  presentationMode: "QUICK" | "PRO";
  selectedLegKey: string;
  onLegSelect: (key: string) => void;
  onOverride: (id: string, value: string) => void;
}

export function CalculatorView({
  snapshot,
  presentationMode,
  selectedLegKey,
  onLegSelect,
  onOverride,
}: CalculatorViewProps) {
  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <p className="eyebrow">{snapshot.definition.marketKind} CALCULATOR</p>
          <h1>{snapshot.selection.symbol} evidence workspace</h1>
          <p>Trace every imported input into a verified five-strike analytical view.</p>
        </div>
        <div className="page-intro__meta">
          <span>Lot size <strong>{snapshot.definition.lotSize}</strong></span>
          <span>Interval <strong>{snapshot.definition.strikeStep}</strong></span>
          <span>Model <strong>{snapshot.selection.market === "MCX" ? "Black–76" : "Black–Scholes"}</strong></span>
        </div>
      </div>
      <InputsPanel inputs={snapshot.inputs} presentationMode={presentationMode} onOverride={onOverride} />
      <OptionChainTable
        chain={snapshot.chain}
        dataMode={snapshot.dataMode}
        onLegSelect={onLegSelect}
        presentationMode={presentationMode}
        selectedLegKey={selectedLegKey}
      />
    </div>
  );
}
