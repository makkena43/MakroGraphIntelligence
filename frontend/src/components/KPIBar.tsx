interface KPIBarProps {
  kpis?: Record<string, number>
}

const KPI_DEFS = [
  { key: 'total_docs',      label: 'Docs' },
  { key: 'total_entities',  label: 'Entities' },
  { key: 'total_signals',   label: 'Signals' },
  { key: 'active_themes',   label: 'Themes' },
  { key: 'total_events',    label: 'Events' },
  { key: 'active_chains',   label: 'Causal Chains' },
  { key: 'replay_runs',     label: 'Replay Runs' },
]

export default function KPIBar({ kpis }: KPIBarProps) {
  if (!kpis) return null
  return (
    <div className="flex gap-2 mt-3 overflow-x-auto pb-1">
      {KPI_DEFS.map(({ key, label }) => (
        <div
          key={key}
          className="flex-shrink-0 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-center min-w-[80px]"
        >
          <div className="text-lg font-black text-indigo-400 leading-none">
            {(kpis[key] ?? 0).toLocaleString()}
          </div>
          <div className="text-xs text-slate-500 mt-1">{label}</div>
        </div>
      ))}
    </div>
  )
}
