import { useState } from "react";

import type { CalculatorInput } from "../../domain/types";
import { Badge } from "../../components/ui/Badge";
import { Icon } from "../../components/ui/Icon";
import { SectionHeader } from "../../components/ui/SectionHeader";

interface InputsPanelProps {
  inputs: readonly CalculatorInput[];
  presentationMode: "QUICK" | "PRO";
  onOverride: (id: string, value: string) => void;
}

const groups: readonly {
  id: CalculatorInput["group"];
  label: string;
  description: string;
}[] = [
  { id: "SESSION", label: "Session", description: "Contract and operating context" },
  { id: "MARKET", label: "Market", description: "Imported prices and calculated strike" },
  { id: "TECHNICAL", label: "Technical", description: "Completed-candle indicators" },
  { id: "RISK", label: "Risk", description: "Capital and verified contract limits" },
];

const quickIds = new Set(["symbol", "option_expiry", "spot", "futures", "vwap", "rsi14", "atm", "event_risk"]);

function sourceTone(
  source: CalculatorInput["source"],
): "info" | "positive" | "warning" | "purple" {
  if (source === "MANUAL OVERRIDE") return "warning";
  if (source === "LIVE FEED") return "positive";
  if (source === "COMPUTED") return "purple";
  return "info";
}

export function InputsPanel({ inputs, presentationMode, onOverride }: InputsPanelProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const visibleInputs = presentationMode === "QUICK" ? inputs.filter((item) => quickIds.has(item.id)) : inputs;
  const overrideCount = inputs.filter((item) => item.source === "MANUAL OVERRIDE").length;

  return (
    <section className="content-section" aria-labelledby="inputs-title">
      <SectionHeader
        id="inputs-title"
        eyebrow="Source transparency"
        title="Calculator inputs"
        description="Imported values, manual changes and effective values stay separate—nothing is silently replaced."
        action={
          overrideCount > 0 ? (
            <Badge tone="warning" dot>{overrideCount} override{overrideCount === 1 ? "" : "s"} active</Badge>
          ) : (
            <Badge tone="positive" dot>No overrides</Badge>
          )
        }
      />

      <div className="input-groups">
        {groups.map((group) => {
          const groupInputs = visibleInputs.filter((item) => item.group === group.id);
          if (groupInputs.length === 0) return null;
          return (
            <article className="input-card" key={group.id}>
              <div className="input-card__heading">
                <div>
                  <h3>{group.label}</h3>
                  <p>{group.description}</p>
                </div>
                <span>{groupInputs.length.toString().padStart(2, "0")}</span>
              </div>
              <div className="input-table-wrap">
                <table className="input-table">
                  <thead>
                    <tr>
                      <th>Input</th>
                      <th>Imported value</th>
                      <th>Manual override</th>
                      <th>Effective value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {groupInputs.map((item) => (
                      <tr className={item.source === "MANUAL OVERRIDE" ? "has-override" : ""} key={item.id}>
                        <th scope="row">
                          <span>{item.label}</span>
                          {item.helper ? <small>{item.helper}</small> : null}
                        </th>
                        <td className="value-cell value-cell--imported">{item.importedValue}</td>
                        <td className="value-cell value-cell--override">
                          {item.source === "COMPUTED" ? (
                            <span className="not-applicable">Not applicable</span>
                          ) : item.source === "LIVE FEED" ? (
                            <span className="not-applicable">Backend-owned</span>
                          ) : editingId === item.id ? (
                            <div className="override-editor">
                              <input
                                aria-label={`Manual override for ${item.label}`}
                                autoFocus
                                defaultValue={item.manualOverride ?? ""}
                                onBlur={(event) => {
                                  onOverride(item.id, event.currentTarget.value);
                                  setEditingId(null);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") event.currentTarget.blur();
                                  if (event.key === "Escape") setEditingId(null);
                                }}
                              />
                            </div>
                          ) : (
                            <button className="override-button" onClick={() => setEditingId(item.id)} type="button">
                              {item.manualOverride ?? "Add override"}
                              <span>{item.manualOverride ? "Edit" : "+"}</span>
                            </button>
                          )}
                        </td>
                        <td className="value-cell value-cell--effective">
                          <strong>{item.effectiveValue}</strong>
                          <Badge tone={sourceTone(item.source)}>{item.source}</Badge>
                          {item.manualOverride ? (
                            <button
                              aria-label={`Clear override for ${item.label}`}
                              className="clear-override"
                              onClick={() => onOverride(item.id, "")}
                              type="button"
                            >
                              <Icon name="x" size={12} />
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
