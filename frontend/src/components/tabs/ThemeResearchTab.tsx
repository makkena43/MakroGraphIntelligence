/**
 * Theme-based Company Research Tab
 *
 * Select any themes from the year focus analysis, then research which companies
 * in those themes actually talked about constraints / demand in their concalls.
 * Returns ranked companies with real quotes from filings.
 */

import { useState, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchYearFocus, runThemeCompanyResearch } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS_LIST = ['2026', '2025', '2024', '2023', '2022', '2021', '2020']

const FC_META: Record<string, { label: string; color: string; bg: string }> = {
  new:        { label: '🆕 New',        color: 'text-emerald-300', bg: 'bg-emerald-950/40' },
  escalating: { label: '⬆️ Escalating', color: 'text-red-300',     bg: 'bg-red-950/40' },
  no_prior:   { label: '❓ No Prior',   color: 'text-amber-300',   bg: 'bg-amber-950/30' },
  persistent: { label: '📍 Persistent', color: 'text-slate-400',   bg: 'bg-slate-800/40' },
  easing:     { label: '⬇️ Easing',     color: 'text-blue-300',    bg: 'bg-blue-950/30' },
  demand_only:{ label: '📈 Demand',     color: 'text-slate-500',   bg: 'bg-slate-900/20' },
}

const SIG_META: Record<string, { bg: string; text: string; label: string }> = {
  supply_bottleneck:     { bg: 'bg-red-900/50',    text: 'text-red-300',    label: 'Supply Bottleneck' },
  inventory_drawdown:    { bg: 'bg-orange-900/40', text: 'text-orange-300', label: 'Inventory Drawdown' },
  capacity_shortage:     { bg: 'bg-rose-900/40',   text: 'text-rose-300',   label: 'Capacity Shortage' },
  demand_exceeds_supply: { bg: 'bg-amber-900/40',  text: 'text-amber-300',  label: 'Demand > Supply' },
  demand_surge:          { bg: 'bg-blue-900/40',   text: 'text-blue-300',   label: 'Demand Surge' },
  capex_increase:        { bg: 'bg-indigo-900/40', text: 'text-indigo-300', label: 'Capex Increase' },
  technology_adoption:   { bg: 'bg-violet-900/40', text: 'text-violet-300', label: 'Tech Adoption' },
}

const ROLE_COLOR: Record<string, string> = {
  supply: 'text-emerald-400', beneficiary: 'text-sky-400', direct: 'text-violet-400',
  supplier: 'text-emerald-400',
}

function CompanyCard({ co, rank }: { co: Record<string, unknown>; rank: number }) {
  const [open, setOpen] = useState(rank <= 5)
  const cSigs  = Number(co.constraint_count ?? 0)
  const dSigs  = Number(co.demand_count ?? 0)
  const total  = Number(co.total_signals ?? 0)
  const role   = String(co.company_role ?? '')
  const quotes = (co.top_quotes as Record<string, unknown>[]) ?? []
  const themes = (co.themes as string[]) ?? []
  const dcRatio = co.dc_ratio != null ? Number(co.dc_ratio) : null

  return (
    <div className={`rounded-xl border overflow-hidden ${
      cSigs > 0 ? 'bg-slate-900/70 border-slate-700' : 'bg-slate-900/40 border-slate-800/50'
    }`} style={{ borderLeft: `4px solid ${cSigs > 0 ? '#ef4444' : '#475569'}` }}>

      <button onClick={() => setOpen(o => !o)}
        className="w-full text-left px-4 py-3 hover:bg-slate-800/30 transition-colors">
        <div className="flex items-start gap-3">
          <span className="text-slate-600 text-xs w-6 pt-0.5">#{rank}</span>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="text-sm font-bold text-slate-100">{String(co.company ?? '')}</span>
              {!!co.ticker && (
                <span className="text-[10px] bg-slate-700 rounded px-1.5 py-0.5 text-slate-300 font-mono">
                  {String(co.ticker)}
                </span>
              )}
              {role && (
                <span className={`text-[10px] font-semibold ${ROLE_COLOR[role] ?? 'text-slate-400'}`}>
                  {role.replace(/_/g, ' ')}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 text-[11px]">
              {cSigs > 0 && <span className="text-red-400 font-bold">⚠️ {cSigs} constraint</span>}
              {dSigs > 0 && <span className="text-blue-400">📈 {dSigs} demand</span>}
              {dcRatio != null && (
                <span className={`font-bold ${dcRatio >= 1.5 ? 'text-red-400' : 'text-slate-400'}`}>
                  {dcRatio.toFixed(1)}× D/C
                </span>
              )}
              {total === 0 && <span className="text-slate-600 italic">No signals in period — supply-chain mapped only</span>}
            </div>
            {themes.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1.5">
                {themes.slice(0, 2).map((tn, i) => (
                  <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-950/50 border border-indigo-800/40 text-indigo-300">
                    {tn.slice(0, 35)}
                  </span>
                ))}
                {themes.length > 2 && <span className="text-[10px] text-slate-600">+{themes.length - 2}</span>}
              </div>
            )}
          </div>
          <div className="flex-shrink-0 text-right">
            <div className={`text-lg font-black ${cSigs > 5 ? 'text-red-400' : cSigs > 0 ? 'text-amber-400' : 'text-slate-600'}`}>
              {cSigs > 0 ? cSigs : '—'}
            </div>
            <div className="text-[9px] text-slate-600">cstr sigs</div>
            <span className="text-slate-600 text-[10px]">{open ? '▼' : '▶'}</span>
          </div>
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 border-t border-slate-800/60 pt-3">
          {quotes.length === 0 ? (
            <p className="text-xs text-slate-600 italic">
              No concall quotes in this period. Company mapped via supply-chain structure (peer filing mentions).
            </p>
          ) : (
            <div className="space-y-2">
              <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wide mb-2">
                📋 Direct evidence from concalls / filings
              </div>
              {quotes.map((q, qi) => {
                const sm = SIG_META[String(q.signal_type ?? '')] ?? { bg: 'bg-slate-800', text: 'text-slate-400', label: String(q.signal_type ?? '') }
                return (
                  <div key={qi} className={`rounded-lg px-3 py-2 ${sm.bg}`}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className={`text-[10px] font-bold ${sm.text}`}>{sm.label}</span>
                      <span className="text-[10px] text-slate-600">{String(q.filed_date ?? '').slice(0, 10)}</span>
                      <span className="text-[10px] text-slate-600 ml-auto">{(Number(q.confidence ?? 0) * 100).toFixed(0)}% conf</span>
                    </div>
                    <p className="text-xs text-slate-300 leading-relaxed italic">
                      "…{String(q.text ?? '').trim()}…"
                    </p>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ThemeResearchTab({ country }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear, setSelectedYear] = useState('2024')
  const [selectedSlugs, setSelectedSlugs] = useState<Set<string>>(new Set())
  const [focusFilter, setFocusFilter]     = useState<string>('actionable')  // 'actionable' | 'all'
  const [researching, setResearching]     = useState(false)
  const [results, setResults]             = useState<Record<string, unknown>[] | null>(null)
  const [roleFilter, setRoleFilter]       = useState('All')

  const yearNum = parseInt(selectedYear)

  const { data: focusThemes = [], isLoading: focusLoading } = useQuery({
    queryKey: ['theme-research-focus', localCountry, selectedYear],
    queryFn: () => fetchYearFocus(localCountry, yearNum),
  })

  const allFocus      = focusThemes as Record<string, unknown>[]
  const actionable    = allFocus.filter(t => t.focus_class === 'new' || t.focus_class === 'escalating')
  const displayThemes = focusFilter === 'actionable' ? actionable : allFocus

  const toggleSlug = (slug: string) =>
    setSelectedSlugs(prev => { const n = new Set(prev); n.has(slug) ? n.delete(slug) : n.add(slug); return n })

  const selectGroup = (themes: Record<string, unknown>[]) =>
    setSelectedSlugs(new Set(themes.map(t => String(t.theme_slug ?? '')).filter(Boolean)))

  const clearAll = () => setSelectedSlugs(new Set())

  // Which slugs will actually be researched
  const effectiveSlugs = selectedSlugs.size > 0
    ? [...selectedSlugs]
    : displayThemes.map(t => String(t.theme_slug ?? '')).filter(Boolean)

  const runResearch = useCallback(async () => {
    if (!effectiveSlugs.length) return
    setResearching(true)
    setResults(null)
    try {
      const data = await runThemeCompanyResearch({
        theme_slugs: effectiveSlugs,
        from_date: `${selectedYear}-01-01`,
        to_date: selectedYear === '2026'
          ? new Date().toISOString().slice(0, 10)
          : `${selectedYear}-12-31`,
        country: localCountry,
        top_n: 50,
      })
      setResults(data as Record<string, unknown>[])
    } catch (e) {
      console.error('Theme research error:', e)
      setResults([])
    } finally {
      setResearching(false)
    }
  }, [effectiveSlugs, selectedYear, localCountry])

  const reset = () => { setResults(null); setSelectedSlugs(new Set()) }

  const displayResults = (results ?? []).filter(r =>
    roleFilter === 'All' || String(r.company_role ?? '') === roleFilter
  )
  const withEvidence = (results ?? []).filter(r => Number(r.constraint_count ?? 0) > 0).length

  return (
    <div className="space-y-4">

      <div>
        <h2 className="text-base font-bold text-slate-100">🔬 Theme-Based Company Research</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          Select themes → research companies in those themes using their actual concall evidence from filings
        </p>
      </div>

      {/* Country + Year */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); reset() }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>
        <div className="flex gap-1.5 flex-wrap">
          {YEARS_LIST.map(y => (
            <button key={y} onClick={() => { setSelectedYear(y); reset() }}
              className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>{y}{y === '2026' ? ' YTD' : ''}</button>
          ))}
        </div>
      </div>

      {/* Theme selector */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-slate-200">Select Themes to Research</span>
            {focusLoading && <Spinner />}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* Show filter */}
            <div className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
              <button onClick={() => { setFocusFilter('actionable'); clearAll() }}
                className={`px-2.5 py-1 ${focusFilter === 'actionable' ? 'bg-slate-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                Actionable only ({actionable.length})
              </button>
              <button onClick={() => { setFocusFilter('all'); clearAll() }}
                className={`px-2.5 py-1 ${focusFilter === 'all' ? 'bg-slate-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                All themes ({allFocus.length})
              </button>
            </div>
            <button onClick={() => selectGroup(displayThemes)} className="text-xs text-indigo-400 hover:text-indigo-300">Select all</button>
            <button onClick={clearAll} className="text-xs text-slate-500 hover:text-slate-300">Clear</button>
          </div>
        </div>

        {!focusLoading && displayThemes.length === 0 && (
          <p className="text-xs text-slate-500 italic py-2">
            No themes found for {selectedYear}. Run the pipeline for this year to build theme data.
          </p>
        )}

        {displayThemes.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 max-h-72 overflow-y-auto pr-1">
            {displayThemes.map(t => {
              const slug = String(t.theme_slug ?? '')
              const isSelected = selectedSlugs.has(slug)
              const fc = String(t.focus_class ?? 'persistent')
              const fcm = FC_META[fc] ?? FC_META.persistent
              const delta = t.delta_pct != null ? Number(t.delta_pct) : null
              return (
                <label key={slug}
                  className={`flex items-start gap-2.5 px-3 py-2 rounded-lg border cursor-pointer transition-all ${
                    isSelected ? 'bg-indigo-950/50 border-indigo-700/60' : 'bg-slate-800/40 border-slate-700/40 hover:border-slate-600'
                  }`}>
                  <input type="checkbox" checked={isSelected} onChange={() => toggleSlug(slug)}
                    className="accent-indigo-500 mt-0.5 flex-shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 mb-0.5 flex-wrap">
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${fcm.color} ${fcm.bg}`}>
                        {fcm.label}
                      </span>
                      {delta != null && (
                        <span className={`text-[10px] font-bold ${delta > 0 ? 'text-red-400' : 'text-blue-400'}`}>
                          {delta > 0 ? '+' : ''}{delta.toFixed(0)}% YoY
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-slate-200 leading-snug">{String(t.theme_name ?? '')}</p>
                  </div>
                </label>
              )
            })}
          </div>
        )}

        <div className="mt-3 flex items-center gap-3 border-t border-slate-800 pt-3 flex-wrap">
          <button
            onClick={runResearch}
            disabled={researching || effectiveSlugs.length === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-bold transition-colors"
          >
            {researching
              ? <><Spinner /> Researching concalls…</>
              : <>🔬 Research {effectiveSlugs.length} theme{effectiveSlugs.length !== 1 ? 's' : ''}</>
            }
          </button>
          {effectiveSlugs.length === 0 && displayThemes.length === 0 && (
            <span className="text-xs text-amber-500">No themes available — select a year with pipeline data</span>
          )}
          {effectiveSlugs.length > 0 && selectedSlugs.size === 0 && (
            <span className="text-xs text-slate-500 italic">
              {displayThemes.length} theme{displayThemes.length !== 1 ? 's' : ''} selected (all shown above)
            </span>
          )}
        </div>
      </div>

      {/* Results */}
      {researching && (
        <div className="flex items-center gap-2 py-8 text-slate-400 text-sm justify-center">
          <Spinner /> Searching concalls and filings for {effectiveSlugs.length} themes…
        </div>
      )}

      {results && !researching && (
        <>
          <div className="flex items-center gap-4 flex-wrap text-xs text-slate-400 bg-slate-800/40 rounded-xl px-4 py-2.5">
            <span><strong className="text-slate-200">{results.length}</strong> companies found</span>
            <span className="text-red-400 font-bold">
              <strong>{withEvidence}</strong> with direct concall evidence
            </span>
            <span className="text-slate-600">·</span>
            <span>{results.length - withEvidence} supply-chain mapped (no direct filing mention)</span>
            <div className="ml-auto flex gap-1">
              {['All', 'supply', 'beneficiary', 'direct'].map(r => (
                <button key={r} onClick={() => setRoleFilter(r)}
                  className={`px-2 py-0.5 rounded text-[11px] capitalize transition-colors ${
                    roleFilter === r ? 'bg-slate-600 text-white' : 'text-slate-500 hover:text-slate-300'
                  }`}>{r}</button>
              ))}
            </div>
          </div>

          {displayResults.length === 0 ? (
            <EmptyState>
              No companies found for selected themes + date range.<br />
              Run the pipeline to build concall signal data for this period.
            </EmptyState>
          ) : (
            <div className="space-y-2">
              {displayResults.map((co, i) => (
                <CompanyCard key={i} co={co} rank={i + 1} />
              ))}
            </div>
          )}
        </>
      )}

      {!results && !researching && allFocus.length > 0 && (
        <div className="text-center py-8 text-slate-500 text-sm border border-dashed border-slate-800 rounded-xl">
          Select themes above and click <strong className="text-slate-300">Research</strong> to pull concall evidence
        </div>
      )}
    </div>
  )
}
