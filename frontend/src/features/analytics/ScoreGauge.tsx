import type { ScoreBand } from "../../domain/types";
import { scoreBand } from "../../domain/score";
import { formatNumber } from "../../lib/format";

interface ScoreGaugeProps {
  label: string;
  score: number;
  side: "call" | "put";
}

function bandLabel(band: ScoreBand): string {
  if (band === "NO TRADE") return "Below threshold";
  return band.charAt(0) + band.slice(1).toLowerCase();
}

export function ScoreGauge({ label, score, side }: ScoreGaugeProps) {
  const band = scoreBand(score);
  return (
    <div className={`score-gauge score-gauge--${side}`}>
      <div
        aria-label={`${label}: ${formatNumber(score, 1)} out of 100, ${bandLabel(band)}`}
        className="score-gauge__ring"
        role="img"
        style={{ "--score": score } as React.CSSProperties}
      >
        <div>
          <strong>{formatNumber(score, 1)}</strong>
          <span>/ 100</span>
        </div>
      </div>
      <div className="score-gauge__copy">
        <span>{label}</span>
        <strong>{bandLabel(band)}</strong>
      </div>
    </div>
  );
}
