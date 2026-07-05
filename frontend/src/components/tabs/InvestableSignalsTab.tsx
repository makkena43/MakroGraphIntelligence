/**
 * Investable Signals — 5-Stage Deterministic Investment Funnel
 * No Claude. Pure constraint signal arithmetic.
 *
 * Stage 1: NEW/ESCALATING constraint themes with named components
 * Stage 2: Supply-side companies in those themes (role=supply)
 * Stage 3: Companies with direct constraint evidence in own filings
 * Stage 4: Evidence quality rank (confidence x count x capex)
 * Stage 5: Final ranked shortlist with all evidence attached
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchInvestmentFinalShortlist } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

const FOCUS_STYLE: Record<string, string> = {
  new:        'text-emerald-300 bg-emerald-950/50 border-emerald-700/50',
  escalating: 'text-red-300    bg-red-950/50     border-red-700/50',
  no_prior:   'text-amber-300  bg-amber-950/30   border-amber-800/40',
}
const CONV_COLOR: Record<string, string> = {
  high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-slate-500',
}
const ROLE_CHIP: Record<string, string> = {
  supply:                  'text-emerald-300 bg-emerald-950/40',
  supplier:                'text-emerald-300 bg-emerald-950/40',
  bottleneck_player:       'text-red-300    bg-red-950/40',
  infrastructure_provider: 'text-blue-300   bg-blue-950/40',
  direct:                  'text-violet-300 bg-violet-950/40',
}
const TIME_COLOR: Record<string, string> = {
  '0-6m': 'text-red-400', '6-12m': 'text-amber-400', '12-18m': 'text-slate-400',
}

type FinalResult = {
  year: number; country: string; period: string
  stats: Record<string, number>
  constraint_regimes: Record<string, unknown>[]
  final_shortlist: Record<string, unknown>[]
}

function FunnelStep({ n, label, count, active }: { n: number; label: string; count: number; active: boolean }) {
  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border transition-all ${
      active ? 'bg-indigo-950/50 border-indigo-700/50' : 'bg-slate-900/30 border-slate-800'
    }`}>
      <div className={`w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-black flex-shrink-0 ${
        active ? 'bg-indigo-600 text-white' : 'bg-slate-700 text-slate-400'
      }`}>{n}</div>
      <div className="min-w-0">
        <div className={`text-[11px] font-medium ${active ? 'text-slate-100' : 'text-slate-400'}`}>{label}</div>
        <div className={`text-lg font-black leading-none ${active ? 'text-indigo-300' : 'text-slate-600'}`}>{count}</div>
      </div>
    </div>
  )
}

function CompanyCard({ co, idx }: { co: Record<string, unknown>; idx: number }) {
  const [open, setOpen] = useState(idx <= 5)
  const conv    = String(co.conviction ?? 'low')
  const role    = String(co.company_role ?? '')
  const focus   = String(co.theme_focus ?? '')
  const th      = String(co.time_horizon ?? '')
  const score   = Number(co.rank_score ?? 0)
  const cSigs   = Number(co.constraint_signals ?? 0)
  const kSigs   = Number(co.capex_signals ?? 0)
  const dSigs   = Number(co.demand_signals ?? 0)
  const conf    = Number(co.avg_confidence ?? 0)
  const nThemes = Number(co.theme_count ?? 0)
  const comp    = String(co.constrained_component ?? '')
  const cQuote  = String(co.best_constraint_quote ?? '')
  const kQuote  = String(co.capex_quote ?? '')
  const cDate   = String(co.best_constraint_date ?? '').slice(0, 10)
  const leftColor = conv === 'high' ? '#22c55e' : conv === 'medium' ? '#f59e0b' : '#475569'

  return (
    <div className="bg-slate-900/70 border border-slate-700 rounded-xl overflow-hidden"
         style={{ borderLeft: `4px solid ${leftColor}` }}>
      <button onClick={() => setOpen(o => !o)}
        className="w-full text-left px-4 py-3 hover:bg-slate-800/40 transition-colors">
        <div className="flex items-start gap-3">
          <div className="flex-shrink-0 text-center w-8">
            <div className="text-slate-600 text-[10px]">#{idx}</div>
            <div className="text-lg font-black text-amber-400 leading-none">{score.toFixed(0)}</div>
            <div className="text-[9px] text-slate-600">score</div>
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="text-sm font-black text-slate-100">{String(co.ticker ?? '')}</span>
              <span className="text-xs text-slate-300">{String(co.company ?? '').slice(0, 35)}</span>
              {role && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${ROLE_CHIP[role] ?? 'text-slate-400 bg-slate-800'}`}>
                  {role.replace(/_/g, ' ')}
                </span>
              )}
              <span className={`text-[10px] font-bold ${CONV_COLOR[conv]}`}>{conv}</span>
              {th && <span className={`text-[10px] font-bold ml-auto ${TIME_COLOR[th] ?? 'text-slate-500'}`}>⏱ {th}</span>}
            </div>
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              {focus && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ${FOCUS_STYLE[focus] ?? 'text-slate-400'}`}>
                  {focus.toUpperCase()}
                </span>
              )}
              <span className="text-[11px] text-slate-400 truncate">{String(co.theme ?? '').slice(0, 45)}</span>
            </div>
            <div className="flex items-center gap-3 text-[11px] flex-wrap">
              <span className="text-red-400 font-bold">⚠️ {cSigs}</span>
              {kSigs > 0 && <span className="text-amber-400 font-bold">🔨 {kSigs}</span>}
              {dSigs > 0 && <span className="text-blue-400">📈 {dSigs}</span>}
              <span className="text-slate-500">{(conf * 100).toFixed(0)}% conf</span>
              {nThemes > 1 && <span className="text-indigo-400 font-bold">×{nThemes} themes</span>}
              {comp && <span className="text-amber-500 italic">🔩 {comp}</span>}
            </div>
          </div>
          <span className="text-slate-600 text-xs flex-shrink-0 pt-1">{open ? '▲' : '▼'}</span>
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 border-t border-slate-800/60 pt-3 space-y-2">
          {cQuote ? (
            <div className="bg-red-950/20 border-l-4 border-red-700/50 rounded-r-lg px-3 py-2">
              <div className="text-[10px] text-red-400 font-bold mb-1">
                ⚠️ CONSTRAINT EVIDENCE{cDate ? ` · ${cDate}` : ''} · {(conf * 100).toFixed(0)}% confidence
              </div>
              <p className="text-[11px] text-slate-300 leading-relaxed italic">"{cQuote}"</p>
            </div>
          ) : (
            <p className="text-[11px] text-slate-600 italic">Constraint signals found but no direct quote available in period.</p>
          )}
          {kQuote && (
            <div className="bg-amber-950/20 border-l-4 border-amber-700/50 rounded-r-lg px-3 py-2">
              <div className="text-[10px] text-amber-400 font-bold mb-1">🔨 CAPEX COMMITMENT</div>
              <p className="text-[11px] text-slate-300 leading-relaxed italic">"{kQuote}"</p>
            </div>
          )}
          {((co.theme_names as string[]) ?? []).length > 1 && (
            <div className="flex flex-wrap gap-1.5 mt-1">
              <span className="text-[10px] text-slate-500 self-center">In themes:</span>
              {(co.theme_names as string[]).map((tn, i) => (
                <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-950/40 border border-indigo-800/40 text-indigo-300">{tn}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function InvestableSignalsTab({ country }: Props) {
  const [localCountry, setLocalCountry]   = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear, setSelectedYear]   = useState<number>(new Date().getFullYear())
  const [minConstraint, setMinConstraint] = useState(2)
  const [requireCapex,  setRequireCapex]  = useState(false)
  const [minConf,       setMinConf]       = useState(0.70)
  const [focusFilter,   setFocusFilter]   = useState<'all'|'new'|'escalating'>('all')
  const [convFilter,    setConvFilter]    = useState<'all'|'high'|'medium'>('all')

  const qc = useQueryClient()

  const { data: raw, isLoading, isError } = useQuery({
    queryKey: ['final-shortlist', localCountry, selectedYear, minConstraint, requireCapex, minConf],
    queryFn:  () => fetchInvestmentFinalShortlist(localCountry, selectedYear, minConstraint, requireCapex, minConf),
    staleTime: 5 * 60_000,
  })

  const result    = raw as FinalResult | undefined
  const stats     = result?.stats ?? {}
  const shortlist = (result?.final_shortlist ?? []) as Record<string, unknown>[]
  const regimes   = (result?.constraint_regimes ?? []) as Record<string, unknown>[]

  const filtered = shortlist.filter(co => {
    if (focusFilter !== 'all' && co.theme_focus !== focusFilter) return false
    if (convFilter  !== 'all' && co.conviction  !== convFilter)  return false
    return true
  })

  const hiConvCount = shortlist.filter(c => c.conviction === 'high').length
  const newCount    = shortlist.filter(c => c.theme_focus === 'new').length
  const escalCount  = shortlist.filter(c => c.theme_focus === 'escalating').length
  const capexCount  = shortlist.filter(c => Number(c.capex_signals ?? 0) > 0).length

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-bold text-slate-100">🎯 Investable Signals — Final Shortlist</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          5-stage deterministic funnel · no Claude API · pure constraint signal arithmetic
        </p>
      </div>

      {/* Methodology banner */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
        <div className="text-[11px] text-slate-400 font-bold uppercase tracking-wide mb-2">How It Works</div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs text-slate-400">
          <div><span className="text-emerald-400 font-bold">Supply-side only</span> — companies that MAKE the constrained item. Pricing power belongs to the supplier, not the consumer.</div>
          <div><span className="text-red-400 font-bold">Direct evidence</span> — company mentioned the constraint in their own concall (not inferred). High confidence = explicit language.</div>
          <div><span className="text-amber-400 font-bold">Capex = timing</span> — investing to expand capacity means they capture the margin when demand continues. Best 6-12m setup.</div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => setLocalCountry(c)}
              className={`px-3 py-1 text-xs font-bold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>
        <div className="flex gap-1.5 flex-wrap">
          {YEARS.map(y => (
            <button key={y} onClick={() => setSelectedYear(y)}
              className={`px-3 py-1 rounded text-xs font-bold border transition-colors ${
                selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>{y}</button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <span>Min ⚠️:</span>
          <select value={minConstraint} onChange={e => setMinConstraint(+e.target.value)} className="select">
            {[1, 2, 3, 5].map(n => <option key={n} value={n}>≥{n}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <span>Min conf:</span>
          <select value={minConf} onChange={e => setMinConf(+e.target.value)} className="select">
            {[0.60, 0.70, 0.80, 0.85].map(v => <option key={v} value={v}>{(v * 100).toFixed(0)}%</option>)}
          </select>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer">
          <input type="checkbox" checked={requireCapex} onChange={e => setRequireCapex(e.target.checked)} className="accent-amber-500" />
          Require 🔨 capex
        </label>
        <button onClick={() => qc.invalidateQueries({ queryKey: ['final-shortlist'] })}
          className="text-xs text-slate-500 hover:text-slate-300 transition-colors">🔄</button>
      </div>

      {isLoading && <div className="flex justify-center py-12"><Spinner /></div>}
      {isError   && <div className="text-red-400 text-xs bg-red-950/30 border border-red-800/40 rounded-xl px-4 py-3">Failed to load — check if pipeline has run for {selectedYear}</div>}

      {result && !isLoading && (
        <>
          {/* Funnel */}
          <div className="grid grid-cols-5 gap-2">
            <FunnelStep n={1} label="Constraint themes"  count={Number(stats.stage1_themes ?? 0)}       active={Number(stats.stage1_themes ?? 0) > 0} />
            <FunnelStep n={2} label="Signal companies"   count={Number(stats.stage2_signal_companies ?? stats.stage2_supply_cos ?? 0)} active={Number(stats.stage2_signal_companies ?? stats.stage2_supply_cos ?? 0) > 0} />
            <FunnelStep n={3} label="With evidence"      count={Number(stats.stage3_with_evidence ?? 0)}active={Number(stats.stage3_with_evidence ?? 0) > 0} />
            <FunnelStep n={4} label="Pass filter"        count={Number(stats.stage4_qualify ?? 0)}      active={Number(stats.stage4_qualify ?? 0) > 0} />
            <FunnelStep n={5} label="Final shortlist"    count={Number(stats.final_count ?? 0)}         active={Number(stats.final_count ?? 0) > 0} />
          </div>
          <div className="text-[10px] text-slate-600 text-center">{result.period} · {result.country}</div>

          {/* Constraint regimes */}
          {regimes.length > 0 && (
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
              <div className="text-xs font-bold text-slate-300 mb-3">⚡ Active Constraint Regimes ({regimes.length})</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
                {regimes.map((r, i) => {
                  const comps = (r.constrained_components as Record<string, unknown>[]) ?? []
                  return (
                    <div key={i} className="bg-slate-800/50 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ${FOCUS_STYLE[String(r.focus ?? '')] ?? 'text-slate-400'}`}>
                          {String(r.focus ?? '').toUpperCase()}
                        </span>
                        {r.delta_pct != null && (
                          <span className={`text-[10px] font-bold ${Number(r.delta_pct) > 0 ? 'text-red-400' : 'text-blue-400'}`}>
                            {Number(r.delta_pct) > 0 ? '+' : ''}{Number(r.delta_pct).toFixed(0)}% YoY
                          </span>
                        )}
                      </div>
                      <p className="text-[11px] text-slate-200 font-medium leading-snug">{String(r.theme_name ?? '')}</p>
                      {comps.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {comps.map((c, ci) => (
                            <span key={ci} className="text-[10px] px-1 py-0.5 rounded bg-amber-950/30 text-amber-400 font-bold">
                              🔩 {String(c.component ?? '')}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Stats + filters */}
          {shortlist.length > 0 && (
            <div className="flex gap-3 flex-wrap items-center">
              {[
                { l: 'Total', v: shortlist.length, c: 'text-slate-200' },
                { l: '🟢 High conv', v: hiConvCount, c: 'text-emerald-400' },
                { l: '🆕 New', v: newCount, c: 'text-emerald-400' },
                { l: '⬆️ Escalating', v: escalCount, c: 'text-red-400' },
                { l: '🔨 Capex', v: capexCount, c: 'text-amber-400' },
              ].map(({ l, v, c }) => (
                <div key={l} className="bg-slate-900/50 border border-slate-800 rounded-lg px-3 py-1.5 text-xs">
                  <span className="text-slate-500">{l}: </span>
                  <span className={`font-bold ${c}`}>{v}</span>
                </div>
              ))}
              <div className="flex gap-1.5 ml-auto flex-wrap items-center text-[11px]">
                {(['all', 'new', 'escalating'] as const).map(f => (
                  <button key={f} onClick={() => setFocusFilter(f)}
                    className={`px-2 py-0.5 rounded border transition-colors ${focusFilter === f ? 'bg-slate-700 border-slate-500 text-white' : 'border-slate-700 text-slate-500 hover:text-slate-300'}`}>
                    {f}
                  </button>
                ))}
                <span className="text-slate-700">|</span>
                {(['all', 'high', 'medium'] as const).map(f => (
                  <button key={f} onClick={() => setConvFilter(f)}
                    className={`px-2 py-0.5 rounded border transition-colors ${convFilter === f ? 'bg-slate-700 border-slate-500 text-white' : 'border-slate-700 text-slate-500 hover:text-slate-300'}`}>
                    {f}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Company cards */}
          {filtered.length === 0 ? (
            <EmptyState>
              No companies meet the criteria for {selectedYear} · {localCountry}.<br />
              Try lowering min constraint signals or confidence.
            </EmptyState>
          ) : (
            <div className="space-y-2">
              {filtered.map((co, i) => (
                <CompanyCard key={i} co={co} idx={Number(co.rank ?? i + 1)} />
              ))}
            </div>
          )}
        </>
      )}

      {!result && !isLoading && !isError && (
        <div className="border border-dashed border-slate-700 rounded-xl p-8 text-center text-slate-500 text-sm">
          Select year + country to run the investment funnel
        </div>
      )}
    </div>
  )
}
