import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchKPIs } from './api'
import Sidebar from './components/Sidebar'
import KPIBar from './components/KPIBar'
import PipelineTab from './components/tabs/PipelineTab'
import FilingsTab from './components/tabs/FilingsTab'
import ThemesTab from './components/tabs/ThemesTab'
import ShortlistedTab from './components/tabs/ShortlistedTab'
import RankingTab from './components/tabs/RankingTab'
import AITab from './components/tabs/AITab'
import MacroTab from './components/tabs/MacroTab'
import CompanyTab from './components/tabs/CompanyTab'

const TABS = [
  { id: 'pipeline',    label: '🚀 Pipeline Runner' },
  { id: 'filings',     label: '📞 Concall & Filings' },
  { id: 'themes',      label: '🗺️ Themes & Companies' },
  { id: 'shortlisted', label: '⭐ Shortlisted Themes' },
  { id: 'ranking',     label: '🏆 Stock Rankings' },
  { id: 'ai',          label: '🤖 AI Analysis' },
  { id: 'macro',       label: '🌐 Macro & Policy' },
  { id: 'company',     label: '🏢 Company Explorer' },
]

export default function App() {
  const [country, setCountry] = useState<'US' | 'IN'>('US')
  const [activeTab, setActiveTab] = useState('pipeline')
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const { data: kpis } = useQuery({
    queryKey: ['kpis', country],
    queryFn: () => fetchKPIs(country),
    refetchInterval: 30_000,
  })

  const countryFlag = country === 'US' ? '🇺🇸' : '🇮🇳'
  const countryLabel = country === 'US' ? 'USA 🇺🇸' : 'India 🇮🇳'

  return (
    <div className="flex h-screen overflow-hidden bg-slate-950">
      <Sidebar
        country={country}
        setCountry={setCountry}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        kpis={kpis}
        open={sidebarOpen}
        setOpen={setSidebarOpen}
      />

      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <header className="flex-shrink-0 bg-slate-950 border-b border-slate-800 px-4 py-3">
          <div className="flex items-center gap-3">
            <button
              className="text-slate-400 hover:text-slate-200 lg:hidden"
              onClick={() => setSidebarOpen(true)}
            >
              ☰
            </button>
            <div>
              <h1 className="text-lg font-bold text-slate-100 leading-none">
                📊 MakroGraph Intelligence
              </h1>
              <p className="text-xs text-slate-500 mt-0.5">
                Event-Centric Macro Research Platform
              </p>
            </div>
            <div className="ml-auto text-xs text-slate-500">
              {countryFlag} {countryLabel}
            </div>
          </div>

          {/* KPI bar */}
          <KPIBar kpis={kpis} />

          {/* Tab bar */}
          <div className="flex gap-0.5 mt-3 overflow-x-auto pb-0.5 scrollbar-none">
            {TABS.map(t => (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`flex-shrink-0 px-3 py-1.5 text-xs font-medium rounded-t-lg border-b-2 transition-colors whitespace-nowrap ${
                  activeTab === t.id
                    ? 'border-indigo-500 text-indigo-400 bg-slate-800'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </header>

        {/* Tab content */}
        <main className="flex-1 overflow-y-auto p-4">
          {activeTab === 'pipeline'    && <PipelineTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'filings'     && <FilingsTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'themes'      && <ThemesTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'shortlisted' && <ShortlistedTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'ranking'     && <RankingTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'ai'          && <AITab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'macro'       && <MacroTab key={country} country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
          {activeTab === 'company'     && <CompanyTab country={country} countryFlag={countryFlag} countryLabel={countryLabel} />}
        </main>
      </div>
    </div>
  )
}
