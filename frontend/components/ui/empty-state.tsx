import * as React from "react";
import { cn } from "@/lib/utils";
import { Inbox } from "lucide-react";

interface EmptyStateProps {
  title?: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  compact?: boolean;
}

export function EmptyState({
  title = "Nothing here yet",
  description,
  icon,
  action,
  className,
  compact = false,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center",
        compact ? "py-8" : "py-14",
        className,
      )}
    >
      <div
        aria-hidden
        className="relative mb-4 flex size-14 items-center justify-center rounded-2xl ring-1 ring-border"
        style={{
          background:
            "radial-gradient(circle at 30% 30%, color-mix(in oklch, var(--accent-indigo) 25%, transparent), transparent 60%), color-mix(in oklch, var(--surface-1) 80%, transparent)",
        }}
      >
        <div className="text-primary">
          {icon ?? <Inbox className="size-6" />}
        </div>
      </div>
      <h3 className="font-display text-base font-semibold tracking-tight">{title}</h3>
      {description ? (
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">{description}</p>
      ) : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}
