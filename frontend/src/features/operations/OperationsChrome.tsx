import type { ReactNode } from "react";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import "./operations.css";

export interface OperationsHeadingProps {
  eyebrow: string;
  title: string;
  description: string;
  meta?: ReactNode;
}

export function OperationsHeading({
  eyebrow,
  title,
  description,
  meta,
}: OperationsHeadingProps) {
  return (
    <header className="ops-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <div className="ops-heading__aside">
        <Badge tone="warning" dot>Demo data</Badge>
        <Badge tone="neutral"><Icon name="lock" size={11} /> Read only</Badge>
        {meta}
      </div>
    </header>
  );
}

export interface DemoBoundaryProps {
  children?: ReactNode;
  tone?: "INFO" | "CAUTION";
}

export function DemoBoundary({ children, tone = "INFO" }: DemoBoundaryProps) {
  return (
    <aside className={`ops-boundary ops-boundary--${tone.toLowerCase()}`}>
      <Icon name={tone === "CAUTION" ? "shield" : "info"} size={18} />
      <p>
        <strong>{tone === "CAUTION" ? "Safety boundary." : "Demonstration boundary."}</strong>{" "}
        {children ?? "Records on this screen are deterministic interface data, not broker, exchange, or production-ledger records."}
      </p>
    </aside>
  );
}

export function formatOperationsDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatOperationsMoney(value: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(value);
}
