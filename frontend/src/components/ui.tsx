import React from 'react'

// ─── Badge ────────────────────────────────────────────────────────────────────
interface BadgeProps {
  text: string
  color?: string
  className?: string
}
export function Badge({ text, color, className = '' }: BadgeProps) {
  const style = color ? { background: color, color: '#fff' } : undefined
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-bold ${className}`}
      style={style}
    >
      {text}
    </span>
  )
}

// ─── Conviction badge ─────────────────────────────────────────────────────────
const CONVICTION_COLOR: Record<string, string> = {
  confirmed:  '#22c55e',
  developing: '#f59e0b',
  emerging:   '#6366f1',
}
export function ConvictionBadge({ conviction }: { conviction: string }) {
  return (
    <Badge
      text={conviction.toUpperCase()}
      color={CONVICTION_COLOR[conviction.toLowerCase()] ?? '#6366f1'}
    />
  )
}

// ─── Country Banner ───────────────────────────────────────────────────────────
export function CountryBanner({
  flag, label, children,
}: { flag: string; label: string; children?: React.ReactNode }) {
  return (
    <div className="bg-indigo-950 border-l-4 border-indigo-500 rounded-r-lg px-4 py-2 mb-4 text-sm text-indigo-200">
      {flag} <strong>{label}</strong> — {children ?? 'change market in the sidebar ←'}
    </div>
  )
}

// ─── Section Header ───────────────────────────────────────────────────────────
export function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-base font-bold text-slate-100 mb-3">{children}</h3>
  )
}

// ─── Empty state ─────────────────────────────────────────────────────────────
export function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-slate-800 border border-dashed border-slate-600 rounded-xl p-8 text-center text-slate-500 text-sm">
      {children}
    </div>
  )
}

// ─── Loading spinner ──────────────────────────────────────────────────────────
export function Spinner({ size = 'md' }: { size?: 'sm' | 'md' | 'lg' }) {
  const cls = size === 'sm' ? 'w-4 h-4' : size === 'lg' ? 'w-8 h-8' : 'w-6 h-6'
  return (
    <div className={`${cls} border-2 border-slate-700 border-t-indigo-500 rounded-full animate-spin`} />
  )
}

// ─── Signal direction colors ──────────────────────────────────────────────────
export const DIR_COLOR: Record<string, string> = {
  increasing: '#22c55e', positive: '#22c55e',
  decreasing: '#ef4444', negative: '#ef4444',
  neutral:    '#94a3b8', stable:   '#94a3b8',
}

// ─── Percent bar ──────────────────────────────────────────────────────────────
export function PctBar({ value, max = 100 }: { value: number; max?: number }) {
  const pct = Math.min(100, (value / Math.max(max, 1)) * 100)
  return (
    <div className="bg-slate-700 rounded h-1.5 w-full overflow-hidden">
      <div className="bg-indigo-400 h-full rounded" style={{ width: `${pct}%` }} />
    </div>
  )
}

// ─── Table ───────────────────────────────────────────────────────────────────
export function Table({
  columns,
  rows,
  onRowClick,
  selectedIdx,
}: {
  columns: { key: string; label: string; width?: string }[]
  rows: Record<string, unknown>[]
  onRowClick?: (idx: number) => void
  selectedIdx?: number
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-700">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-slate-800 border-b border-slate-700">
            {columns.map(c => (
              <th
                key={c.key}
                className={`px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide ${c.width ?? ''}`}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={i}
              onClick={() => onRowClick?.(i)}
              className={`border-b border-slate-800 transition-colors ${
                onRowClick ? 'cursor-pointer' : ''
              } ${
                selectedIdx === i
                  ? 'bg-indigo-950 border-l-2 border-l-indigo-500'
                  : 'hover:bg-slate-800/60'
              }`}
            >
              {columns.map(c => (
                <td key={c.key} className="px-3 py-2 text-slate-300 whitespace-nowrap">
                  {String(row[c.key] ?? '—')}
                </td>
              ))}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-6 text-center text-slate-600">
                No data
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

// ─── Freshness pill ───────────────────────────────────────────────────────────
export function FreshnessPill({ freshness }: { freshness: string }) {
  const map: Record<string, string> = {
    Fresh:   'bg-emerald-900/40 text-emerald-400',
    Active:  'bg-yellow-900/40 text-yellow-400',
    Mature:  'bg-red-900/40 text-red-400',
    Unknown: 'bg-slate-800 text-slate-500',
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${map[freshness] ?? map.Unknown}`}>
      {freshness}
    </span>
  )
}
