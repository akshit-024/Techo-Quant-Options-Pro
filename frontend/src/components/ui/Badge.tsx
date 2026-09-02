import type { PropsWithChildren } from "react";

type BadgeTone = "neutral" | "positive" | "warning" | "danger" | "info" | "purple";

interface BadgeProps extends PropsWithChildren {
  tone?: BadgeTone;
  dot?: boolean;
}

export function Badge({ children, tone = "neutral", dot = false }: BadgeProps) {
  return (
    <span className={`badge badge--${tone}`}>
      {dot ? <span className="badge__dot" /> : null}
      {children}
    </span>
  );
}
