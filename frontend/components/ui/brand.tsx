import * as React from "react";
import { cn } from "@/lib/utils";

interface BrandMarkProps extends React.HTMLAttributes<HTMLDivElement> {
  size?: number;
  glow?: boolean;
}

/**
 * Discoin mark. Inline SVG so colors follow theme tokens and scales crisply.
 */
export function BrandMark({ size = 36, glow = false, className, ...props }: BrandMarkProps) {
  const uid = React.useId();
  return (
    <div
      className={cn(
        "relative inline-flex shrink-0 items-center justify-center",
        glow && "drop-shadow-[0_6px_24px_oklch(0.68_0.22_262/0.35)]",
        className,
      )}
      style={{ width: size, height: size }}
      {...props}
    >
      <svg
        viewBox="0 0 64 64"
        width={size}
        height={size}
        fill="none"
        aria-hidden="true"
      >
        <defs>
          <linearGradient id={`brand-ring-${uid}`} x1="8" y1="8" x2="56" y2="56" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor="#7C5CFF" />
            <stop offset="55%" stopColor="#4FB3FF" />
            <stop offset="100%" stopColor="#5AEBE0" />
          </linearGradient>
        </defs>
        <circle cx="32" cy="32" r="30" fill={`url(#brand-ring-${uid})`} />
        <circle cx="32" cy="32" r="25" fill="#0B0D1F" />
        <path
          d="M22 18h11c8.28 0 14 6.27 14 14s-5.72 14-14 14H22V18zm6 6v16h5c4.42 0 8-3.58 8-8s-3.58-8-8-8h-5z"
          fill={`url(#brand-ring-${uid})`}
        />
        <path d="M43 22l4-4 2 2-4 4-2-2z" fill="#5AEBE0" opacity="0.9" />
      </svg>
    </div>
  );
}

interface BrandLockupProps extends React.HTMLAttributes<HTMLSpanElement> {
  size?: "sm" | "md" | "lg";
  showMark?: boolean;
}

export function BrandLockup({
  size = "md",
  showMark = true,
  className,
  ...props
}: BrandLockupProps) {
  const markSize = size === "sm" ? 24 : size === "lg" ? 40 : 30;
  const textClass =
    size === "sm"
      ? "text-base"
      : size === "lg"
        ? "text-2xl"
        : "text-lg";

  return (
    <span
      className={cn("inline-flex items-center gap-2", className)}
      {...props}
    >
      {showMark ? <BrandMark size={markSize} /> : null}
      <span
        className={cn(
          "font-display font-semibold tracking-tight text-gradient-brand",
          textClass,
        )}
      >
        Discoin
      </span>
    </span>
  );
}
