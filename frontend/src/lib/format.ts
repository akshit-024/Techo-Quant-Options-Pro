const indianNumber = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 2,
});

const compactNumber = new Intl.NumberFormat("en-IN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export function formatNumber(value: number, digits = 2): string {
  return indianNumber.format(Number(value.toFixed(digits)));
}

export function formatCompact(value: number): string {
  return compactNumber.format(value);
}

export function formatPrice(value: number): string {
  return `₹${formatNumber(value)}`;
}

export function formatExpiry(value: string): string {
  const normalized = value.trim();
  const date = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(normalized)
      ? `${normalized}T00:00:00+05:30`
      : normalized,
  );
  if (!Number.isFinite(date.getTime())) return "Invalid expiry";
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  }).format(date);
}

export function signed(value: number, suffix = ""): string {
  return `${value > 0 ? "+" : ""}${formatNumber(value)}${suffix}`;
}
