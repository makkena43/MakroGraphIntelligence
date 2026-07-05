/**
 * Investment Shortlist Tab
 *
 * The answer to: "Which companies should I invest in right now?"
 *
 * Investability Score (0-100):
 *   35% Theme conviction  — CONFIRMED > DEVELOPING > EMERGING
 *   25% Escalation signal — NEW/ESCALATING > PERSISTENT > EASING
 *   25% Constraint proof  — supply_bottleneck mentions in their own concalls
 *   15% Theme overlap     — company in 3 themes = higher structural conviction
 *
 * Action classification:
 *   🔴 ACT NOW   — escalating/new theme + confirmed conviction + constraint evidence
 *   🟡 RESEARCH  — promising signal, needs deeper validation
 *   🔵 WATCH     — persistent theme, likely priced in
 */

import { useState, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchInvestmentShortlist } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

const ACTION_META = {
  act_now:  { label: '🔴 Act Now',    color: 'text-red-300',    bg: 'bg-red-950/60',    border: 'border-red-700/60',    bar: '#ef4444', desc: 'All 4 conditions met: NEW/ESCALATING theme + CONFIRMED conviction + ≥3 constraint signals + demand ≥ constraint' },
  research: { label: '🟡 Research',   color: 'text-amber-300',  bg: 'bg-amber-950/40',  border: 'border-amber-700/50',  bar: '#f59e0b', desc: 'Promising — theme escalating but conviction still developing, or constraint signals < 3. Validate before sizing.' },
  watch:    { label: '🔵 Watch',      color: 'text-sky-300',    bg: 'bg-sky-950/30',    border: 'border-sky-800/40',    bar: '#38bdf8', desc: 'Has constraint evidence but persistent theme — likely priced in. Wait for YoY escalation signal.' },
}

const CONV_BADGE: Record<string, string> = {
  high:       'text-emerald-300 bg-emerald-900/40 border-emerald-700/50',
  confirmed:  'text-blue-300   bg-blue-900/40    border-blue-700/50',
  developing: 'text-amber-300  bg-amber-900/40   border-amber-700/50',
  emerging:   'text-slate-300  bg-slate-800/40   border-slate-600/50',
  watch:      'text-slate-500  bg-slate-900/30   border-slate-700/30',
}

const FOCUS_CHIP: Record<string, string> = {
  new:        'text-emerald-400 bg-emerald-950/40',
  escalating: 'text-red-400    bg-red-950/40',
  no_prior:   'text-amber-400  bg-amber-950/30',
  persistent: 'text-slate-500  bg-slate-800/30',
  easing:     'text-blue-400   bg-blue-950/30',
}

const ROLE_COLOR: Record<string, string> = {
  supply: '#4ade80', beneficiary: '#93c5fd', direct: '#c4b5fd', supplier: '#4ade80',
}

function ScoreBar({ score }: { score: number }) {
  const color = score >= 70 ? '#ef4444' : score >= 50 ? '#f59e0b' : score >= 30 ? '#38bdf8' : '#475569'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${score}%`, background: color }} />
      </div>
      <span className="text-sm font-black w-8 text-right" style={{ color }}>{score}</span>
    </div>
  )
}

function CompanyRow({
  co, rank, expanded, onToggle,
}: { co: Record<string, unknown>; rank: number; expanded: boolean; onToggle: () => void }) {
  const action = String(co.action ?? 'watch') as keyof typeof ACTION_META
  const am     = ACTION_META[action] ?? ACTION_META.watch
  const inv    = Number(co.investability_score ?? 0)
  const conv   = String(co.conviction ?? 'emerging').toLowerCase()
  const role   = String(co.company_role ?? '')
  const roleC  = ROLE_COLOR[role] ?? '#94a3b8'
  const cSigs  = Number(co.constraint_signals ?? 0)
  const dSigs  = Number(co.demand_signals ?? 0)
  const themes = (co.themes as Record<string, unknown>[]) ?? []
  const quote  = String(co.best_quote ?? '')
  const lastF  = String(co.last_filing ?? '').slice(0, 10)
  const ticker = String(co.ticker ?? '')

  return (
    <div className={`rounded-xl border overflow-hidden transition-all ${am.bg} ${am.border}`}
         style={{ borderLeft: `4px solid ${am.bar}` }}>

      {/* Main row */}
      <button onClick={onToggle} className="w-full text-left px-4 py-3 hover:brightness-110 transition-all">
        <div className="flex items-start gap-3">
          {/* Rank + action */}
          <div className="flex-shrink-0 text-center w-8">
            <div className="text-slate-600 text-[10px]">#{rank}</div>
          </div>

          {/* Company identity */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="text-sm font-black text-slate-100">{String(co.company ?? '')}</span>
              {ticker && (
                <span className="text-[11px] bg-slate-700/80 rounded px-1.5 py-0.5 text-slate-300 font-mono font-bold">
                  {ticker}
                </span>
              )}
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${CONV_BADGE[conv] ?? CONV_BADGE.watch}`}>
                {conv}
              </span>
              {role && (
                <span className="text-[10px] font-semibold" style={{ color: roleC }}>
                  {role.replace(/_/g, ' ')}
                </span>
              )}
              <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${am.color} ${am.bg}`}>
                {am.label}
              </span>
            </div>

            {/* Investability score bar */}
            <ScoreBar score={inv} />

            {/* Signal summary row */}
            <div className="flex items-center gap-2 mt-1.5 flex-wrap text-[11px]">
              {cSigs > 0 && (
                <span className="text-red-400 font-bold">⚠️ {cSigs}</span>
              )}
              {/* YoY delta */}
              {Number(co.constraint_delta ?? 0) > 0 && (
                <span className="text-emerald-400 font-bold">
                  ↑+{Number(co.constraint_delta)} vs prior yr
                  {co.constraint_delta_pct != null && (
                    <span className="text-emerald-600"> (+{Number(co.constraint_delta_pct).toFixed(0)}%)</span>
                  )}
                </span>
              )}
              {Number(co.capex_signals ?? 0) > 0 && (
                <span className="text-amber-400 font-bold">🔨 {Number(co.capex_signals)} capex</span>
              )}
              {themes.length > 0 && (
                <span className="text-slate-600">· {themes.length} theme{themes.length !== 1 ? 's' : ''}</span>
              )}
              {lastF && <span className="text-slate-700 ml-auto">{lastF}</span>}
            </div>

            {/* Theme chips */}
            <div className="flex items-center gap-1.5 mt-1 flex-wrap">
              {themes.slice(0, 3).map((t, i) => {
                const fc = String(t.focus ?? t.focus_class ?? 'persistent')
                return (
                  <span key={i} className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${FOCUS_CHIP[fc] ?? FOCUS_CHIP.persistent}`}>
                    {String(t.name ?? '').slice(0, 28)}
                    {t.delta != null && (
                      <span className={`ml-1 ${Number(t.delta) > 0 ? 'text-red-400' : 'text-blue-400'}`}>
                        {Number(t.delta) > 0 ? '+' : ''}{Number(t.delta).toFixed(0)}%
                      </span>
                    )}
                  </span>
                )
              })}
              {themes.length > 3 && (
                <span className="text-[10px] text-slate-600">+{themes.length - 3}</span>
              )}
            </div>
          </div>

          {/* Score */}
          <div className="flex-shrink-0 text-right">
            <div className="text-2xl font-black" style={{ color: am.bar }}>{inv}</div>
            <div className="text-[9px] text-slate-600">inv. score</div>
            <div className="text-[10px] text-slate-500 mt-1">{expanded ? '▲' : '▼'} detail</div>
          </div>
        </div>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-4 pb-4 border-t border-slate-800/60 pt-3 space-y-3">

          {/* Why this score */}
          <div className="text-[10px] text-slate-500 italic">{am.desc}</div>

          {/* All themes */}
          {themes.length > 0 && (
            <div>
              <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wide mb-1.5">
                📌 Active constraint themes ({themes.length})
              </div>
              <div className="space-y-1">
                {themes.map((t, i) => {
                  const fc = String(t.focus ?? t.focus_class ?? 'persistent')
                  return (
                    <div key={i} className="flex items-center gap-2 text-[11px]">
                      <span className={`text-[10px] px-1.5 rounded font-bold ${FOCUS_CHIP[fc] ?? FOCUS_CHIP.persistent}`}>
                        {fc.toUpperCase()}
                      </span>
                      <span className="text-slate-300 flex-1 truncate">{String(t.name ?? '')}</span>
                      {t.delta != null && (
                        <span className={`font-bold ${Number(t.delta) >= 0 ? 'text-red-400' : 'text-blue-400'}`}>
                          {Number(t.delta) > 0 ? '+' : ''}{Number(t.delta).toFixed(0)}% YoY
                        </span>
                      )}
                      <span className="text-slate-600">{Number(t.strength ?? 0).toFixed(0)}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Signal evidence grid */}
          <div className="grid grid-cols-4 gap-2 text-center">
            <div className="bg-red-950/30 rounded-lg py-2">
              <div className="text-sm font-black text-red-300">{cSigs}</div>
              <div className="text-[9px] text-slate-600">⚠️ constraint</div>
            </div>
            <div className={`rounded-lg py-2 ${Number(co.constraint_delta ?? 0) > 0 ? 'bg-emerald-950/30' : 'bg-slate-800/30'}`}>
              <div className={`text-sm font-black ${Number(co.constraint_delta ?? 0) > 0 ? 'text-emerald-400' : 'text-slate-500'}`}>
                {Number(co.constraint_delta ?? 0) > 0 ? `+${Number(co.constraint_delta)}` : '—'}
              </div>
              <div className="text-[9px] text-slate-600">YoY delta</div>
            </div>
            <div className={`rounded-lg py-2 ${Number(co.capex_signals ?? 0) > 0 ? 'bg-amber-950/30' : 'bg-slate-800/20'}`}>
              <div className={`text-sm font-black ${Number(co.capex_signals ?? 0) > 0 ? 'text-amber-400' : 'text-slate-600'}`}>
                {Number(co.capex_signals ?? 0) > 0 ? Number(co.capex_signals) : '—'}
              </div>
              <div className="text-[9px] text-slate-600">🔨 capex sigs</div>
            </div>
            <div className="bg-blue-950/20 rounded-lg py-2">
              <div className="text-sm font-black text-blue-300">{dSigs}</div>
              <div className="text-[9px] text-slate-600">📈 demand</div>
            </div>
          </div>
          {Number(co.capex_signals ?? 0) > 0 && Number(co.constraint_signals ?? 0) > 0 && (
            <div className="text-[10px] bg-amber-950/20 border border-amber-900/30 rounded-lg px-3 py-1.5 text-amber-300">
              🔨 <strong>Capex + Constraint:</strong> Company is actively investing to solve the bottleneck — highest conviction signal.
            </div>
          )}

          {/* Best concall quote */}
          {quote && (
            <div className="bg-slate-800/50 rounded-lg px-3 py-2.5 border-l-4 border-red-700/60">
              <div className="text-[10px] text-red-400 font-bold mb-1">📋 Best constraint evidence from filings</div>
              <p className="text-xs text-slate-300 italic leading-relaxed">"…{quote.trim()}…"</p>
            </div>
          )}

          {!quote && cSigs === 0 && (
            <p className="text-[11px] text-slate-600 italic">
              No direct concall constraint evidence found in the selected period.
              This company is mapped via supply-chain structure — run pipeline for this period to build signal data.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default function InvestmentShortlistTab({ country }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear, setSelectedYear] = useState<number | undefined>(undefined)
  const [actionFilter, setActionFilter]   = useState<'all' | 'act_now' | 'research' | 'watch'>('all')
  const [minConstraint, setMinConstraint] = useState(1)
  const [capexFocus, setCapexFocus]       = useState(false)
  const [expandedIdx, setExpandedIdx]     = useState<number | null>(null)
  const [topN, setTopN]                   = useState(60)

  const queryClient = useQueryClient()

  const { data: rawList = [], isLoading, isFetching } = useQuery({
    queryKey: ['investment-shortlist', localCountry, selectedYear, topN, capexFocus],
    queryFn: () => fetchInvestmentShortlist(localCountry, selectedYear, topN, capexFocus),
    staleTime: 2 * 60_000,
  })

  const list = (rawList as Record<string, unknown>[]).filter(co => {
    if (actionFilter !== 'all' && co.action !== actionFilter) return false
    if (Number(co.constraint_signals ?? 0) < minConstraint) return false
    return true
  })

  const actNow   = (rawList as Record<string, unknown>[]).filter(r => r.action === 'act_now').length
  const research = (rawList as Record<string, unknown>[]).filter(r => r.action === 'research').length
  const watch    = (rawList as Record<string, unknown>[]).filter(r => r.action === 'watch').length

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['investment-shortlist'] })

  return (
    <div className="space-y-4">

      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-base font-bold text-slate-100">🎯 Investment Shortlist</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Constraint-driven investing — companies ranked by Investability Score across all active themes
          </p>
        </div>
        <button onClick={refresh} disabled={isFetching} className="text-xs text-slate-500 hover:text-slate-300 transition-colors disabled:opacity-50">
          {isFetching ? <Spinner /> : '🔄 Refresh'}
        </button>
      </div>

      {/* Investability Score explanation */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl px-4 py-3 text-xs text-slate-400 leading-relaxed">
        <strong className="text-slate-200">Investability Score (0–100)</strong> =
        <span className="text-red-400"> 40% constraint signals</span> (year-specific, from own filings) +
        <span className="text-amber-400"> 35% theme quality</span> (NEW/ESCALATING × CONFIRMED) +
        <span className="text-blue-400"> 15% demand pressure</span> (demand/constraint ratio) +
        <span className="text-slate-300"> 10% theme overlap</span> (multi-theme = structural conviction).
        <div className="mt-1.5 flex gap-4 flex-wrap">
          <span><span className="text-red-400">🔴 Act Now</span> = all 4: escalating + confirmed + ≥3 constraint sigs + demand ≥ constraint</span>
          <span><span className="text-amber-400">🟡 Research</span> = promising but 1-2 conditions missing</span>
          <span><span className="text-sky-400">🔵 Watch</span> = evidence but persistent theme — likely priced in</span>
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 flex-wrap">
        {/* Country */}
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); setExpandedIdx(null) }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>

        {/* Year */}
        <div className="flex gap-1.5 flex-wrap">
          <button onClick={() => setSelectedYear(undefined)}
            className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
              !selectedYear ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
            }`}>All years</button>
          {YEARS.map(y => (
            <button key={y} onClick={() => setSelectedYear(y)}
              className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
                selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>{y}</button>
          ))}
        </div>

        {/* Min constraint signals dropdown */}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min ⚠️ constraint signals</label>
          <select
            value={minConstraint}
            onChange={e => setMinConstraint(Number(e.target.value))}
            className="select"
          >
            <option value={1}>≥ 1 signal</option>
            <option value={2}>≥ 2 signals</option>
            <option value={3}>≥ 3 signals</option>
            <option value={4}>≥ 4 signals</option>
            <option value={5}>≥ 5 signals</option>
            <option value={10}>≥ 10 signals</option>
          </select>
        </div>

        {/* Capex focus toggle */}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Mode</label>
          <button
            onClick={() => setCapexFocus(f => !f)}
            className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
              capexFocus
                ? 'bg-amber-700/60 border-amber-600 text-amber-200 shadow-lg shadow-amber-900/30'
                : 'bg-slate-800 border-slate-700 text-slate-400 hover:border-amber-700 hover:text-amber-300'
            }`}
          >
            🔨 {capexFocus ? 'Capex Focus ON' : 'Capex Focus'}
          </button>
          {capexFocus && (
            <p className="text-[10px] text-amber-500 mt-1 max-w-48">
              Boosting companies with capex_increase + constraint signals — investing to solve the bottleneck
            </p>
          )}
        </div>
      </div>

      {isLoading && <div className="flex justify-center py-12"><Spinner /></div>}

      {!isLoading && (
        <>
          {/* Action filter cards */}
          <div className="grid grid-cols-4 gap-2">
            <button onClick={() => setActionFilter('all')}
              className={`rounded-xl border px-3 py-2.5 text-left transition-all ${
                actionFilter === 'all' ? 'bg-slate-700 border-slate-500' : 'bg-slate-900/50 border-slate-800 hover:border-slate-600'
              }`}>
              <div className="text-xl font-black text-slate-200">{(rawList as unknown[]).length}</div>
              <div className="text-[11px] text-slate-400">All Companies</div>
            </button>
            {([
              { key: 'act_now',  count: actNow,   meta: ACTION_META.act_now  },
              { key: 'research', count: research,  meta: ACTION_META.research },
              { key: 'watch',    count: watch,     meta: ACTION_META.watch    },
            ] as const).map(({ key, count, meta }) => (
              <button key={key} onClick={() => setActionFilter(actionFilter === key ? 'all' : key)}
                className={`rounded-xl border px-3 py-2.5 text-left transition-all ${
                  actionFilter === key ? `${meta.bg} ${meta.border}` : 'bg-slate-900/50 border-slate-800 hover:border-slate-600'
                }`}>
                <div className={`text-xl font-black ${meta.color}`}>{count}</div>
                <div className={`text-[11px] font-semibold ${meta.color}`}>{meta.label}</div>
              </button>
            ))}
          </div>

          {list.length === 0 && (
            <EmptyState>
              No companies found for these filters.<br />
              Run the pipeline to build theme and beneficiary data for this period.
            </EmptyState>
          )}

          {/* Company list */}
          <div className="space-y-2">
            {list.map((co, i) => (
              <CompanyRow
                key={i}
                co={co}
                rank={i + 1}
                expanded={expandedIdx === i}
                onToggle={() => setExpandedIdx(expandedIdx === i ? null : i)}
              />
            ))}
          </div>

          {list.length > 0 && (rawList as unknown[]).length >= topN && (
            <button onClick={() => setTopN(n => n + 30)}
              className="w-full py-2 text-xs text-slate-500 border border-dashed border-slate-700 rounded-lg hover:text-slate-300 hover:border-slate-500 transition-colors">
              Load more
            </button>
          )}
        </>
      )}
    </div>
  )
}
