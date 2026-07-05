/**
 * Company Sentiment Board
 *
 * All companies ranked by concall sentiment (1-10) computed from NLP/spaCy signals.
 * No Claude API call. Filterable by country, year, month/date range.
 * Sortable by any column.
 */

import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchSentimentBoard } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS  = [2026, 2025, 2024, 2023, 2022, 2021, 2020]
const MONTHS = [
  { v: '01', l: 'Jan' }, { v: '02', l: 'Feb' }, { v: '03', l: 'Mar' },
  { v: '04', l: 'Apr' }, { v: '05', l: 'May' }, { v: '06', l: 'Jun' },
  { v: '07', l: 'Jul' }, { v: '08', l: 'Aug' }, { v: '09', l: 'Sep' },
  { v: '10', l: 'Oct' }, { v: '11', l: 'Nov' }, { v: '12', l: 'Dec' },
]

type SortKey = 'sentiment_score' | 'invest_score' | 'constraint_sigs' | 'capex_sigs' | 'positive_sigs' | 'negative_sigs' | 'total_sigs' | 'filing_count' | 'company' | 'last_filing'

const SIG_LABEL: Record<string, string> = {
  supply_bottleneck:     '⚠️ Supply Bottleneck',
  inventory_drawdown:    '⚠️ Inventory Drawdown',
  capacity_shortage:     '⚠️ Capacity Shortage',
  demand_exceeds_supply: '🔴 Demand > Supply',
  demand_surge:          '📈 Demand Surge',
  capex_increase:        '🔨 Capex Increase',
  technology_adoption:   '💡 Tech Adoption',
  demand_slowdown:       '📉 Demand Slowing',
  hiring_surge:          '👥 Hiring Surge',
  regulatory_tailwind:   '🏛️ Policy Tailwind',
}

function SentimentBar({ score }: { score: number }) {
  const color = score >= 7 ? '#22c55e' : score >= 5 ? '#f59e0b' : '#ef4444'
  const label = score >= 7 ? 'Bullish' : score >= 5 ? 'Neutral' : 'Bearish'
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-2 bg-slate-700 rounded-full overflow-hidden flex-shrink-0">
        <div className="h-full rounded-full" style={{ width: `${Math.round(score * 10)}%`, background: color }} />
      </div>
      <span className="text-sm font-black w-7" style={{ color }}>{score.toFixed(1)}</span>
      <span className="text-[10px]" style={{ color }}>{label}</span>
    </div>
  )
}

function SortTh({ label, col, sortKey, sortDir, onSort }: {
  label: string; col: SortKey; sortKey: SortKey; sortDir: 'asc' | 'desc'; onSort: (k: SortKey) => void
}) {
  const active = sortKey === col
  return (
    <th className="px-3 py-2 text-left cursor-pointer hover:text-slate-200 transition-colors whitespace-nowrap select-none"
        onClick={() => onSort(col)}>
      <span className={`text-xs font-semibold uppercase tracking-wide ${active ? 'text-indigo-400' : 'text-slate-400'}`}>
        {label}{active ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
      </span>
    </th>
  )
}

export default function SentimentBoardTab({ country }: Props) {
  const today    = new Date().toISOString().slice(0, 10)
  const thisYear = new Date().getFullYear()

  // Date controls
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear,  setSelectedYear]  = useState<number | undefined>(undefined)
  const [selectedMonth, setSelectedMonth] = useState<string>('')           // '' = whole year
  const [fromDate,      setFromDate]      = useState('')
  const [toDate,        setToDate]        = useState(today)
  const [useDateRange,  setUseDateRange]  = useState(false)  // toggle: year/month vs exact dates

  // Filter controls
  const [search,      setSearch]      = useState('')
  const [minSignals,  setMinSignals]  = useState(3)
  const [minDirectional, setMinDirectional] = useState(2)
  const [sentFilter,  setSentFilter]  = useState<'all' | 'bullish' | 'neutral' | 'bearish'>('all')
  const [smartFilter, setSmartFilter] = useState(false)  // top picks only
  const [sortKey,     setSortKey]     = useState<SortKey>('invest_score')
  const [sortDir,     setSortDir]     = useState<'asc' | 'desc'>('desc')

  // Compute effective date range
  // "As of" date logic — never shows future data
  // Year only → backend handles "as of year-end" window (last 3 yrs up to Dec 31)
  // Month selected → to_d = last day of month, from_d = 1 year prior
  // Date range → exact window
  // Nothing → backend uses last 6 years
  const effectiveYear = useMemo(() => {
    if (useDateRange) return undefined
    if (selectedYear && !selectedMonth) return selectedYear  // pass year, let backend do "as of"
    return undefined
  }, [useDateRange, selectedYear, selectedMonth])

  const effectiveFrom = useMemo(() => {
    if (useDateRange && fromDate) return fromDate
    if (!selectedYear) return undefined
    if (selectedMonth) {
      // "As of month-end" — look back 1 year for context
      const yr = selectedYear - 1
      return `${yr}-${selectedMonth}-01`
    }
    return undefined  // year only → handled by backend via effectiveYear
  }, [useDateRange, fromDate, selectedYear, selectedMonth])

  const effectiveTo = useMemo(() => {
    if (useDateRange && toDate) return toDate
    if (!selectedYear) return undefined
    if (selectedMonth) {
      // Last day of selected month — no future data
      const d = new Date(selectedYear, parseInt(selectedMonth), 0)
      return d.toISOString().slice(0, 10)
    }
    return undefined  // year only → handled by backend via effectiveYear
  }, [useDateRange, toDate, selectedYear, selectedMonth, thisYear, today])

  // Display label for current range
  const rangeLabel = useMemo(() => {
    if (useDateRange) return fromDate && toDate ? `${fromDate} → ${toDate}` : 'Pick dates'
    if (!selectedYear) return 'All time'
    if (selectedMonth) return `${MONTHS.find(m => m.v === selectedMonth)?.l} ${selectedYear}`
    return String(selectedYear)
  }, [useDateRange, fromDate, toDate, selectedYear, selectedMonth])

  const { data: rows = [], isLoading } = useQuery({
    queryKey: ['sentiment-board', localCountry, effectiveYear, effectiveFrom, effectiveTo, minSignals, minDirectional],
    queryFn: () => fetchSentimentBoard(
      localCountry,
      effectiveYear,      // when year-only, backend handles "as of year-end"
      effectiveFrom,
      effectiveTo,
      1,
      minSignals,
      minDirectional
    ),
    staleTime: 5 * 60_000,
  })

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortDir(d => d === 'desc' ? 'asc' : 'desc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const allRows = rows as Record<string, unknown>[]

  const filtered = useMemo(() => {
    let r = [...allRows]
    if (search.trim()) {
      const q = search.toLowerCase()
      r = r.filter(row => String(row.company ?? '').toLowerCase().includes(q)
                       || String(row.ticker ?? '').toLowerCase().includes(q))
    }
    // Smart filter: only top picks (confirmed bullish + constraint + theme)
    if (smartFilter) {
      r = r.filter(row =>
        Number(row.invest_score ?? 0) >= 40 &&
        String(row.persistence_status ?? '').includes('bullish')
      )
    }
    if (sentFilter === 'bullish') r = r.filter(row => 
      Number(row.sentiment_score ?? 0) >= 6.5 &&
      ['confirmed_bullish','single_bullish'].includes(String(row.persistence_status ?? ''))
    )
    if (sentFilter === 'neutral') r = r.filter(row => { const s = Number(row.sentiment_score ?? 0); return s >= 5 && s < 6.5 })
    if (sentFilter === 'bearish') r = r.filter(row => 
      Number(row.sentiment_score ?? 0) <= 4.5 &&
      ['confirmed_bearish','single_bearish'].includes(String(row.persistence_status ?? ''))
    )

    r.sort((a, b) => {
      const isStr = sortKey === 'company' || sortKey === 'last_filing'
      const va = isStr ? String(a[sortKey] ?? '') : Number(a[sortKey] ?? 0)
      const vb = isStr ? String(b[sortKey] ?? '') : Number(b[sortKey] ?? 0)
      if (typeof va === 'string') return sortDir === 'asc' ? va.localeCompare(vb as string) : (vb as string).localeCompare(va)
      return sortDir === 'asc' ? (va as number) - (vb as number) : (vb as number) - (va as number)
    })
    return r
  }, [allRows, search, sentFilter, sortKey, sortDir])

  const bullishCt = allRows.filter(r => Number(r.sentiment_score ?? 0) >= 7).length
  const neutralCt = allRows.filter(r => { const s = Number(r.sentiment_score ?? 0); return s >= 5 && s < 7 }).length
  const bearishCt = allRows.filter(r => Number(r.sentiment_score ?? 0) < 5).length

  return (
    <div className="space-y-4">

      {/* Header */}
      <div>
        <h2 className="text-base font-bold text-slate-100">📊 Company Concall Sentiment Board</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          All companies ranked by NLP/spaCy concall sentiment · no Claude API · sortable by any column
        </p>
      </div>

      {/* ── Primary filters: Country + Date ──────────────────────────────── */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 space-y-3">

        <div className="flex items-center gap-3 flex-wrap">
          {/* Country */}
          <div>
            <div className="text-[10px] text-slate-500 mb-1 uppercase tracking-wide">Country</div>
            <div className="flex rounded-lg border border-slate-700 overflow-hidden">
              {(['IN', 'US'] as const).map(c => (
                <button key={c} onClick={() => setLocalCountry(c)}
                  className={`px-4 py-1.5 text-xs font-bold transition-colors ${
                    localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
                  }`}>
                  {c === 'IN' ? '🇮🇳 India' : '🇺🇸 USA'}
                </button>
              ))}
            </div>
          </div>

          {/* Mode toggle */}
          <div>
            <div className="text-[10px] text-slate-500 mb-1 uppercase tracking-wide">Date Mode</div>
            <div className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
              <button onClick={() => setUseDateRange(false)}
                className={`px-3 py-1.5 font-semibold transition-colors ${!useDateRange ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                Year / Month
              </button>
              <button onClick={() => setUseDateRange(true)}
                className={`px-3 py-1.5 font-semibold transition-colors ${useDateRange ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                Date Range
              </button>
            </div>
          </div>

          {/* Current range label */}
          <div className="flex items-center gap-2">
            <div className="text-[10px] text-slate-500 uppercase tracking-wide">Period</div>
            <div className="px-3 py-1.5 bg-indigo-900/30 border border-indigo-800/40 rounded-lg text-xs text-indigo-300 font-bold">
              {localCountry === 'IN' ? '🇮🇳' : '🇺🇸'} {rangeLabel}
            </div>
          </div>
        </div>

        {/* Year / Month selectors */}
        {!useDateRange && (
          <div className="flex items-start gap-6 flex-wrap">
            {/* Year */}
            <div>
              <div className="text-[10px] text-slate-500 mb-1.5 uppercase tracking-wide">Year</div>
              <div className="flex gap-1.5 flex-wrap">
                <button onClick={() => { setSelectedYear(undefined); setSelectedMonth('') }}
                  className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
                    !selectedYear ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
                  }`}>All</button>
                {YEARS.map(y => (
                  <button key={y} onClick={() => { setSelectedYear(y); setSelectedMonth('') }}
                    className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
                      selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
                    }`}>{y}</button>
                ))}
              </div>
            </div>

            {/* Month (only when year selected) */}
            {selectedYear && (
              <div>
                <div className="text-[10px] text-slate-500 mb-1.5 uppercase tracking-wide">Month (optional)</div>
                <div className="flex gap-1 flex-wrap">
                  <button onClick={() => setSelectedMonth('')}
                    className={`px-2.5 py-1 rounded text-xs font-semibold border transition-colors ${
                      !selectedMonth ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'
                    }`}>All</button>
                  {MONTHS.map(m => (
                    <button key={m.v} onClick={() => setSelectedMonth(m.v)}
                      className={`px-2.5 py-1 rounded text-xs font-semibold border transition-colors ${
                        selectedMonth === m.v ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'
                      }`}>{m.l}</button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Exact date range */}
        {useDateRange && (
          <div className="flex items-end gap-3 flex-wrap">
            <div>
              <label className="text-[10px] text-slate-500 uppercase tracking-wide block mb-1">From date</label>
              <input type="date" value={fromDate} max={today}
                onChange={e => setFromDate(e.target.value)} className="input" />
            </div>
            <div>
              <label className="text-[10px] text-slate-500 uppercase tracking-wide block mb-1">To date</label>
              <input type="date" value={toDate} max={today}
                onChange={e => setToDate(e.target.value)} className="input" />
            </div>
            <button onClick={() => { setFromDate(''); setToDate(today) }}
              className="text-xs text-slate-500 hover:text-red-400 transition-colors pb-1">
              Clear
            </button>
          </div>
        )}
      </div>

      {/* ── Secondary filters ─────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 flex-wrap">
        {/* Smart Filter toggle */}
        <button onClick={() => setSmartFilter(f => !f)}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl border font-bold text-sm transition-all flex-shrink-0 ${
            smartFilter
              ? 'bg-amber-700/60 border-amber-500 text-amber-200 shadow-lg shadow-amber-900/30'
              : 'bg-slate-800 border-slate-700 text-slate-400 hover:border-amber-600 hover:text-amber-300'
          }`}>
          🎯 {smartFilter ? 'Smart Filter ON' : 'Smart Filter'}
        </button>
        {smartFilter && (
          <div className="text-[10px] text-amber-500 bg-amber-950/20 border border-amber-900/30 rounded-lg px-2 py-1 flex-shrink-0">
            Showing invest_score ≥40 + confirmed bullish only
          </div>
        )}
        <input type="text" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search company name or ticker…"
          className="input flex-1 min-w-40" />
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <span className="whitespace-nowrap">Min signals/filing:</span>
          <select value={minSignals} onChange={e => setMinSignals(Number(e.target.value))} className="select">
            {[1,3,5,10].map(n => <option key={n} value={n}>≥{n}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <span className="whitespace-nowrap">Min directional:</span>
          <select value={minDirectional} onChange={e => setMinDirectional(Number(e.target.value))} className="select">
            {[1,2,3,5,10].map(n => <option key={n} value={n}>≥{n}</option>)}
          </select>
        </div>
      </div>

      {/* Sentiment filter cards */}
      {!isLoading && allRows.length > 0 && (
        <div className="grid grid-cols-4 gap-2">
          {[
            { key: 'all',     label: 'All Companies', count: allRows.length, color: 'text-slate-200', bg: 'bg-slate-700', border: 'border-slate-500' },
            { key: 'bullish', label: '🟢 Bullish ≥7',  count: bullishCt,      color: 'text-emerald-400', bg: 'bg-emerald-950/60', border: 'border-emerald-700' },
            { key: 'neutral', label: '🟡 Neutral 5–7', count: neutralCt,      color: 'text-amber-400',   bg: 'bg-amber-950/40',   border: 'border-amber-700' },
            { key: 'bearish', label: '🔴 Bearish <5',  count: bearishCt,      color: 'text-red-400',     bg: 'bg-red-950/50',     border: 'border-red-700' },
          ].map(({ key, label, count, color, bg, border }) => (
            <button key={key}
              onClick={() => setSentFilter(sentFilter === key && key !== 'all' ? 'all' : key as typeof sentFilter)}
              className={`rounded-xl border px-3 py-2.5 text-left transition-all ${
                sentFilter === key ? `${bg} ${border}` : 'bg-slate-900/50 border-slate-800 hover:border-slate-600'
              }`}>
              <div className={`text-xl font-black ${color}`}>{count}</div>
              <div className={`text-[11px] font-semibold ${color}`}>{label}</div>
            </button>
          ))}
        </div>
      )}

      {isLoading && <div className="flex justify-center py-12"><Spinner /></div>}
      {!isLoading && filtered.length === 0 && (
        <EmptyState>No companies found for {rangeLabel} · {localCountry}. Run the pipeline to build signal data.</EmptyState>
      )}

      {/* Table */}
      {!isLoading && filtered.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-slate-700">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-slate-800 border-b border-slate-700">
                <th className="px-3 py-2 text-left text-slate-500 text-[10px] w-8">#</th>
                <SortTh label="Company"      col="company"         sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="🎯 Score"     col="invest_score"    sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="Sentiment"    col="sentiment_score" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="⚠️ Constraint" col="constraint_sigs" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="📈 Demand"    col="positive_sigs"   sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="🔨 Capex"     col="capex_sigs"      sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="Total Sigs"   col="total_sigs"      sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="Filings"      col="filing_count"    sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <SortTh label="Last Filing"  col="last_filing"     sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                <th className="px-3 py-2 text-left text-slate-400 text-xs font-semibold uppercase whitespace-nowrap">Trend</th>
                <th className="px-3 py-2 text-left text-slate-400 text-xs font-semibold uppercase">Top Signal</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row, i) => {
                const score  = Number(row.sentiment_score ?? 5)
                const cSigs  = Number(row.constraint_sigs ?? 0)
                const dSigs  = Number(row.positive_sigs ?? 0)
                const capex  = Number(row.capex_sigs ?? 0)
                const tSigs  = Number(row.total_sigs ?? 0)
                const domSig = String(row.dominant_signal ?? '')
                const rowBg  = i % 2 === 0 ? 'bg-slate-900/30' : ''
                return (
                  <tr key={i} className={`border-b border-slate-800 hover:bg-slate-800/50 transition-colors ${rowBg}`}>
                    <td className="px-3 py-2 text-slate-600 text-[10px]">{i + 1}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-mono font-bold text-indigo-300 text-[11px] w-16 flex-shrink-0 truncate">
                          {String(row.ticker ?? '—').slice(0, 12)}
                        </span>
                        <div className="min-w-0">
                          <span className="text-slate-200 truncate block max-w-40">{String(row.company ?? '')}</span>
                          {!!row.theme_names && (
                            <span className="text-[9px] text-indigo-400 truncate block max-w-40">{String(row.theme_names ?? '').slice(0,50)}</span>
                          )}
                        </div>
                        {String(row.persistence_status ?? '') === 'confirmed_bullish' && (
                          <span className="text-[9px] px-1 rounded bg-emerald-900/50 text-emerald-400 font-bold whitespace-nowrap">✓ 2Q</span>
                        )}
                        {String(row.persistence_status ?? '') === 'confirmed_bearish' && (
                          <span className="text-[9px] px-1 rounded bg-red-900/50 text-red-400 font-bold whitespace-nowrap">✓ 2Q</span>
                        )}
                        {String(row.persistence_status ?? '').startsWith('single_') && (
                          <span className="text-[9px] px-1 rounded bg-slate-700/50 text-slate-500 whitespace-nowrap">1Q</span>
                        )}
                        {String(row.persistence_status ?? '') === 'mixed' && (
                          <span className="text-[9px] px-1 rounded bg-amber-900/30 text-amber-600 whitespace-nowrap">mixed</span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <div className="w-12 h-2 bg-slate-700 rounded-full overflow-hidden">
                          <div className="h-full rounded-full" style={{
                            width: `${Math.min(100, Number(row.invest_score ?? 0))}%`,
                            background: Number(row.invest_score ?? 0) >= 60 ? '#f59e0b' : Number(row.invest_score ?? 0) >= 40 ? '#818cf8' : '#475569'
                          }} />
                        </div>
                        <span className="text-xs font-black" style={{
                          color: Number(row.invest_score ?? 0) >= 60 ? '#f59e0b' : Number(row.invest_score ?? 0) >= 40 ? '#818cf8' : '#475569'
                        }}>{Number(row.invest_score ?? 0).toFixed(0)}</span>
                      </div>
                    </td>
                    <td className="px-3 py-2"><SentimentBar score={score} /></td>
                    <td className="px-3 py-2">
                      <span className={`font-bold text-sm ${cSigs > 5 ? 'text-red-400' : cSigs > 0 ? 'text-orange-400' : 'text-slate-600'}`}>
                        {cSigs > 0 ? cSigs : '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`font-bold text-sm ${dSigs > 5 ? 'text-emerald-400' : dSigs > 0 ? 'text-blue-400' : 'text-slate-600'}`}>
                        {dSigs > 0 ? dSigs : '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`font-bold text-sm ${capex > 0 ? 'text-amber-400' : 'text-slate-600'}`}>
                        {capex > 0 ? capex : '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">{tSigs}</td>
                    <td className="px-3 py-2 text-slate-400">{Number(row.filing_count ?? 0)}</td>
                    <td className="px-3 py-2 text-slate-500 font-mono text-[11px]">
                      <div>{String(row.last_filing ?? '').slice(0, 10)}</div>
                      {!!row.last_filing_type && (
                        <div className="text-[9px] text-slate-600">{String(row.last_filing_type ?? '')}</div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {String(row.sentiment_trend ?? '') === 'improving' && (
                        <span className="text-[10px] text-emerald-400 font-bold">↑ Improving</span>
                      )}
                      {String(row.sentiment_trend ?? '') === 'declining' && (
                        <span className="text-[10px] text-red-400 font-bold">↓ Declining</span>
                      )}
                      {String(row.sentiment_trend ?? '') === 'stable' && (
                        <span className="text-[10px] text-slate-500">→ Stable</span>
                      )}
                      {!!row.period_avg_sentiment && (
                        <div className="text-[9px] text-slate-600 mt-0.5">
                          avg {Number(row.period_avg_sentiment ?? 0).toFixed(1)}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {domSig && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700/50 text-slate-400 whitespace-nowrap">
                          {SIG_LABEL[domSig] ?? domSig.replace(/_/g, ' ')}
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="px-4 py-2 text-[10px] text-slate-600 border-t border-slate-800 flex items-center gap-3">
            <span>{filtered.length} companies · {localCountry === 'IN' ? '🇮🇳 India' : '🇺🇸 USA'} · {rangeLabel}</span>
            {filtered.length < allRows.length && <span>· {allRows.length - filtered.length} hidden by filter</span>}
            <span className="ml-auto">Sorted: {sortKey.replace(/_/g, ' ')} {sortDir === 'desc' ? '↓' : '↑'}</span>
          </div>
        </div>
      )}
    </div>
  )
}
