"use client"

import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Tabs / segmented control.
 *
 * Previous versions targeted `data-horizontal` / `data-vertical`, but Base UI
 * sets `data-orientation="horizontal"|"vertical"` -- so none of those styles
 * fired. The list rendered as a thin sidebar next to the panel, which made
 * pages like Leaderboard, Predictions and Admin practically unusable on
 * narrow viewports. We now key off `[data-orientation=...]` directly.
 *
 * The horizontal layout also wraps when the trigger row is wider than its
 * container so labels can never collapse into a thin unreadable column.
 */

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: TabsPrimitive.Root.Props) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      // Pass the prop (Base UI's canonical API) AND the data attribute
      // explicitly. Setting `data-orientation` directly guarantees our
      // CSS attribute selectors fire even if a future Base UI release
      // changes how internal data attributes are emitted.
      orientation={orientation}
      data-orientation={orientation}
      className={cn(
        // Stack list-then-panel for horizontal; list-on-the-side for vertical.
        "group/tabs flex gap-4",
        "data-[orientation=horizontal]:flex-col",
        "data-[orientation=vertical]:flex-row",
        className
      )}
      {...props}
    />
  )
}

// All orientation-keyed styles target the Root via the `tabs` group instead
// of self-attributes. Base UI doesn't reliably propagate `data-orientation`
// from <Tabs.Root> down to <Tabs.List>, so a `data-[orientation=...]:` class
// on the list element can silently fail to match. `group-data-[...]/tabs:`
// always works because the Root carries the attribute we set ourselves.
const tabsListVariants = cva(
  cn(
    // Base
    "group/tabs-list relative text-muted-foreground",
    // Horizontal: a wrapping pill row that uses full width.
    "group-data-[orientation=horizontal]/tabs:flex group-data-[orientation=horizontal]/tabs:w-full",
    "group-data-[orientation=horizontal]/tabs:flex-wrap group-data-[orientation=horizontal]/tabs:gap-1",
    "group-data-[orientation=horizontal]/tabs:items-center",
    // Vertical: a column anchored to the start, fixed-fit width.
    "group-data-[orientation=vertical]/tabs:inline-flex group-data-[orientation=vertical]/tabs:w-fit",
    "group-data-[orientation=vertical]/tabs:flex-col group-data-[orientation=vertical]/tabs:gap-1"
  ),
  {
    variants: {
      variant: {
        // Pill-style segmented control. Each trigger is a self-contained pill;
        // wrapping is supported because triggers are individual flex items
        // rather than living inside a fixed-height bar.
        default:
          "group-data-[orientation=horizontal]/tabs:rounded-xl group-data-[orientation=horizontal]/tabs:border group-data-[orientation=horizontal]/tabs:border-border/60 group-data-[orientation=horizontal]/tabs:bg-muted/40 group-data-[orientation=horizontal]/tabs:p-1",
        // Underline-only variant for tighter layouts.
        line:
          "group-data-[orientation=horizontal]/tabs:border-b group-data-[orientation=horizontal]/tabs:border-border/60 group-data-[orientation=horizontal]/tabs:rounded-none group-data-[orientation=horizontal]/tabs:p-0",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function TabsList({
  className,
  variant = "default",
  ...props
}: TabsPrimitive.List.Props & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: TabsPrimitive.Tab.Props) {
  return (
    <TabsPrimitive.Tab
      data-slot="tabs-trigger"
      className={cn(
        // Layout
        "relative inline-flex items-center justify-center gap-1.5 whitespace-nowrap select-none",
        "px-3 py-1.5 text-sm font-medium",
        // Default state
        "rounded-lg text-muted-foreground transition-colors",
        "hover:text-foreground",
        // Focus
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        // Active (default variant: filled pill). Base UI emits `data-active`
        // when the tab is selected -- not `data-state="active"` -- so we key
        // off that single attribute consistently.
        "group-data-[variant=default]/tabs-list:data-active:bg-background",
        "group-data-[variant=default]/tabs-list:data-active:text-foreground",
        "group-data-[variant=default]/tabs-list:data-active:shadow-sm",
        "group-data-[variant=default]/tabs-list:data-active:border",
        "group-data-[variant=default]/tabs-list:data-active:border-border/60",
        // Active (line variant: underline)
        "group-data-[variant=line]/tabs-list:rounded-none",
        "group-data-[variant=line]/tabs-list:border-b-2 group-data-[variant=line]/tabs-list:border-transparent",
        "group-data-[variant=line]/tabs-list:data-active:border-foreground",
        "group-data-[variant=line]/tabs-list:data-active:text-foreground",
        // Vertical orientation: fill the column.
        "group-data-[orientation=vertical]/tabs:w-full",
        "group-data-[orientation=vertical]/tabs:justify-start",
        // Disabled
        "disabled:pointer-events-none disabled:opacity-50",
        "aria-disabled:pointer-events-none aria-disabled:opacity-50",
        // Icon sizing
        "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: TabsPrimitive.Panel.Props) {
  return (
    <TabsPrimitive.Panel
      data-slot="tabs-content"
      className={cn(
        // Use full available width inside the Tabs flex container.
        "min-w-0 flex-1 text-sm outline-none",
        className
      )}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
