import type { ReactNode } from "react";

interface SectionHeaderProps {
  id?: string;
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}

export function SectionHeader({ id, eyebrow, title, description, action }: SectionHeaderProps) {
  return (
    <div className="section-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h2 id={id}>{title}</h2>
        {description ? <p className="section-header__description">{description}</p> : null}
      </div>
      {action ? <div className="section-header__action">{action}</div> : null}
    </div>
  );
}
