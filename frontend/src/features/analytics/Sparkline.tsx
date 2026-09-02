interface SparklineProps {
  values: readonly number[];
  tone?: "cyan" | "green" | "violet";
  label: string;
}

export function Sparkline({ values, tone = "cyan", label }: SparklineProps) {
  const width = 420;
  const height = 120;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1);
  const points = values
    .map((value, index) => {
      const x = (index / Math.max(values.length - 1, 1)) * width;
      const y = height - ((value - min) / range) * (height - 18) - 9;
      return `${x},${y}`;
    })
    .join(" ");
  const area = `0,${height} ${points} ${width},${height}`;

  return (
    <svg className={`sparkline sparkline--${tone}`} role="img" aria-label={label} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id={`spark-fill-${tone}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity=".28" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line className="sparkline__guide" x1="0" x2={width} y1={height / 2} y2={height / 2} />
      <polygon fill={`url(#spark-fill-${tone})`} points={area} />
      <polyline className="sparkline__line" fill="none" points={points} />
      <circle className="sparkline__point" cx={width} cy={points.split(" ").at(-1)?.split(",")[1]} r="4" />
    </svg>
  );
}
