import type { OperationalGate } from "../domain/gate";
import { Badge } from "./ui/Badge";
import { Icon } from "./ui/Icon";

interface OperationalGateBannerProps {
  gate: OperationalGate;
  onOpenStatus?: () => void;
}

export function OperationalGateBanner({ gate, onOpenStatus }: OperationalGateBannerProps) {
  const ready = gate.signalingAllowed;
  return (
    <aside className={`operational-gate ${ready ? "is-ready" : "is-blocked"}`} aria-label="Operational decision gate">
      <div className="operational-gate__icon"><Icon name={ready ? "activity" : "shield"} size={18} /></div>
      <div className="operational-gate__copy">
        <span>Operational decision</span>
        <strong>{gate.decision}</strong>
        <p>{gate.reason}</p>
      </div>
      <Badge tone={ready ? "positive" : gate.decision === "INSUFFICIENT DATA" ? "warning" : "danger"}>
        {gate.code.replaceAll("_", " ")}
      </Badge>
      {onOpenStatus ? (
        <button className="operational-gate__action" onClick={onOpenStatus} type="button">
          Inspect status <Icon name="chevron" size={13} />
        </button>
      ) : null}
    </aside>
  );
}
