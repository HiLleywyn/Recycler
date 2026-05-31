"use client"

import * as React from "react"
import { ArrowUp, ArrowDown, Search, SlidersHorizontal } from "lucide-react"
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableCell,
  TableHead,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

export interface ColumnDef<T> {
  key: string
  label: string
  sortable?: boolean
  render?: (row: T) => React.ReactNode
  visible?: boolean
  className?: string
}

interface SortState {
  key: string
  dir: "asc" | "desc"
}

interface SortableTableProps<T> {
  columns: ColumnDef<T>[]
  data: T[]
  defaultSort?: { key: string; dir: "asc" | "desc" }
  searchable?: boolean
  searchPlaceholder?: string
  columnToggle?: boolean
  emptyMessage?: string
}

export function SortableTable<T extends Record<string, unknown>>({
  columns,
  data,
  defaultSort,
  searchable = false,
  searchPlaceholder = "Search...",
  columnToggle = false,
  emptyMessage = "No results found.",
}: SortableTableProps<T>) {
  const [sort, setSort] = React.useState<SortState | null>(
    defaultSort ?? null
  )
  const [query, setQuery] = React.useState("")
  const [visibilityMap, setVisibilityMap] = React.useState<
    Record<string, boolean>
  >(() => {
    const map: Record<string, boolean> = {}
    for (const col of columns) {
      map[col.key] = col.visible !== false
    }
    return map
  })
  const [dropdownOpen, setDropdownOpen] = React.useState(false)
  const dropdownRef = React.useRef<HTMLDivElement>(null)

  // Close dropdown on outside click
  React.useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setDropdownOpen(false)
      }
    }
    if (dropdownOpen) {
      document.addEventListener("mousedown", handleClick)
      return () => document.removeEventListener("mousedown", handleClick)
    }
  }, [dropdownOpen])

  const visibleColumns = columns.filter((col) => visibilityMap[col.key])

  // Filter rows by search query across all visible column string values
  const filteredData = React.useMemo(() => {
    if (!query.trim()) return data
    const q = query.toLowerCase()
    return data.filter((row) =>
      visibleColumns.some((col) => {
        const val = row[col.key]
        if (val == null) return false
        return String(val).toLowerCase().includes(q)
      })
    )
  }, [data, query, visibleColumns])

  // Sort filtered rows
  const sortedData = React.useMemo(() => {
    if (!sort) return filteredData
    const { key, dir } = sort
    return [...filteredData].sort((a, b) => {
      const aVal = a[key]
      const bVal = b[key]
      if (aVal == null && bVal == null) return 0
      if (aVal == null) return 1
      if (bVal == null) return -1
      if (typeof aVal === "number" && typeof bVal === "number") {
        return dir === "asc" ? aVal - bVal : bVal - aVal
      }
      const aStr = String(aVal)
      const bStr = String(bVal)
      const cmp = aStr.localeCompare(bStr)
      return dir === "asc" ? cmp : -cmp
    })
  }, [filteredData, sort])

  function handleSort(key: string) {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "asc" }
      if (prev.dir === "asc") return { key, dir: "desc" }
      return null
    })
  }

  function toggleColumnVisibility(key: string) {
    setVisibilityMap((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      {(searchable || columnToggle) && (
        <div className="flex items-center gap-2">
          {searchable && (
            <div className="relative flex-1 max-w-sm">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={searchPlaceholder}
                className="w-full rounded-md border border-input bg-background py-1.5 pl-8 pr-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
          )}
          {columnToggle && (
            <div className="relative" ref={dropdownRef}>
              <button
                type="button"
                onClick={() => setDropdownOpen((o) => !o)}
                className="inline-flex items-center gap-1.5 rounded-md border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent hover:text-accent-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <SlidersHorizontal className="h-4 w-4" />
                Columns
              </button>
              {dropdownOpen && (
                <div className="absolute right-0 z-50 mt-1 w-48 rounded-md border border-input bg-popover p-1.5 shadow-md">
                  {columns.map((col) => (
                    <label
                      key={col.key}
                      className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-accent"
                    >
                      <input
                        type="checkbox"
                        checked={visibilityMap[col.key]}
                        onChange={() => toggleColumnVisibility(col.key)}
                        className="h-3.5 w-3.5 rounded border-input"
                      />
                      {col.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Table */}
      <Table>
        <TableHeader>
          <TableRow>
            {visibleColumns.map((col) => (
              <TableHead
                key={col.key}
                className={cn(
                  col.sortable !== false && "cursor-pointer select-none",
                  col.className
                )}
                onClick={
                  col.sortable !== false
                    ? () => handleSort(col.key)
                    : undefined
                }
              >
                <span className="inline-flex items-center gap-1">
                  {col.label}
                  {col.sortable !== false && sort?.key === col.key && (
                    <span className="text-muted-foreground">
                      {sort.dir === "asc" ? (
                        <ArrowUp className="h-3.5 w-3.5" />
                      ) : (
                        <ArrowDown className="h-3.5 w-3.5" />
                      )}
                    </span>
                  )}
                </span>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {sortedData.length === 0 ? (
            <TableRow>
              <TableCell
                colSpan={visibleColumns.length}
                className="h-24 text-center text-muted-foreground"
              >
                {emptyMessage}
              </TableCell>
            </TableRow>
          ) : (
            sortedData.map((row, i) => (
              <TableRow key={i}>
                {visibleColumns.map((col) => (
                  <TableCell key={col.key} className={col.className}>
                    {col.render
                      ? col.render(row)
                      : (row[col.key] as React.ReactNode) ?? "—"}
                  </TableCell>
                ))}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  )
}
