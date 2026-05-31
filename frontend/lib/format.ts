/** Safe number formatting — never crashes on null/undefined. */

export function fmt(n: number | null | undefined, decimals = 2): string {
  return (n ?? 0).toFixed(decimals);
}

export function fmtPct(n: number | null | undefined, decimals = 1): string {
  return `${(n ?? 0).toFixed(decimals)}%`;
}

export function fmtUsd(n: number | null | undefined, decimals = 2): string {
  return `$${(n ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

/** Format a number with locale separators, null-safe. */
export function fmtNum(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

/** Format a number with locale separators and specific fraction digits, null-safe. */
export function fmtNumDecimals(
  n: number | null | undefined,
  options?: { min?: number; max?: number },
): string {
  return (n ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: options?.min,
    maximumFractionDigits: options?.max,
  });
}

/**
 * Format a number as currency (e.g., $1,234.56 or $1.2M)
 */
export function formatCurrency(
  value: number | null | undefined,
  options?: {
    compact?: boolean;
    decimals?: number;
    symbol?: string;
  }
): string {
  const v = value ?? 0;
  const { compact = false, decimals, symbol = "$" } = options || {};

  if (compact) {
    if (Math.abs(v) >= 1_000_000_000) {
      return `${symbol}${(v / 1_000_000_000).toFixed(decimals ?? 2)}B`;
    }
    if (Math.abs(v) >= 1_000_000) {
      return `${symbol}${(v / 1_000_000).toFixed(decimals ?? 1)}M`;
    }
    if (Math.abs(v) >= 1_000) {
      return `${symbol}${(v / 1_000).toFixed(decimals ?? 1)}K`;
    }
  }

  const d = decimals ?? (Math.abs(v) < 1 ? 4 : 2);
  return `${symbol}${v.toLocaleString("en-US", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  })}`;
}

/**
 * Format a percentage with +/- sign and color class
 */
export function formatPercent(value: number | null | undefined): {
  text: string;
  isPositive: boolean;
  colorClass: string;
} {
  const v = value ?? 0;
  const isPositive = v >= 0;
  const sign = isPositive ? "+" : "";
  return {
    text: `${sign}${v.toFixed(2)}%`,
    isPositive,
    colorClass: isPositive ? "text-chart-green" : "text-chart-red",
  };
}

/**
 * Format a number with specified decimal places
 */
export function formatNumber(
  value: number | null | undefined,
  decimals: number = 2,
  compact: boolean = false
): string {
  const v = value ?? 0;
  if (compact) {
    if (Math.abs(v) >= 1_000_000_000) {
      return `${(v / 1_000_000_000).toFixed(decimals)}B`;
    }
    if (Math.abs(v) >= 1_000_000) {
      return `${(v / 1_000_000).toFixed(decimals)}M`;
    }
    if (Math.abs(v) >= 1_000) {
      return `${(v / 1_000).toFixed(decimals)}K`;
    }
  }

  return v.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * Format a token amount with its symbol (e.g., "1,234.5678 DCOIN")
 */
export function formatTokenAmount(
  value: number,
  symbol: string,
  decimals: number = 4
): string {
  return `${formatNumber(value, decimals)} ${symbol}`;
}

/**
 * Shorten a hash or address (e.g., "abc123...xyz789")
 */
export function shortenAddress(
  hash: string,
  startChars: number = 6,
  endChars: number = 4
): string {
  if (hash.length <= startChars + endChars + 3) {
    return hash;
  }
  return `${hash.slice(0, startChars)}...${hash.slice(-endChars)}`;
}

/**
 * Format a relative time string (e.g., "2 minutes ago")
 */
export function formatRelativeTime(date: string | Date): string {
  const now = new Date();
  const then = new Date(date);
  const seconds = Math.floor((now.getTime() - then.getTime()) / 1000);

  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;

  return then.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: then.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
  });
}
