import * as React from "react";
import { cn } from "@/lib/utils";

interface GradientBorderProps extends React.HTMLAttributes<HTMLDivElement> {
  radius?: string;
  thickness?: number;
  animate?: boolean;
}

export function GradientBorder({
  className,
  children,
  radius = "1rem",
  thickness = 1,
  animate = false,
  ...props
}: GradientBorderProps) {
  return (
    <div
      className={cn("relative", className)}
      style={{
        borderRadius: radius,
        padding: thickness,
        background: "var(--gradient-brand)",
      }}
      {...props}
    >
      {animate ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-80"
          style={{
            background: "var(--gradient-aurora)",
            borderRadius: radius,
            animation: "aurora 18s ease-in-out infinite",
          }}
        />
      ) : null}
      <div
        className="relative h-full w-full bg-card"
        style={{ borderRadius: `calc(${radius} - ${thickness}px)` }}
      >
        {children}
      </div>
    </div>
  );
}
