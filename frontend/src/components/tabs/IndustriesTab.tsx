/**
 * Industries Tab
 *
 * Pipeline position:
 *   Events → Themes → Causal Chains → INDUSTRIES → Human Research
 *
 * Ranks industries by:
 *   45% Theme strength aggregate  (sum of theme scores for this industry)
 *   25% Causal chain activation   (how many chains point here + their scores)
 *   20% Signal density            (domain-specific signals in this sector)
 *   10% Company breadth           (distinct companies with confirmed signals)
 *
 * Year selector + own country toggle (defaults to India where historical data exists).
 */

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchIndustries, fetchAISummary, generateIndustrySummary } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [
  { label: 'Live', value: 'live'  },
  { label: '2025', value: '2025'  },
  { label: '2024', value: '2024'  },
  { label: '2023', value: '2023'  },
  { label: '2022', value: '2022'  },
  { label: '2021', value: '2021'  },
  { label: '2020', value: '2020'  },
]

const YEAR_AS_OF: Record<string, string | undefined> = {
  live: undefined, '2025': '2025-12-31', '2024': '2024-12-31',
  '2023': '2023-12-31', '2022': '2022-12-31', '2021': '2021-12-31', '2020': '2020-12-31',
}
const YEAR_FROM: Record<string, string | undefined> = {
  live: undefined, '2025': '2025-01-01', '2024': '2024-01-01',
  '2023': '2023-01-01', '2022': '2022-01-01', '2021': '2021-01-01', '2020': '2020-01-01',
}

/** Returns the previous year key, or null if there's no prior year in our list. */
function prevYear(year: string): string | null {
  const idx = YEARS.findIndex(y => y.value === year)
  // last entry (e.g. '2020') has no prior in our dataset
  if (idx < 0 || idx >= YEARS.length - 1) return null
  return YEARS[idx + 1].value  // YEARS is ordered newest→oldest
}

type Industry = {
  industry: string; icon: string; final_score: number
  theme_score: number; chain_score: number
  constraint_score: number; demand_score: number
  pressure_score: number; avg_conviction: number
  theme_count: number; chain_count: number; company_count: number
  themes: { name: string; score: number; conviction: string; slug: string; snap_date?: string }[]
  chains: { name: string; terminal: string; score: number }[]
  top_companies: { company: string; ticker: string; conviction: number }[]
  constraint_products: string[]
}

function ScoreBar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = Math.min(100, max > 0 ? (value / max) * 100 : 0)
  return (
    <div className="h-1.5 w-full bg-slate-700 rounded-full overflow-hidden">
      <div className={`h-full ${color} rounded-full transition-all`} style={{ width: `${pct}%` }} />
    </div>
  )
}

/** Inline YoY delta badge — green for growth, red for decline, grey for new. */
function Delta({ current, prior }: { current: number; prior: number | undefined }) {
  if (prior === undefined) return <span className="text-[10px] text-slate-600">new</span>
  const diff = Math.round(current - prior)
  if (diff === 0) return <span className="text-[10px] text-slate-500">—</span>
  const up = diff > 0
  return (
    <span className={`text-[10px] font-semibold ${up ? 'text-emerald-400' : 'text-red-400'}`}>
      {up ? '▲' : '▼'} {up ? '+' : ''}{diff}
    </span>
  )
}

const CONV_BADGE: Record<string, string> = {
  high:       'bg-emerald-900/50 text-emerald-300 border-emerald-700/50',
  confirmed:  'bg-blue-900/50 text-blue-300 border-blue-700/50',
  developing: 'bg-amber-900/50 text-amber-300 border-amber-700/50',
  emerging:   'bg-slate-800 text-slate-400 border-slate-600/50',
}

export default function IndustriesTab({ country: _globalCountry }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>('IN')
  const [selectedYear, setSelectedYear] = useState('2021')
  const [selectedInd, setSelectedInd]  = useState<string | null>(null)
  const [detailView, setDetailView]    = useState<'themes' | 'companies' | 'chains'>('themes')

  const queryClient = useQueryClient()
  const as_of     = YEAR_AS_OF[selectedYear]
  const from_date = YEAR_FROM[selectedYear]

  const { data: industries = [], isLoading } = useQuery({
    queryKey: ['industries', localCountry, selectedYear],
    queryFn:  () => fetchIndustries({
      country: localCountry,
      ...(as_of ? { as_of, from_date } : {}),
      top_n: 20,
    }),
  })

  // ── Prior-year data for YoY delta ────────────────────────────────────────
  const prior = prevYear(selectedYear)
  const prior_as_of   = prior ? YEAR_AS_OF[prior]   : undefined
  const prior_from    = prior ? YEAR_FROM[prior]     : undefined
  const { data: priorIndustries = [] } = useQuery({
    queryKey: ['industries', localCountry, prior ?? '__none__'],
    queryFn:  () => fetchIndustries({
      country: localCountry,
      ...(prior_as_of ? { as_of: prior_as_of, from_date: prior_from } : {}),
      top_n: 20,
    }),
    enabled: !!prior,
  })
  // Map: industry name → prior final_score for O(1) delta lookup
  const priorScoreMap = (priorIndustries as Industry[]).reduce<Record<string, number>>(
    (acc, ind) => { acc[ind.industry] = ind.final_score; return acc },
    {}
  )

  const list = industries as Industry[]
  const maxScore = list.length ? list[0].final_score : 1
  const selected = list.find(i => i.industry === selectedInd) ?? null

  // ── AI Industry Summary ───────────────────────────────────────────────────
  const aiKey = ['ai-industry-summary', localCountry, selectedYear]
  const { data: aiData } = useQuery({
    queryKey: aiKey,
    queryFn:  () => fetchAISummary(localCountry, selectedYear, 'industries'),
  })
  const aiSummary = aiData as { summary_text?: string; industry_insights?: Record<string, string>; generated_at?: string } | undefined

  const { mutate: refreshAI, isPending: aiLoading } = useMutation({
    mutationFn: () => generateIndustrySummary(localCountry, selectedYear),
    onSuccess:  () => queryClient.invalidateQueries({ queryKey: aiKey }),
  })

  // Auto-select first industry when data loads (must be in useEffect, not render)
  useEffect(() => {
    if (list.length > 0 && !selectedInd) {
      setSelectedInd(list[0].industry)
    }
  }, [list.length, selectedInd])

  // Reset selection when year or country changes
  useEffect(() => {
    setSelectedInd(null)
  }, [selectedYear, localCountry])

  return (
    <div className="space-y-3 h-full">

      {/* Pipeline breadcrumb */}
      <div className="flex items-center gap-1.5 text-[11px] text-slate-500 flex-wrap">
        {['Events', 'Themes', 'Causal Chains'].map((s, i) => (
          <span key={s} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-slate-700">→</span>}
            <span>{s}</span>
          </span>
        ))}
        <span className="text-slate-700">→</span>
        <span className="text-indigo-400 font-semibold bg-indigo-950/40 border border-indigo-800/40 px-2 py-0.5 rounded">
          🏭 Industries
        </span>
        <span className="text-slate-700">→</span>
        <span>Human Research</span>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 flex-wrap">
        {/* Country */}
        <div className="flex rounded-lg border border-slate-700 overflow-hidden flex-shrink-0">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); setSelectedInd(null) }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>
              {c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}
            </button>
          ))}
        </div>

        <span className="text-xs text-slate-600">|</span>
        <span className="text-xs text-slate-400 font-medium">Year</span>

        {/* Year pills */}
        <div className="flex gap-1.5 flex-wrap">
          {YEARS.map(y => (
            <button key={y.value}
              onClick={() => { setSelectedYear(y.value); setSelectedInd(null) }}
              className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                selectedYear === y.value
                  ? 'bg-indigo-600 border-indigo-500 text-white'
                  : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-600 hover:text-indigo-300'
              }`}>
              {y.label}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2 text-xs text-slate-500">
          {prior && <span className="text-[10px] text-slate-600">▲▼ vs {prior}</span>}
          {isLoading ? 'Loading…' : `${list.length} industries ranked`}
        </div>
      </div>

      {/* AI Industry Ranking Summary */}
      <div className="bg-indigo-950/30 border border-indigo-800/40 rounded-xl p-3">
        <div className="flex items-center justify-between mb-2 gap-2">
          <div className="flex items-center gap-2">
            <span className="text-sm">🤖</span>
            <span className="text-xs font-semibold text-indigo-300">AI Industry Ranking Narrative</span>
            {aiSummary?.generated_at && (
              <span className="text-[10px] text-slate-500">
                Generated {new Date(aiSummary.generated_at).toLocaleDateString()}
              </span>
            )}
          </div>
          <button
            onClick={() => refreshAI()}
            disabled={aiLoading}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-indigo-800/50 border border-indigo-700/50 text-indigo-300 text-xs font-medium hover:bg-indigo-700/60 disabled:opacity-50 transition-colors"
          >
            {aiLoading ? <><Spinner />Generating…</> : '🔄 Refresh AI'}
          </button>
        </div>
        {aiLoading && (
          <p className="text-xs text-slate-400 italic">Generating industry analysis for {localCountry === 'IN' ? 'India' : 'US'} {selectedYear}…</p>
        )}
        {!aiLoading && !aiSummary?.summary_text && (
          <p className="text-xs text-slate-500 italic">No AI analysis yet. Click "Refresh AI" to generate rankings narrative.</p>
        )}
        {!aiLoading && aiSummary?.summary_text && (
          <p className="text-xs text-slate-300 leading-relaxed">{aiSummary.summary_text}</p>
        )}
      </div>

      {/* Main layout */}
      <div className="grid grid-cols-12 gap-4" style={{ minHeight: '68vh' }}>

        {/* LEFT — Industry leaderboard */}
        <div className="col-span-12 lg:col-span-5 flex flex-col gap-1.5 overflow-y-auto pr-1" style={{ maxHeight: '72vh' }}>
          {isLoading && <Spinner />}
          {!isLoading && list.length === 0 && (
            <div className="bg-slate-800/40 border border-slate-700/50 rounded-xl p-4 text-center">
              <div className="text-2xl mb-2">📭</div>
              <p className="text-xs text-slate-400">No industry data for {localCountry} — {selectedYear}</p>
              <p className="text-[11px] text-slate-600 mt-1">Run the pipeline for this year first.</p>
            </div>
          )}

          {list.map((ind, idx) => {
            const isSelected = ind.industry === selectedInd
            const pct = maxScore > 0 ? (ind.final_score / maxScore) * 100 : 0
            const barColor = pct > 75 ? 'bg-emerald-500' : pct > 50 ? 'bg-blue-500' : pct > 30 ? 'bg-amber-500' : 'bg-slate-500'

            return (
              <button key={ind.industry}
                onClick={() => { setSelectedInd(ind.industry); setDetailView('themes') }}
                className={`w-full text-left px-3 py-2.5 rounded-lg border transition-all ${
                  isSelected
                    ? 'bg-indigo-950/60 border-indigo-600 shadow-lg shadow-indigo-900/20'
                    : 'bg-slate-900/60 border-slate-800 hover:border-slate-600 hover:bg-slate-800/40'
                }`}>
                <div className="flex items-center gap-2">
                  {/* Rank */}
                  <span className={`text-xs font-bold w-6 text-center flex-shrink-0 ${
                    idx === 0 ? 'text-amber-400' : idx === 1 ? 'text-slate-300' : idx === 2 ? 'text-amber-600' : 'text-slate-600'
                  }`}>
                    {idx + 1}
                  </span>

                  {/* Icon */}
                  <span className="text-base flex-shrink-0">{ind.icon}</span>

                  {/* Name + score bar */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className={`text-xs font-semibold truncate ${isSelected ? 'text-indigo-200' : 'text-slate-200'}`}>
                        {ind.industry}
                      </span>
                      <div className="flex items-center gap-1.5 flex-shrink-0">
                        <Delta current={ind.final_score} prior={priorScoreMap[ind.industry]} />
                        <span className="text-xs font-bold text-slate-300">
                          {ind.final_score.toFixed(0)}
                        </span>
                      </div>
                    </div>
                    <ScoreBar value={ind.final_score} max={maxScore} color={barColor} />

                    {/* Stats pills */}
                    <div className="flex items-center gap-2 mt-1.5">
                      <span className="text-[10px] text-slate-500">
                        📊 {ind.theme_count} themes
                      </span>
                      <span className="text-[10px] text-slate-500">
                        🔗 {ind.chain_count} chains
                      </span>
                      <span className="text-[10px] text-slate-500">
                        🏭 {ind.company_count} cos
                      </span>
                      {ind.constraint_score > 0 && (
                        <span className="text-[10px] text-red-500/70">
                          ⚠ {ind.constraint_score.toFixed(0)}
                        </span>
                      )}
                      {ind.demand_score > 0 && (
                        <span className="text-[10px] text-emerald-600/70">
                          ↑ {ind.demand_score.toFixed(0)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Constraint products */}
                {isSelected && ind.constraint_products.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-2 pl-8">
                    {ind.constraint_products.map(p => (
                      <span key={p} className="text-[10px] bg-indigo-900/30 border border-indigo-800/40 rounded-full px-2 py-0.5 text-indigo-300">
                        {p}
                      </span>
                    ))}
                  </div>
                )}
              </button>
            )
          })}
        </div>

        {/* RIGHT — Industry detail */}
        <div className="col-span-12 lg:col-span-7 flex flex-col gap-3">
          {selected ? (
            <>
              {/* Header */}
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                <div className="flex items-center gap-3 mb-3">
                  <span className="text-3xl">{selected.icon}</span>
                  <div>
                    <h2 className="text-base font-bold text-slate-100">{selected.industry}</h2>
                    <p className="text-xs text-slate-500 mt-0.5">
                      {selectedYear === 'live' ? 'Live' : selectedYear} · {localCountry === 'IN' ? 'India' : 'US'}
                    </p>
                  </div>
                  <div className="ml-auto text-right">
                    <div className="text-2xl font-bold text-indigo-300">{selected.final_score.toFixed(0)}</div>
                    <div className="flex items-center justify-end gap-1 mt-0.5">
                      <Delta current={selected.final_score} prior={priorScoreMap[selected.industry]} />
                      {prior && <span className="text-[10px] text-slate-600">vs {prior}</span>}
                    </div>
                  </div>
                </div>

                {/* AI per-industry insight */}
                {aiSummary?.industry_insights?.[selected.industry] && (
                  <div className="mb-3 px-3 py-2 bg-indigo-950/40 border border-indigo-800/30 rounded-lg">
                    <p className="text-[11px] text-indigo-300 leading-relaxed">
                      🤖 {aiSummary.industry_insights[selected.industry]}
                    </p>
                  </div>
                )}

                {/* Score breakdown bars */}
                <div className="space-y-2">
                  {/* Row 1: Theme strength + Chain activation */}
                  <div className="grid grid-cols-2 gap-3">
                    {[
                      { label: 'Theme Strength', sublabel: 'conviction-weighted', value: selected.theme_score, max: maxScore * 2, color: 'bg-indigo-500' },
                      { label: 'Chain Activation', sublabel: 'causal evidence',    value: selected.chain_score, max: 1000,          color: 'bg-violet-500' },
                    ].map(b => (
                      <div key={b.label}>
                        <div className="flex justify-between mb-1">
                          <div>
                            <span className="text-[10px] text-slate-400 font-medium">{b.label}</span>
                            <span className="text-[10px] text-slate-600 ml-1">· {b.sublabel}</span>
                          </div>
                          <span className="text-[10px] text-slate-400">{b.value.toFixed(0)}</span>
                        </div>
                        <ScoreBar value={b.value} max={b.max} color={b.color} />
                      </div>
                    ))}
                  </div>
                  {/* Row 2: Supply constraint pressure + Demand pull (split) */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <div className="flex justify-between mb-1">
                        <div>
                          <span className="text-[10px] text-red-400 font-medium">Supply Constraint</span>
                          <span className="text-[10px] text-slate-600 ml-1">· bottleneck severity</span>
                        </div>
                        <span className="text-[10px] text-slate-400">{selected.constraint_score.toFixed(0)}</span>
                      </div>
                      <ScoreBar value={selected.constraint_score} max={Math.max(...list.map(i => i.constraint_score), 1)} color="bg-red-500" />
                    </div>
                    <div>
                      <div className="flex justify-between mb-1">
                        <div>
                          <span className="text-[10px] text-emerald-400 font-medium">Demand Pull</span>
                          <span className="text-[10px] text-slate-600 ml-1">· growth signals</span>
                        </div>
                        <span className="text-[10px] text-slate-400">{selected.demand_score.toFixed(0)}</span>
                      </div>
                      <ScoreBar value={selected.demand_score} max={Math.max(...list.map(i => i.demand_score), 1)} color="bg-emerald-500" />
                    </div>
                  </div>
                  {/* Row 3: Avg conviction quality */}
                  <div>
                    <div className="flex justify-between mb-1">
                      <div>
                        <span className="text-[10px] text-amber-400 font-medium">Beneficiary Conviction</span>
                        <span className="text-[10px] text-slate-600 ml-1">· avg company confidence</span>
                      </div>
                      <span className="text-[10px] text-slate-400">{selected.avg_conviction.toFixed(0)}</span>
                    </div>
                    <ScoreBar value={selected.avg_conviction} max={100} color="bg-amber-500" />
                  </div>
                </div>
              </div>

              {/* Detail sub-tabs */}
              <div className="flex gap-1">
                {([
                  { id: 'themes', label: `📊 Themes (${selected.theme_count})` },
                  { id: 'companies', label: `🏭 Companies (${selected.company_count})` },
                  { id: 'chains', label: `🔗 Chains (${selected.chain_count})` },
                ] as const).map(dt => (
                  <button key={dt.id}
                    onClick={() => setDetailView(dt.id)}
                    className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                      detailView === dt.id
                        ? 'bg-slate-800 border-indigo-600 text-indigo-300'
                        : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'
                    }`}>
                    {dt.label}
                  </button>
                ))}
              </div>

              {/* Themes view */}
              {detailView === 'themes' && (
                <div className="flex-1 space-y-1.5 overflow-y-auto" style={{ maxHeight: '40vh' }}>
                  {selected.themes.length === 0 && <EmptyState>No themes.</EmptyState>}
                  {selected.themes.map((t, ti) => (
                    <div key={ti} className="bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-slate-600 w-4 flex-shrink-0">{ti + 1}</span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-slate-200 truncate">{t.name}</p>
                          <div className="flex items-center gap-2 mt-1">
                            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${
                              CONV_BADGE[t.conviction.toLowerCase()] ?? CONV_BADGE.emerging
                            }`}>
                              {t.conviction}
                            </span>
                            {t.snap_date && (
                              <span className="text-[10px] text-slate-600">@ {t.snap_date.slice(0,7)}</span>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-1.5 flex-shrink-0">
                          <div className="h-1.5 w-16 bg-slate-700 rounded-full overflow-hidden">
                            <div className="h-full bg-indigo-500 rounded-full"
                              style={{ width: `${Math.min(100, (t.score / 200) * 100)}%` }} />
                          </div>
                          <span className="text-xs font-semibold text-slate-300 w-8 text-right">
                            {t.score.toFixed(0)}
                          </span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Companies view */}
              {detailView === 'companies' && (
                <div className="flex-1 space-y-1.5 overflow-y-auto" style={{ maxHeight: '40vh' }}>
                  {selected.top_companies.length === 0 && <EmptyState>No companies mapped.</EmptyState>}
                  {selected.top_companies.map((c, ci) => (
                    <div key={ci} className="flex items-center gap-3 bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2">
                      <span className="text-xs text-slate-600 w-4 flex-shrink-0">{ci + 1}</span>
                      <div className="flex-1 min-w-0">
                        <span className="text-xs font-medium text-slate-200 truncate block">{c.company}</span>
                      </div>
                      {c.ticker && (
                        <span className="text-[10px] bg-slate-700 rounded px-1.5 text-slate-400 flex-shrink-0">
                          {c.ticker}
                        </span>
                      )}
                      <div className="flex items-center gap-1.5 flex-shrink-0">
                        <div className="h-1.5 w-12 bg-slate-700 rounded-full overflow-hidden">
                          <div className="h-full bg-emerald-500 rounded-full"
                            style={{ width: `${Math.min(100, c.conviction * 100)}%` }} />
                        </div>
                        <span className="text-[10px] text-slate-500">{(c.conviction * 100).toFixed(0)}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Chains view */}
              {detailView === 'chains' && (
                <div className="flex-1 space-y-2 overflow-y-auto" style={{ maxHeight: '40vh' }}>
                  {selected.chains.length === 0 && <EmptyState>No causal chains for this industry.</EmptyState>}
                  {selected.chains.map((ch, chi) => (
                    <div key={chi} className="bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2.5">
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-slate-200">{ch.name}</p>
                          {ch.terminal && (
                            <p className="text-[10px] text-slate-500 mt-1">
                              Terminal: <span className="text-amber-400">{ch.terminal}</span>
                            </p>
                          )}
                        </div>
                        <div className="flex items-center gap-1 flex-shrink-0">
                          <div className="h-1.5 w-12 bg-slate-700 rounded-full overflow-hidden">
                            <div className="h-full bg-emerald-500 rounded-full"
                              style={{ width: `${Math.min(100, (ch.score / 100) * 100)}%` }} />
                          </div>
                          <span className="text-[10px] text-slate-500">{ch.score.toFixed(0)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="flex-1 bg-slate-900/30 border border-slate-800/50 rounded-xl flex items-center justify-center">
              <div className="text-center">
                <div className="text-4xl mb-2">🏭</div>
                <p className="text-sm text-slate-500">Select an industry to explore</p>
                <p className="text-xs text-slate-600 mt-1">Themes · Companies · Causal chains</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
