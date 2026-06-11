interface SidebarProps {
  country: 'US' | 'IN'
  setCountry: (c: 'US' | 'IN') => void
  activeTab: string
  setActiveTab: (t: string) => void
  kpis?: Record<string, number>
  open: boolean
  setOpen: (v: boolean) => void
}

const NAV_ITEMS = [
  { id: 'pipeline',    icon: '🚀', label: 'Pipeline Runner' },
  { id: 'filings',     icon: '📞', label: 'Concall Analysis' },
  { id: 'themes',      icon: '🗺️',  label: 'Themes & Companies' },
  { id: 'shortlisted', icon: '⭐', label: 'Shortlisted Themes' },
  { id: 'ranking',     icon: '🏆', label: 'Stock Rankings' },
  { id: 'ai',          icon: '🤖', label: 'AI Analysis' },
  { id: 'macro',       icon: '🌐', label: 'Macro & Policy' },
  { id: 'company',     icon: '🏢', label: 'Company Explorer' },
]

export default function Sidebar({
  country, setCountry, activeTab, setActiveTab, kpis, open, setOpen,
}: SidebarProps) {
  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 bg-black/60 z-20 lg:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      <aside className={`
        fixed lg:static inset-y-0 left-0 z-30 w-56 flex-shrink-0
        bg-slate-900 border-r border-slate-800 flex flex-col
        transform transition-transform duration-200
        ${open ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
      `}>
        {/* Logo */}
        <div className="px-4 py-4 border-b border-slate-800">
          <div className="text-indigo-400 font-bold text-base leading-none">📊 MakroGraph</div>
          <div className="text-slate-500 text-xs mt-1">Intelligence Platform</div>
        </div>

        {/* Country selector */}
        <div className="px-4 py-3 border-b border-slate-800">
          <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-2">
            🌍 Market
          </div>
          <div className="flex flex-col gap-1">
            {(['US', 'IN'] as const).map(c => (
              <button
                key={c}
                onClick={() => setCountry(c)}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${
                  country === c
                    ? 'bg-indigo-600/20 border border-indigo-600/40 text-indigo-300'
                    : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                }`}
              >
                <span>{c === 'US' ? '🇺🇸' : '🇮🇳'}</span>
                <span>{c === 'US' ? 'USA' : 'India'}</span>
              </button>
            ))}
          </div>
          {country === 'IN' && (
            <div className="mt-2 text-xs text-emerald-400 bg-emerald-400/10 rounded-lg p-2">
              🇮🇳 India pipeline active — NSE · BSE · Screener
            </div>
          )}
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-2 py-3 overflow-y-auto">
          <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-2 px-2">
            📍 Navigation
          </div>
          {NAV_ITEMS.map(item => (
            <button
              key={item.id}
              onClick={() => { setActiveTab(item.id); setOpen(false) }}
              className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm mb-0.5 transition-colors text-left ${
                activeTab === item.id
                  ? 'bg-indigo-600/20 text-indigo-300 font-medium'
                  : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
              }`}
            >
              <span className="text-base">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        {/* DB Stats */}
        {kpis && Object.keys(kpis).length > 0 && (
          <div className="px-4 py-3 border-t border-slate-800">
            <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-2">
              📊 Database
            </div>
            {[
              ['Docs', kpis.total_docs],
              ['Signals', kpis.total_signals],
              ['Themes', kpis.active_themes],
              ['Chains', kpis.active_chains],
            ].map(([label, val]) => (
              <div key={String(label)} className="flex justify-between text-xs py-0.5">
                <span className="text-slate-500">{label}</span>
                <span className="text-indigo-400 font-bold">{(val as number)?.toLocaleString() ?? 0}</span>
              </div>
            ))}
          </div>
        )}

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800">
          <div className="text-xs text-slate-600 text-center">
            v0.2.0 · {new Date().toLocaleDateString()}
          </div>
        </div>
      </aside>
    </>
  )
}
