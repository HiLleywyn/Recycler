"use client";

import { useModules } from "@/hooks/useModules";
import { AlertCircle } from "lucide-react";

interface ModuleGateProps {
  /** One or more module keys — page renders if ANY is enabled (OR logic). */
  modules: string[];
  children: React.ReactNode;
}

/**
 * Wrap a dashboard page with this component to hide it when the
 * required guild module(s) are all disabled.
 *
 * Usage:
 *   <ModuleGate modules={["gambling", "games"]}>
 *     <GamesPageContent />
 *   </ModuleGate>
 */
export default function ModuleGate({ modules, children }: ModuleGateProps) {
  const { isEnabled, loading } = useModules();

  if (loading) return null; // still fetching — don't flash disabled state

  if (!isEnabled(...modules)) {
    const label = modules[0].replace(/_/g, " ");
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-24 text-muted-foreground">
        <AlertCircle className="h-12 w-12" />
        <h2 className="text-xl font-semibold">Module Disabled</h2>
        <p className="text-sm">
          The <span className="font-medium capitalize">{label}</span> module is
          disabled on this server.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
