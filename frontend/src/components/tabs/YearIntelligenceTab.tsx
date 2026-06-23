/**
 * Year Intelligence Tab — Year-over-Year Focus Dashboard
 *
 * Primary signal: what CHANGED this year vs last year.
 * Persistent/structural themes are known and priced-in.
 * Only NEW and ESCALATING themes are investment signals.
 *
 * Focus classes (purely data-driven, no human judgment):
 *   🆕 NEW        — first constraint evidence this year (no prior-year baseline)
 *   ⬆️ ESCALATING — constraint signals grew ≥50% vs prior year
 *   📍 PERSISTENT — same level as prior year (structural, priced-in, skip)
 *   ⬇️ EASING     — constraint signals fell ≥25% (supply resolving, exit signal)
 */

import { useState, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchYearFocus, fetchThemes, fetchCausalChains, fetchBeneficiaries,
  fetchSnapshots, fetchSourceCompanies,
  fetchAISummary, generateYearSummary,
  runYearRankings, fetchConstraintComponents,
} from '../../api'
import { Spinner, EmptyState, SectionHeader } from '../ui'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [
  { label: 'Live',      value: 'live' },
  { label: '2026 YTD', value: '2026' },
  { label: '2025',     value: '2025' },
  { label: '2024',     value: '2024' },
  { label: '2023',     value: '2023' },
  { label: '2022',     value: '2022' },
  { label: '2021',     value: '2021' },
  { label: '2020',     value: '2020' },
]

// For closed historical years: fixed end date.
// For 2026 (current year): computed fresh at query time so it's always "today".
const YEAR_AS_OF: Record<string, string | undefined> = {
  live: undefined,
  '2026': undefined,      // resolved dynamically in component
  '2025': '2025-12-31', '2024': '2024-12-31', '2023': '2023-12-31',
  '2022': '2022-12-31', '2021': '2021-12-31', '2020': '2020-12-31',
}
const YEAR_FROM: Record<string, string | undefined> = {
  live: undefined,
  '2026': '2026-01-01',
  '2025': '2025-01-01', '2024': '2024-01-01', '2023': '2023-01-01',
  '2022': '2022-01-01', '2021': '2021-01-01', '2020': '2020-01-01',
}

// Returns the as_of ceiling for a given year selection.
// For the current/open year (2026) this is today's date, computed fresh each call.
function resolveAsOf(year: string): string | undefined {
  if (year === 'live') return undefined
  if (year === '2026') return new Date().toISOString().slice(0, 10)
  return YEAR_AS_OF[year]
}

type FocusClass = 'new' | 'escalating' | 'no_prior' | 'persistent' | 'easing' | 'demand_only'

const FOCUS_META: Record<FocusClass, {
  label: string; short: string; color: string; bg: string; border: string; leftBar: string
}> = {
  new:        { label: '🆕 New Theme',        short: 'NEW',       color: 'text-emerald-300', bg: 'bg-emerald-950/50', border: 'border-emerald-700/60', leftBar: '#10b981' },
  escalating: { label: '⬆️ Escalating',       short: 'ESCALATING',color: 'text-red-300',     bg: 'bg-red-950/50',     border: 'border-red-700/60',     leftBar: '#ef4444' },
  no_prior:   { label: '❓ No Prior Baseline', short: 'NO PRIOR',  color: 'text-amber-300',   bg: 'bg-amber-950/30',   border: 'border-amber-800/40',   leftBar: '#f59e0b' },
  persistent: { label: '📍 Persistent',       short: 'PERSISTENT',color: 'text-slate-400',   bg: 'bg-slate-900/40',   border: 'border-slate-700/30',   leftBar: '#475569' },
  easing:     { label: '⬇️ Easing',           short: 'EASING',    color: 'text-blue-300',    bg: 'bg-blue-950/30',    border: 'border-blue-800/40',    leftBar: '#3b82f6' },
  demand_only:{ label: '📈 Demand Only',      short: 'DEMAND',    color: 'text-slate-500',   bg: 'bg-slate-900/20',   border: 'border-slate-800/20',   leftBar: '#334155' },
}

const CONV_COLOR: Record<string, string> = {
  high:       'text-emerald-300 bg-emerald-900/40 border-emerald-700/50',
  confirmed:  'text-blue-300   bg-blue-900/40    border-blue-700/50',
  developing: 'text-amber-300  bg-amber-900/40   border-amber-700/50',
  emerging:   'text-slate-300  bg-slate-800/40   border-slate-600/50',
  watch:      'text-slate-500  bg-slate-900/30   border-slate-700/30',
}
const LINK_COLOR: Record<string, string> = {
  corroborates: '#22c55e', amplifies: '#16a34a', constrains: '#ef4444', reduces: '#f97316',
}
const LINK_LABEL: Record<string, string> = {
  corroborates: '✅', amplifies: '🚀', constrains: '⚠️', reduces: '🔻',
}

// ─── Focus Theme Card ─────────────────────────────────────────────────────────
function FocusThemeCard({
  t, rank, isSelected, onClick,
}: { t: Record<string, unknown>; rank: number; isSelected: boolean; onClick: () => void }) {
  const fc       = (t.focus_class as FocusClass) || 'persistent'
  const fm       = FOCUS_META[fc] || FOCUS_META.persistent
  const thisStr  = Number(t.this_avg_strength  ?? t.snap_strength ?? t.strength_score ?? 0)
  const priorStr = Number(t.prior_avg_strength ?? 0)
  const peakStr  = Number(t.this_peak_strength ?? 0)
  const peakMom  = Number(t.this_peak_momentum ?? t.snap_momentum ?? 0)
  const deltaPct = t.delta_pct != null ? Number(t.delta_pct) : null
  const delta    = Number(t.strength_delta ?? 0)
  const conv     = String(t.conviction ?? 'emerging').toLowerCase()

  return (
    <button onClick={onClick}
      className={`w-full text-left rounded-xl border transition-all px-3 py-2.5 ${
        isSelected ? `${fm.bg} ${fm.border} shadow-lg` : 'bg-slate-900/50 border-slate-800 hover:border-slate-600'
      }`}
      style={{ borderLeft: `4px solid ${isSelected ? fm.leftBar : 'transparent'}` }}
    >
      <div className="flex items-start gap-2 mb-1.5">
        <span className="text-slate-600 text-[10px] w-4 flex-shrink-0 pt-0.5">{rank}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 mb-1 flex-wrap">
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ${fm.color} ${fm.bg} ${fm.border}`}>
              {fm.short}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${CONV_COLOR[conv] ?? CONV_COLOR.watch}`}>
              {conv}
            </span>
          </div>
          <p className={`text-xs font-semibold leading-snug ${isSelected ? fm.color : 'text-slate-100'}`}>
            {String(t.theme_name ?? '')}
          </p>
        </div>
      </div>

      {/* Strength score YoY — the reliable metric from snapshots */}
      <div className="grid grid-cols-3 gap-2 mt-2 text-center">
        <div className="bg-slate-800/60 rounded-lg py-1.5">
          <div className={`text-xs font-black ${thisStr > 60 ? 'text-red-300' : thisStr > 35 ? 'text-amber-300' : 'text-slate-300'}`}>
            {thisStr.toFixed(0)}
          </div>
          <div className="text-[9px] text-slate-500">this yr score</div>
        </div>
        <div className="bg-slate-800/60 rounded-lg py-1.5">
          <div className="text-xs font-black text-slate-400">{priorStr.toFixed(0)}</div>
          <div className="text-[9px] text-slate-500">prior yr</div>
        </div>
        <div className={`rounded-lg py-1.5 ${
          fc === 'new' ? 'bg-emerald-900/30' :
          fc === 'escalating' ? 'bg-red-900/30' :
          fc === 'easing' ? 'bg-blue-900/30' :
          fc === 'no_prior' ? 'bg-amber-900/20' : 'bg-slate-800/60'
        }`}>
          {deltaPct != null ? (
            <>
              <div className={`text-xs font-black ${deltaPct >= 0 ? 'text-red-400' : 'text-blue-400'}`}>
                {deltaPct > 0 ? '+' : ''}{deltaPct.toFixed(0)}%
              </div>
              <div className="text-[9px] text-slate-500">YoY Δ</div>
            </>
          ) : fc === 'new' ? (
            <>
              <div className="text-xs font-black text-emerald-400">new</div>
              <div className="text-[9px] text-slate-500">1st yr</div>
            </>
          ) : (
            <>
              <div className="text-xs font-black text-amber-400">—</div>
              <div className="text-[9px] text-slate-500">no prior</div>
            </>
          )}
        </div>
      </div>

      {/* Peak + momentum */}
      <div className="flex items-center gap-3 mt-1.5 text-[10px] text-slate-500">
        <span>Peak: <strong className="text-slate-300">{peakStr.toFixed(0)}</strong></span>
        {peakMom > 0 && (
          <span className={`font-bold ${peakMom > 60 ? 'text-red-400' : peakMom > 30 ? 'text-amber-400' : 'text-slate-500'}`}>
            ↑ {peakMom.toFixed(0)} mom
          </span>
        )}
        {fc === 'escalating' && delta > 0 && (
          <span className="text-red-400 font-bold">+{delta.toFixed(0)} pts</span>
        )}
      </div>
    </button>
  )
}

// ─── Main Component ───────────────────────────────────────────────────────────
export default function YearIntelligenceTab({ country: _globalCountry }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>('IN')
  const [selectedYear, setSelectedYear] = useState('2021')
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [selectedId,   setSelectedId]   = useState<number | null>(null)
  const [selectedTheme, setSelectedTheme] = useState<Record<string, unknown> | null>(null)
  const [detailTab, setDetailTab]       = useState<'constraints' | 'beneficiaries' | 'snapshots' | 'sources'>('constraints')
  const [showPersistent, setShowPersistent] = useState(false)
  const [zoneFilter, setZoneFilter]         = useState<FocusClass | 'all'>('all')
  const [stocksResult, setStocksResult]     = useState<{ themes: Record<string, unknown>[]; stocks: Record<string, unknown>[] } | null>(null)
  const [stocksLoading, setStocksLoading]   = useState(false)
  const [stocksCtx, setStocksCtx]           = useState<string>('')

  const queryClient = useQueryClient()
  const country   = localCountry
  const as_of     = resolveAsOf(selectedYear)
  const from_date = YEAR_FROM[selectedYear]
  const isYearMode = selectedYear !== 'live'
  const yearNum    = isYearMode ? parseInt(selectedYear) : null

  // ── Data ─────────────────────────────────────────────────────────────────
  // Primary: YoY focus analysis (requires prior-year signal data)
  const { data: focusThemes = [], isLoading: focusLoading } = useQuery({
    queryKey: ['year-focus', country, selectedYear],
    queryFn: () => yearNum ? fetchYearFocus(country, yearNum) : Promise.resolve([]),
    enabled: isYearMode,
  })

  // Fallback: all themes for the year — used when YoY analysis has no data
  // (e.g. first pipeline run for this year, no prior-year baseline yet)
  const { data: fallbackThemes = [], isLoading: fallbackLoading } = useQuery({
    queryKey: ['year-themes-fallback', country, selectedYear],
    queryFn: () => fetchThemes(country, {
      ...(as_of ? { as_of, from_date } : {}),
      min_strength: 0,
    }),
    // Only fetch if focus analysis returned nothing
    enabled: !focusLoading && (focusThemes as unknown[]).length === 0,
  })

  const { data: chains = [], isLoading: chainsLoading } = useQuery({
    queryKey: ['year-chains', country, selectedYear],
    queryFn: () => fetchCausalChains(country, as_of ? { as_of, from_date } : undefined),
  })

  const { data: bens = [], isLoading: bensLoading } = useQuery({
    queryKey: ['year-bens', selectedId, as_of],
    queryFn: () => fetchBeneficiaries(selectedId!, as_of),
    enabled: selectedId !== null && detailTab === 'beneficiaries',
  })
  const { data: snapshots = [] } = useQuery({
    queryKey: ['year-snaps', selectedId, from_date, as_of],
    queryFn: () => fetchSnapshots(selectedId!, from_date, as_of),
    enabled: selectedId !== null && detailTab === 'snapshots',
  })
  const { data: sourceCompanies = [] } = useQuery({
    queryKey: ['year-sources', selectedSlug, from_date, as_of],
    queryFn: () => fetchSourceCompanies(selectedSlug!, as_of, from_date),
    enabled: selectedSlug !== null && detailTab === 'sources',
  })
  const { data: constraintComponents = [], isLoading: componentsLoading } = useQuery({
    queryKey: ['year-constraint-components', selectedId, from_date, as_of],
    queryFn: () => fetchConstraintComponents(selectedId!, from_date, as_of),
    enabled: selectedId !== null && detailTab === 'constraints',
  })

  const aiSummaryKey = ['ai-year-summary', country, selectedYear]
  const { data: aiSummary } = useQuery<{ summary_text?: string; generated_at?: string }>({
    queryKey: aiSummaryKey,
    queryFn: () => fetchAISummary(country, selectedYear, 'year_intelligence'),
  })
  const { mutate: refreshAI, isPending: aiLoading } = useMutation({
    mutationFn: () => generateYearSummary(country, selectedYear),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: aiSummaryKey }),
  })

  // ── Focus classification ──────────────────────────────────────────────────
  const hasFocusData = (focusThemes as unknown[]).length > 0
  const isLoading    = focusLoading || (!hasFocusData && fallbackLoading)

  // Assign focus_class to fallback themes when no YoY data.
  // We never call anything 'new' without a first_detected date check —
  // 'no_prior' is the honest label when prior baseline doesn't exist.
  const classifyFallback = (t: Record<string, unknown>): FocusClass => {
    const c   = Number(t.year_constraint_signals ?? 0)
    const d   = Number(t.year_demand_signals ?? 0)
    const mom = Number(t.year_peak_momentum ?? t.snap_momentum ?? 0)
    const ratio = c > 0 ? d / c : 0
    const fd  = String(t.first_detected ?? '')
    // Only call 'new' if first_detected is in the selected year
    if (fd && yearNum && fd >= `${yearNum}-01-01` && c >= 1) return 'new'
    if (c >= 2 && ratio >= 1.5 && mom > 30) return 'no_prior'  // has constraint, no prior to compare
    if (c >= 2 && (ratio >= 1.0 || mom > 20)) return 'no_prior'
    if (c >= 1) return 'no_prior'                               // has signals, no prior baseline
    return 'demand_only'
  }

  const rawFocus = hasFocusData
    ? (focusThemes as Record<string, unknown>[])
    : (fallbackThemes as Record<string, unknown>[]).map(t => ({
        ...t,
        focus_class: classifyFallback(t),
        this_constraint: Number(t.year_constraint_signals ?? 0),
        this_demand:     Number(t.year_demand_signals ?? 0),
        prior_constraint: 0,
        constraint_delta: null,
        delta_pct: null,
        dc_ratio: Number(t.year_constraint_signals ?? 0) > 0
          ? Number(t.year_demand_signals ?? 0) / Number(t.year_constraint_signals ?? 1)
          : null,
      }))

  const allFocus = rawFocus as Record<string, unknown>[]
  const allFocusFiltered = zoneFilter === 'all'
    ? allFocus
    : allFocus.filter(t => t.focus_class === zoneFilter)

  const newThemes        = allFocus.filter(t => t.focus_class === 'new')
  const escalatingThemes = allFocus.filter(t => t.focus_class === 'escalating')
  const noPriorThemes    = allFocus.filter(t => t.focus_class === 'no_prior')
  const persistentThemes = allFocus.filter(t => t.focus_class === 'persistent')
  const easingThemes     = allFocus.filter(t => t.focus_class === 'easing')
  const demandThemes     = allFocus.filter(t => t.focus_class === 'demand_only')
  const actionableThemes = [...newThemes, ...escalatingThemes]
  const actionableSlugs  = actionableThemes.map(t => String(t.theme_slug ?? ''))

  // Chains: show those with constraint evidence; fall back to all active when no signal data
  const activeChains = (chains as Record<string, unknown>[])
    .filter(ch => {
      if (!isYearMode) return true           // live: show all
      const cSigs = Number(ch.constraint_signals ?? 0)
      const dSigs = Number(ch.demand_signals ?? 0)
      return cSigs > 0 || dSigs > 0 || (chains as unknown[]).every(c => !Number((c as Record<string,unknown>).constraint_signals ?? 0))
    })
    .slice(0, 12)

  // ── Stock ranking (focused) ───────────────────────────────────────────────
  const runFocusedRanking = useCallback(async () => {
    if (!selectedYear) return
    setStocksLoading(true)
    setStocksResult(null)
    const slugs = actionableSlugs.length > 0 ? actionableSlugs : []
    setStocksCtx(`${selectedYear} · ${country} · ${slugs.length > 0 ? `${slugs.length} focused themes` : 'all themes'}`)
    try {
      const res = await runYearRankings(country, selectedYear, 20, slugs)
      setStocksResult(res)
    } catch (e) { console.error(e) }
    finally { setStocksLoading(false) }
  }, [selectedYear, country, actionableSlugs])

  const selectTheme = (t: Record<string, unknown>) => {
    setSelectedSlug(String(t.theme_slug ?? ''))
    setSelectedId(Number(t.id ?? 0) || null)
    setSelectedTheme(t)
    setDetailTab('constraints')
  }

  const resetYear = () => {
    setSelectedSlug(null); setSelectedId(null); setSelectedTheme(null)
    setStocksResult(null); setShowPersistent(false); setZoneFilter('all')
  }

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-3">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); resetYear() }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>
        <span className="text-xs text-slate-400 font-medium">Year</span>
        <div className="flex gap-1.5 flex-wrap">
          {YEARS.map(y => (
            <button key={y.value}
              onClick={() => { setSelectedYear(y.value); resetYear() }}
              className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                selectedYear === y.value
                  ? 'bg-indigo-600 border-indigo-500 text-white'
                  : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500 hover:text-indigo-300'
              }`}>{y.label}</button>
          ))}
        </div>
      </div>

      {/* ── Year Focus Overview ─────────────────────────────────────────────── */}
      {isYearMode && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            <span className="text-sm font-bold text-slate-100">
              🎯 Year Focus — {selectedYear}
              {hasFocusData ? ` vs ${parseInt(selectedYear) - 1}` : ''}
            </span>
            <span className="text-[10px] text-slate-500 italic">
              {hasFocusData ? 'What changed this year. Persistent = priced-in.' : 'Ranked by constraint intensity.'}
            </span>
            {isLoading && <Spinner />}
          </div>

          {isLoading && <div className="flex items-center gap-2 text-slate-500 text-xs py-2"><Spinner /> Loading…</div>}

          {!isLoading && allFocus.length === 0 && (
            <p className="text-xs text-slate-500 italic">No theme data for {selectedYear}. Run the pipeline for this year.</p>
          )}

          {!isLoading && !hasFocusData && allFocus.length > 0 && (
            <div className="text-[10px] text-amber-600/80 bg-amber-950/20 border border-amber-900/30 rounded-lg px-3 py-1.5 mb-2">
              ⚠️ No prior-year signal baseline for {parseInt(selectedYear) - 1} — showing constraint intensity classification instead of YoY delta.
              Run the pipeline for {parseInt(selectedYear) - 1} to get true NEW/ESCALATING comparison.
            </div>
          )}

          {/* Summary stat row — click to filter theme list */}
          {!isLoading && allFocus.length > 0 && (
            <>
              <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mb-3">
                {/* All button */}
                <button
                  onClick={() => setZoneFilter('all')}
                  className={`rounded-xl border px-3 py-2.5 text-left transition-all col-span-1 ${
                    zoneFilter === 'all' ? 'bg-slate-700 border-slate-500 ring-1 ring-slate-400' : 'bg-slate-800/60 border-slate-700 hover:border-slate-500'
                  }`}
                >
                  <div className="text-2xl font-black text-slate-200">{allFocus.length}</div>
                  <div className="text-[11px] font-bold text-slate-300">All</div>
                </button>

                {([
                  { key: 'new',        count: newThemes.length,        desc: 'first_detected this year' },
                  { key: 'escalating', count: escalatingThemes.length, desc: '≥20% stronger vs prior year' },
                  { key: 'no_prior',   count: noPriorThemes.length,    desc: 'no prior-year baseline' },
                  { key: 'persistent', count: persistentThemes.length, desc: 'stable — priced-in' },
                  { key: 'easing',     count: easingThemes.length,     desc: '≥15% weaker vs prior year' },
                ] as const).map(({ key, count, desc }) => {
                  const fm = FOCUS_META[key as FocusClass]
                  const isActive = zoneFilter === key
                  return (
                    <button
                      key={key}
                      onClick={() => setZoneFilter(isActive ? 'all' : key as FocusClass)}
                      className={`rounded-xl border px-3 py-2.5 text-left transition-all ${
                        isActive
                          ? `${fm.bg} ${fm.border} ring-1 ring-white/20`
                          : `bg-slate-900/50 border-slate-800 hover:${fm.bg} hover:${fm.border}`
                      }`}
                    >
                      <div className={`text-2xl font-black ${fm.color}`}>{count}</div>
                      <div className={`text-[11px] font-bold ${fm.color}`}>{fm.short}</div>
                      <div className="text-[9px] text-slate-600 mt-0.5 leading-tight">{desc}</div>
                    </button>
                  )
                })}
              </div>
              {zoneFilter !== 'all' && (
                <div className={`text-[11px] px-2 py-1 rounded mb-2 inline-flex items-center gap-2 ${
                  FOCUS_META[zoneFilter]?.bg} ${FOCUS_META[zoneFilter]?.border} border ${FOCUS_META[zoneFilter]?.color}`}>
                  Filtered: {FOCUS_META[zoneFilter]?.label}
                  <button onClick={() => setZoneFilter('all')} className="ml-1 opacity-60 hover:opacity-100">✕</button>
                </div>
              )}
            </>
          )}

          {/* Theme list — respects zoneFilter */}
          {allFocusFiltered.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                {zoneFilter === 'all' && actionableThemes.length > 0 && (
                  <span className="text-xs font-bold text-slate-200">
                    {actionableThemes.length} actionable (new + escalating) · {persistentThemes.length + noPriorThemes.length} structural
                  </span>
                )}
                {(zoneFilter === 'all' || zoneFilter === 'new' || zoneFilter === 'escalating') && actionableThemes.length > 0 && (
                  <button onClick={runFocusedRanking} disabled={stocksLoading}
                    className="ml-auto flex items-center gap-1.5 px-3 py-1 rounded-lg bg-emerald-800/50 border border-emerald-700/50 text-emerald-300 text-xs font-bold hover:bg-emerald-700/60 disabled:opacity-50 transition-colors">
                    {stocksLoading ? <><Spinner />Ranking…</> : `▶ Rank Stocks (${actionableThemes.length} focused themes)`}
                  </button>
                )}
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-1.5">
                {/* When filtered, show just the filtered group */}
                {zoneFilter !== 'all'
                  ? allFocusFiltered.map((t, i) => (
                      <FocusThemeCard key={String(t.theme_slug)} t={t} rank={i + 1}
                        isSelected={t.theme_slug === selectedSlug} onClick={() => selectTheme(t)} />
                    ))
                  : (
                    <>
                      {/* NEW */}
                      {newThemes.map((t, i) => (
                        <FocusThemeCard key={String(t.theme_slug)} t={t} rank={i + 1}
                          isSelected={t.theme_slug === selectedSlug} onClick={() => selectTheme(t)} />
                      ))}
                      {/* ESCALATING */}
                      {escalatingThemes.map((t, i) => (
                        <FocusThemeCard key={String(t.theme_slug)} t={t} rank={newThemes.length + i + 1}
                          isSelected={t.theme_slug === selectedSlug} onClick={() => selectTheme(t)} />
                      ))}
                      {/* NO_PRIOR separator */}
                      {noPriorThemes.length > 0 && (
                        <div className="col-span-full">
                          <button onClick={() => setShowPersistent(p => !p)}
                            className="flex items-center gap-2 text-xs text-amber-600 hover:text-amber-400 transition-colors py-1 w-full">
                            <span>{showPersistent ? '▼' : '▶'}</span>
                            <span>❓ {noPriorThemes.length} no prior baseline — constraint evident but can't compare vs last year</span>
                          </button>
                        </div>
                      )}
                      {showPersistent && noPriorThemes.map((t, i) => (
                        <FocusThemeCard key={String(t.theme_slug)} t={t} rank={actionableThemes.length + i + 1}
                          isSelected={t.theme_slug === selectedSlug} onClick={() => selectTheme(t)} />
                      ))}
                      {/* PERSISTENT — always collapsed */}
                      {persistentThemes.length > 0 && (
                        <div className="col-span-full text-[10px] text-slate-600 border-t border-slate-800 pt-2 mt-1">
                          📍 {persistentThemes.length} persistent (priced-in) · {easingThemes.length} easing — click zone filter above to view
                        </div>
                      )}
                    </>
                  )
                }
              </div>
            </div>
          )}

        </div>
      )}

      {/* ── Stock ranking result ─────────────────────────────────────────────── */}
      {(stocksLoading || stocksResult) && (
        <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-sm font-bold text-slate-200">🏅 Ranked Stocks</span>
            {stocksCtx && <span className="text-xs text-slate-500">— {stocksCtx}</span>}
          </div>
          {stocksLoading && <div className="flex items-center gap-2 text-slate-400 text-sm"><Spinner /> Computing focused rankings…</div>}
          {!stocksLoading && stocksResult && (
            stocksResult.stocks.length === 0
              ? <EmptyState>No stocks found for the focused themes in {selectedYear}. Try including persistent themes or run the pipeline.</EmptyState>
              : (
                <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
                  {stocksResult.stocks.slice(0, 30).map((s, i) => {
                    const role   = String(s.company_role ?? '')
                    const cq     = (s.cq_breakdown as Record<string, unknown>) ?? {}
                    const rColor = role === 'supply' ? '#4ade80' : role === 'beneficiary' ? '#93c5fd' : role === 'direct' ? '#c4b5fd' : '#94a3b8'
                    return (
                      <div key={i} className="bg-slate-800/60 border border-slate-700/60 rounded-lg px-3 py-2.5"
                        style={{ borderLeft: `3px solid ${rColor}` }}>
                        <div className="flex items-start justify-between gap-2 mb-1">
                          <div>
                            <div className="flex items-center gap-1.5">
                              <span className="text-slate-500 text-xs">#{String(s.rank ?? i + 1)}</span>
                              <span className="text-amber-400 font-black text-sm">{String(s.ticker ?? '')}</span>
                            </div>
                            <p className="text-xs text-slate-300 truncate">{String(s.company_name ?? '').slice(0, 32)}</p>
                          </div>
                          <div className="text-right">
                            <div className="text-sm font-black text-amber-400">{Number(s.final_score ?? 0).toFixed(3)}</div>
                            <div className="text-[10px]" style={{ color: rColor }}>{role}</div>
                          </div>
                        </div>
                        <div className="h-1 bg-slate-700 rounded-full mb-1 overflow-hidden">
                          <div className="h-full bg-amber-500 rounded-full" style={{ width: `${Math.min(100, Number(s.final_score ?? 0) * 35)}%` }} />
                        </div>
                        <div className="text-[10px] text-violet-300 truncate">
                          ⚡ {String(cq['Best Theme'] ?? '—').slice(0, 38)}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )
          )}
        </div>
      )}

      {/* ── Detail + Chains grid ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-12 gap-4">

        {/* Chains */}
        <div className="col-span-12 lg:col-span-5">
          <div className="bg-slate-900/50 border border-slate-800 rounded-xl p-4">
            <div className="flex items-center justify-between mb-3">
              <SectionHeader>🔗 Active Constraint Chains — {selectedYear}</SectionHeader>
              {chainsLoading && <Spinner />}
              <span className="text-xs text-slate-500">{activeChains.length} with signal evidence</span>
            </div>
            {activeChains.length === 0 && !chainsLoading && (
              <p className="text-xs text-slate-600 italic">
                {isYearMode ? 'No chains with constraint evidence in this year.' : 'No active chains.'}
              </p>
            )}
            <div className="space-y-1.5 max-h-80 overflow-y-auto pr-1">
              {activeChains.map((chain, ci) => {
                const links  = (chain.links as Record<string, unknown>[] | undefined) ?? []
                const score  = Number(chain.activation_score ?? 0)
                const cSigs  = Number(chain.constraint_signals ?? 0)
                const dSigs  = Number(chain.demand_signals ?? 0)
                const isCrit = cSigs >= 5
                return (
                  <div key={ci} className={`rounded-lg px-3 py-2 ${isCrit ? 'bg-red-950/40 border border-red-800/50' : 'bg-slate-800/60'}`}>
                    <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                      <p className="text-xs font-medium text-slate-200 flex-1 truncate">{String(chain.chain_name ?? '')}</p>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        {cSigs > 0 && <span className={`text-[10px] px-1.5 rounded font-bold ${isCrit ? 'bg-red-800/60 text-red-200' : 'bg-red-900/40 text-red-300'}`}>⚠️ {cSigs}</span>}
                        {dSigs > 0 && <span className="text-[10px] px-1.5 rounded bg-blue-900/40 text-blue-300">📈 {dSigs}</span>}
                        <div className="flex items-center gap-1">
                          <div className="h-1.5 w-10 bg-slate-700 rounded-full overflow-hidden">
                            <div className={`h-full rounded-full ${isCrit ? 'bg-red-500' : 'bg-indigo-500'}`}
                              style={{ width: `${Math.min(100, score)}%` }} />
                          </div>
                        </div>
                      </div>
                    </div>
                    {links.length > 0 && (
                      <div className="flex items-center gap-1 flex-wrap">
                        {links.map((lk, li) => {
                          const rel = String(lk.relation ?? 'corroborates')
                          const col = LINK_COLOR[rel] ?? '#64748b'
                          const ico = LINK_LABEL[rel] ?? '→'
                          return (
                            <span key={li} className="flex items-center gap-1">
                              {li > 0 && <span style={{ color: col }} className="text-[10px] font-bold mx-0.5">{ico}</span>}
                              <span className="text-[10px] bg-slate-700/60 rounded px-1.5 py-0.5 text-slate-300">
                                {String((lk as Record<string,unknown>).cause ?? '')}
                              </span>
                            </span>
                          )
                        })}
                        {String((links[links.length - 1] as Record<string,unknown>)?.effect ?? '') && (
                          <span className="flex items-center gap-1">
                            <span className="text-[10px] text-indigo-400 font-bold mx-0.5">→</span>
                            <span className="text-[10px] bg-indigo-900/40 border border-indigo-700/40 rounded px-1.5 py-0.5 text-indigo-300">
                              {String((links[links.length - 1] as Record<string,unknown>)?.effect ?? '')}
                            </span>
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* Theme detail */}
        <div className="col-span-12 lg:col-span-7">
          {selectedTheme ? (
            <div className="bg-slate-900/50 border border-slate-800 rounded-xl p-4 flex flex-col gap-3">
              <div>
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  {!!selectedTheme.focus_class && (
                    <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ${
                      FOCUS_META[selectedTheme.focus_class as FocusClass]?.color} ${
                      FOCUS_META[selectedTheme.focus_class as FocusClass]?.bg} ${
                      FOCUS_META[selectedTheme.focus_class as FocusClass]?.border}`}>
                      {String(FOCUS_META[selectedTheme.focus_class as FocusClass]?.label ?? '')}
                    </span>
                  )}
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${
                    CONV_COLOR[String(selectedTheme.conviction ?? 'watch').toLowerCase()] ?? CONV_COLOR.watch}`}>
                    {String(selectedTheme.conviction ?? 'watch')}
                  </span>
                </div>
                <h2 className="text-sm font-bold text-slate-100">{String(selectedTheme.theme_name ?? '')}</h2>
                <div className="flex gap-3 mt-1.5 text-[10px] text-slate-400 flex-wrap">
                  <span>Score this yr: <strong className="text-slate-200">{Number(selectedTheme.this_avg_strength ?? selectedTheme.snap_strength ?? 0).toFixed(1)}</strong></span>
                  <span>Prior yr: <strong className="text-slate-400">{Number(selectedTheme.prior_avg_strength ?? 0).toFixed(1)}</strong></span>
                  {selectedTheme.delta_pct != null && (
                    <span className={`font-bold ${Number(selectedTheme.delta_pct) > 0 ? 'text-red-400' : 'text-blue-400'}`}>
                      {Number(selectedTheme.delta_pct) > 0 ? '+' : ''}{Number(selectedTheme.delta_pct).toFixed(0)}% YoY
                    </span>
                  )}
                  <span>Peak: <strong className="text-slate-300">{Number(selectedTheme.this_peak_strength ?? 0).toFixed(1)}</strong></span>
                </div>
              </div>

              {/* Sub-tabs */}
              <div className="flex gap-1 border-b border-slate-800 flex-wrap">
                {(['constraints', 'beneficiaries', 'snapshots', 'sources'] as const).map(dt => (
                  <button key={dt} onClick={() => setDetailTab(dt)}
                    className={`px-3 py-1.5 text-xs font-medium rounded-t transition-colors ${
                      detailTab === dt ? 'bg-slate-800 text-indigo-300 border-b-2 border-indigo-500' : 'text-slate-500 hover:text-slate-300'
                    }`}>
                    {dt === 'constraints' ? "⚠️ What's Constrained" : dt === 'beneficiaries' ? '🏭 Companies' : dt === 'snapshots' ? '📈 Trend' : '📄 Sources'}
                  </button>
                ))}
              </div>

              {/* Constrained components */}
              {detailTab === 'constraints' && (
                <div className="overflow-y-auto space-y-2 max-h-72">
                  {componentsLoading && <div className="flex justify-center py-4"><Spinner /></div>}
                  {!componentsLoading && (constraintComponents as unknown[]).length === 0 && (
                    <EmptyState>No constraint signal text for this theme in {selectedYear}.</EmptyState>
                  )}
                  {!componentsLoading && (constraintComponents as Record<string, unknown>[]).map((c, ci) => {
                    const sig = String(c.signal_type ?? '')
                    const sigColor = sig === 'supply_bottleneck' ? 'bg-red-900/50 text-red-300 border-red-700/50'
                      : sig === 'inventory_drawdown' ? 'bg-orange-900/40 text-orange-300 border-orange-700/40'
                      : 'bg-amber-900/40 text-amber-300 border-amber-700/40'
                    return (
                      <div key={ci} className="bg-slate-800/60 rounded-lg px-3 py-2">
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ${sigColor}`}>{sig.replace(/_/g,' ')}</span>
                          <span className="text-xs font-semibold text-slate-100">{String(c.component ?? 'unspecified')}</span>
                          <div className="ml-auto text-[10px] text-slate-500 flex gap-2">
                            <span className="font-bold text-red-400">{Number(c.frequency ?? 0)}×</span>
                            <span>{Number(c.companies_mentioning ?? 0)} co</span>
                          </div>
                        </div>
                        {!!c.best_quote && (
                          <p className="text-[10px] text-slate-400 italic line-clamp-2 leading-relaxed border-l-2 border-slate-700 pl-2">
                            "{String(c.best_quote).trim()}"
                          </p>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {detailTab === 'beneficiaries' && (
                <div className="overflow-y-auto space-y-1.5 max-h-72">
                  {bensLoading && <Spinner />}
                  {!bensLoading && (bens as unknown[]).length === 0 && <EmptyState>No companies mapped.</EmptyState>}
                  {(bens as Record<string, unknown>[]).map((b, bi) => (
                    <div key={bi} className="flex items-center gap-3 bg-slate-800/50 rounded-lg px-3 py-2">
                      <span className="text-xs text-slate-500 w-5">{bi + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-medium text-slate-200 truncate">{String(b.company_name ?? b.entity_name ?? '')}</span>
                          {!!b.ticker && <span className="text-[10px] bg-slate-700 rounded px-1 text-slate-400">{String(b.ticker)}</span>}
                        </div>
                        {String(b.company_role ?? '') && <p className="text-[10px] text-slate-500">{String(b.company_role ?? '').replace(/_/g,' ')}</p>}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {detailTab === 'snapshots' && (
                <div className="max-h-64">
                  {(snapshots as unknown[]).length === 0 ? <EmptyState>No snapshot history.</EmptyState> : (
                    <ResponsiveContainer width="100%" height={200}>
                      <LineChart data={snapshots as Record<string, unknown>[]}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                        <XAxis dataKey="snapshot_date" tick={{ fontSize: 10, fill: '#64748b' }} tickFormatter={v => String(v).slice(0, 7)} />
                        <YAxis tick={{ fontSize: 10, fill: '#64748b' }} width={35} />
                        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 11 }} />
                        <Line type="monotone" dataKey="strength_score" stroke="#6366f1" strokeWidth={2} dot={{ r: 3 }} name="Strength" />
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              )}

              {detailTab === 'sources' && (
                <div className="overflow-y-auto space-y-1.5 max-h-72">
                  {(sourceCompanies as Record<string, unknown>[]).length === 0
                    ? <EmptyState>No source companies.</EmptyState>
                    : (sourceCompanies as Record<string, unknown>[]).slice(0, 20).map((s, si) => (
                      <div key={si} className="flex items-center gap-3 bg-slate-800/40 rounded-lg px-3 py-1.5">
                        <span className="text-xs text-slate-500 w-5">{si + 1}</span>
                        <span className="text-xs text-slate-200 flex-1 truncate">{String(s.company ?? s.entity ?? '')}</span>
                        {!!s.ticker && <span className="text-[10px] bg-slate-700 rounded px-1 text-slate-400">{String(s.ticker)}</span>}
                        <span className="text-[10px] text-slate-500">{Number(s.signal_count ?? 0)} sig</span>
                      </div>
                    ))
                  }
                </div>
              )}

              {/* AI Summary inline */}
              <div className="border-t border-slate-800 pt-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-semibold text-indigo-300">🤖 AI Year Intelligence — {selectedYear}</span>
                  <button onClick={() => refreshAI()} disabled={aiLoading}
                    className="text-[11px] px-2 py-0.5 rounded bg-indigo-800/40 border border-indigo-700/40 text-indigo-300 hover:bg-indigo-700/50 disabled:opacity-50 transition-colors">
                    {aiLoading ? <Spinner /> : '🔄'} Refresh
                  </button>
                </div>
                {aiSummary?.summary_text
                  ? <p className="text-xs text-slate-300 leading-relaxed">{aiSummary.summary_text}</p>
                  : <p className="text-[11px] text-slate-600 italic">No analysis yet.</p>}
              </div>
            </div>
          ) : (
            <div className="bg-slate-900/20 border border-slate-800/40 rounded-xl flex items-center justify-center min-h-48">
              <div className="text-center p-6">
                <div className="text-3xl mb-2">🎯</div>
                <p className="text-sm text-slate-400 font-medium">Select a theme to inspect</p>
                <p className="text-xs text-slate-600 mt-1">What's constrained · Companies · Trend · Source filings</p>
                {isYearMode && actionableThemes.length > 0 && (
                  <p className="text-xs text-emerald-500 mt-2 font-medium">
                    {actionableThemes.length} actionable theme{actionableThemes.length !== 1 ? 's' : ''} detected for {selectedYear}
                  </p>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
