"use client";

import { Info } from "lucide-react";

interface RuleBannerProps {
  title: string;
  rules: string[];
}

/**
 * Compact info banner showing protocol rules for a page.
 * Usage: <RuleBanner title="Staking Rules" rules={["24h lock", ...]} />
 */
export default function RuleBanner({ title, rules }: RuleBannerProps) {
  return (
    <div className="rounded-lg border border-primary/20 bg-primary/5 px-4 py-3">
      <div className="flex items-start gap-2">
        <Info className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="text-sm">
          <p className="font-semibold text-primary">{title}</p>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            {rules.map((rule, i) => (
              <li key={i}>{rule}</li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
