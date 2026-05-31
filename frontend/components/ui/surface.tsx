import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const surfaceVariants = cva(
  "relative overflow-hidden rounded-2xl text-card-foreground",
  {
    variants: {
      variant: {
        glass:
          "glass",
        solid:
          "bg-card ring-1 ring-border shadow-[var(--shadow-glass)]",
        subtle:
          "glass-subtle",
        outlined:
          "bg-transparent ring-1 ring-border",
      },
      elevation: {
        0: "",
        1: "shadow-sm",
        2: "shadow-md",
        3: "shadow-xl",
      },
      interactive: {
        true: "transition-all duration-200 hover:ring-1 hover:ring-primary/30 hover:-translate-y-0.5",
        false: "",
      },
    },
    defaultVariants: {
      variant: "glass",
      elevation: 1,
      interactive: false,
    },
  },
);

export interface SurfaceProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof surfaceVariants> {}

export function Surface({
  className,
  variant,
  elevation,
  interactive,
  ...props
}: SurfaceProps) {
  return (
    <div
      data-slot="surface"
      className={cn(surfaceVariants({ variant, elevation, interactive }), className)}
      {...props}
    />
  );
}

export { surfaceVariants };
