import type { SVGProps } from "react";

export type IconName =
  | "activity"
  | "book"
  | "calculator"
  | "chart"
  | "chevron"
  | "database"
  | "flask"
  | "greeks"
  | "grid"
  | "info"
  | "journal"
  | "lock"
  | "menu"
  | "pulse"
  | "ranking"
  | "settings"
  | "shield"
  | "spark"
  | "target"
  | "x";

const paths: Record<IconName, React.ReactNode> = {
  activity: <path d="M3 12h4l2.5-7 5 14 2.5-7H21" />,
  book: <path d="M5 4.5A2.5 2.5 0 0 1 7.5 2H20v17H7.5A2.5 2.5 0 0 0 5 21.5zm0 0v17M9 7h7M9 11h7" />,
  calculator: <><rect x="4" y="2" width="16" height="20" rx="2" /><path d="M8 6h8v4H8zm0 8h.01m4-.01V14m4-.01V14M8 18h.01m4-.01V18m4-.01V18" /></>,
  chart: <><path d="M4 19V9m6 10V5m6 14v-7m4 7H2" /><path d="m3 7 5-4 5 4 7-5" /></>,
  chevron: <path d="m9 18 6-6-6-6" />,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>,
  flask: <path d="M9 3h6m-5 0v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7.5 16h9" />,
  greeks: <><path d="M6 4h12M9 4c0 7-3 8-3 12a4 4 0 0 0 8 0c0-4-3-5-3-12" /><path d="M14 9h4m-2-2v4" /></>,
  grid: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5m0-8h.01" /></>,
  journal: <><path d="M5 3h12a2 2 0 0 1 2 2v16H7a2 2 0 0 1-2-2z" /><path d="M9 7h6m-6 4h6m-6 4h4M5 19a2 2 0 0 1 2-2h12" /></>,
  lock: <><rect x="4" y="10" width="16" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3" /></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  pulse: <><circle cx="12" cy="12" r="9" /><path d="M4 12h4l2-4 4 8 2-4h4" /></>,
  ranking: <><path d="M8 6h13M8 12h10M8 18h7" /><path d="M3 6h.01M3 12h.01M3 18h.01" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V3h4v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" /></>,
  shield: <path d="M12 3 4.5 6v5.5c0 4.6 3.2 7.9 7.5 9.5 4.3-1.6 7.5-4.9 7.5-9.5V6zm-3 9 2 2 4-5" />,
  spark: <path d="m12 2 1.6 5.4L19 9l-5.4 1.6L12 16l-1.6-5.4L5 9l5.4-1.6zm7 13 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z" />,
  target: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1" /></>,
  x: <path d="m6 6 12 12M18 6 6 18" />,
};

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
