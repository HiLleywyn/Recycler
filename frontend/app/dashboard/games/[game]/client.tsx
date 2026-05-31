"use client";

import { Card, CardContent } from "@/components/ui/card";
import { buttonVariants } from "@/components/ui/button";
import { ArrowLeft, Gamepad2 } from "lucide-react";
import { cn } from "@/lib/utils";
import Link from "next/link";

export default function GamePageClient() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/dashboard/games"
          className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
        >
          <ArrowLeft className="size-4" />
        </Link>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Games</h1>
          <p className="text-sm text-muted-foreground">
            Currently unavailable
          </p>
        </div>
      </div>

      <Card>
        <CardContent className="flex flex-col items-center justify-center py-16 text-center">
          <Gamepad2 className="mb-4 size-12 text-muted-foreground/50" />
          <h2 className="text-lg font-semibold">Games are temporarily disabled</h2>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            Games are currently undergoing maintenance and will be back soon.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
