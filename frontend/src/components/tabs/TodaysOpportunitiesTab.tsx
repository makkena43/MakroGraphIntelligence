/**
 * Today's Opportunities — The single tab you open every morning.
 *
 * Shows all investment opportunities grouped by constraint cycle stage:
 *   Stage 1 🔴 STRONG BUY — Early edge, before consensus
 *   Stage 2 🟡 BUY — Accelerating, still good alpha
 *   Stage 3 🟢 HOLD — Known, thesis maturing
 *   Stage 4 ⚫ REDUCE — Supply easing, exit soon
 *
 * Every stock includes a plain-English auto-thesis so you can immediately
 * understand why it's on the list. No signal counting, no tab-hopping.
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchTodaysOpportunities } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

const TIER1_META = {
  tier_1: {
    label: '🏆 Tier 1 — QUALITY COMPOUNDERS',
    sub: 'Hold 5-20 years · ROIC >20% · competitive moat · expanding TAM',
    bg: 'bg-purple-950/40', border: 'border-purple-700/50',
    badge: 'bg-purple-900/60 text-purple-200', bar: '#a855f7',
  },
  tier_1_watch: {
    label: '👀 Tier 1 Watch — Potential Compounders',
    sub: 'Quality signals emerging · accumulate on dips',
    bg: 'bg-violet-950/30', border: 'border-violet-800/40',
    badge: 'bg-violet-900/40 text-violet-300', bar: '#8b5cf6',
  },
}

const STAGE_META = {
  1: {
    label: '🔴 Stage 1 — STRONG BUY',
    sub: 'Early edge · before analyst consensus · highest alpha window',
    bg: 'bg-red-950/40', border: 'border-red-700/50', badge: 'bg-red-900/60 text-red-200',
    bar: '#ef4444',
  },
  2: {
    label: '🟡 Stage 2 — BUY',
    sub: 'Accelerating · signals building · margin lift starting',
    bg: 'bg-amber-950/30', border: 'border-amber-700/40', badge: 'bg-amber-900/50 text-amber-200',
    bar: '#f59e0b',
  },
  3: {
    label: '🟢 Stage 3 — HOLD',
    sub: 'Consensus forming · still valid but shrinking alpha',
    bg: 'bg-emerald-950/20', border: 'border-emerald-800/30', badge: 'bg-emerald-900/40 text-emerald-300',
    bar: '#22c55e',
  },
  4: {
    label: '⚫ Stage 4 — REDUCE',
    sub: 'Supply easing · thesis closing · watch exit signals',
    bg: 'bg-slate-900/30', border: 'border-slate-700/30', badge: 'bg-slate-800/50 text-slate-400',
    bar: '#64748b',
  },
}

function StockCard({ s, defaultOpen }: { s: Record<string, unknown>; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const stage = Number(s.constraint_stage ?? 3) as 1|2|3|4
  const meta  = STAGE_META[stage] ?? STAGE_META[3]
  const score = Number(s.rank_score ?? 0)
  const thesis = String(s.auto_thesis ?? '')
  const cSigs  = Number(s.constraint_signals ?? 0)
  const kSigs  = Number(s.capex_signals ?? 0)
  const conf   = Number(s.avg_confidence ?? 0)
  const cQuote = String(s.best_constraint_quote ?? '')
  const kQuote = String(s.capex_quote ?? '')
  const exits  = (s.exit_triggers as string[]) ?? []
  const th     = Number(s.time_horizon_months ?? 12)
  const stageConf = Number(s.stage_confidence ?? 0)
  const component = String(s.constrained_component ?? '')
  const theme  = String(s.theme ?? '')
  const themes = (s.theme_names as string[]) ?? []

  return (
    <div className={`rounded-xl border overflow-hidden ${meta.bg} ${meta.border}`}
         style={{ borderLeft: `4px solid ${meta.bar}` }}>
      <button onClick={() => setOpen(o => !o)}
        className="w-full text-left px-4 py-3 hover:brightness-110 transition-all">
        <div className="flex items-start gap-3">
          {/* Score */}
          <div className="flex-shrink-0 text-center w-10">
            <div className="text-lg font-black text-amber-400 leading-none">{score.toFixed(0)}</div>
            <div className="text-[9px] text-slate-600">score</div>
          </div>
          {/* Company info */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="text-sm font-black text-slate-100">{String(s.ticker ?? '')}</span>
              <span className="text-xs text-slate-300">{String(s.company ?? '').slice(0,35)}</span>
              {component && <span className="text-[10px] text-amber-400 italic">🔩 {component}</span>}
              <div className="flex items-center gap-1.5 ml-auto flex-shrink-0">
                {cSigs > 0 && <span className="text-[10px] text-red-400 font-bold">⚠️{cSigs}</span>}
                {kSigs > 0 && <span className="text-[10px] text-amber-400 font-bold">🔨{kSigs}</span>}
                <span className="text-[10px] text-slate-600">⏱{th}m</span>
              </div>
            </div>
            {/* Auto-thesis — the plain English "why" */}
            <p className="text-[11px] text-slate-300 leading-relaxed">{thesis.slice(0,250)}</p>
          </div>
          <span className="text-slate-600 text-xs flex-shrink-0">{open ? '▲' : '▼'}</span>
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 pt-2 border-t border-slate-800/50 space-y-2">
          {/* Theme */}
          {theme && (
            <div className="text-[10px] text-indigo-400 font-medium">📌 {theme}</div>
          )}

          {/* Confidence breakdown */}
          <div className="flex gap-3 text-[10px] text-slate-500 flex-wrap">
            <span>Stage confidence: <strong className="text-slate-300">{(stageConf*100).toFixed(0)}%</strong></span>
            <span>Signal confidence: <strong className="text-slate-300">{(conf*100).toFixed(0)}%</strong></span>
            <span>Multi-theme: <strong className="text-slate-300">{themes.length}</strong></span>
          </div>

          {/* Best constraint quote */}
          {cQuote && (
            <div className="bg-red-950/20 border-l-4 border-red-700/50 rounded-r px-3 py-2">
              <div className="text-[9px] text-red-400 font-bold mb-0.5">⚠️ CONSTRAINT EVIDENCE</div>
              <p className="text-[11px] text-slate-300 italic leading-relaxed">"{cQuote.slice(0,250)}"</p>
            </div>
          )}

          {/* Capex quote */}
          {kQuote && (
            <div className="bg-amber-950/20 border-l-4 border-amber-700/50 rounded-r px-3 py-2">
              <div className="text-[9px] text-amber-400 font-bold mb-0.5">🔨 CAPACITY INVESTMENT</div>
              <p className="text-[11px] text-slate-300 italic leading-relaxed">"{kQuote.slice(0,200)}"</p>
            </div>
          )}

          {/* Multi-theme corroboration */}
          {themes.length > 1 && (
            <div className="flex flex-wrap gap-1">
              <span className="text-[9px] text-slate-500">Appears in {themes.length} themes:</span>
              {themes.slice(0,4).map((tn, i) => (
                <span key={i} className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-950/40 border border-indigo-800/40 text-indigo-400">{tn}</span>
              ))}
            </div>
          )}

          {/* Exit signals */}
          {exits.length > 0 && (
            <div className="bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2">
              <div className="text-[9px] text-slate-500 font-bold mb-1">⚠️ MONITOR THESE EXIT SIGNALS</div>
              {exits.map((e, i) => (
                <div key={i} className="text-[10px] text-slate-400">{e.replace(/_/g,' ')}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StageSection({ stage, stocks, defaultOpen }: { stage: 1|2|3|4; stocks: Record<string,unknown>[]; defaultOpen: boolean }) {
  const [collapsed, setCollapsed] = useState(!defaultOpen)
  const meta = STAGE_META[stage]
  if (!stocks.length) return null
  return (
    <div className="space-y-2">
      <button
        onClick={() => setCollapsed(c => !c)}
        className={`w-full flex items-center gap-3 px-4 py-2.5 rounded-xl border text-left ${meta.bg} ${meta.border} hover:brightness-110 transition-all`}
      >
        <div className="flex-1">
          <div className="text-sm font-black text-slate-100">{meta.label}</div>
          <div className="text-[11px] text-slate-400 mt-0.5">{meta.sub}</div>
        </div>
        <div className={`text-xl font-black px-3 py-1 rounded-lg ${meta.badge}`}>{stocks.length}</div>
        <span className="text-slate-500 text-xs">{collapsed ? '▼ show' : '▲ hide'}</span>
      </button>
      {!collapsed && (
        <div className="space-y-2 ml-2">
          {stocks.map((s, i) => (
            <StockCard key={i} s={s} defaultOpen={stage === 1 && i < 3} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function TodaysOpportunitiesTab({ country }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN'|'US'>(country === 'IN' ? 'IN' : 'US')
  const [asOfYear,     setAsOfYear]     = useState<number|undefined>(undefined)
  const [showAll,      setShowAll]      = useState(false)

  const qc = useQueryClient()

  const { data: raw, isLoading, isFetching, isError } = useQuery({
    queryKey: ['todays-opps', localCountry, asOfYear],
    queryFn:  () => fetchTodaysOpportunities(localCountry, asOfYear),
    staleTime: 10 * 60_000,   // 10 min cache
    refetchOnWindowFocus: false,
  })

  const data   = raw as Record<string,unknown> | undefined
  const t1     = (data?.tier1_compounders as Record<string,unknown>[]) ?? []
  const t1w    = (data?.tier1_watch        as Record<string,unknown>[]) ?? []
  const s1     = (data?.stage1_strong_buy  as Record<string,unknown>[]) ?? []
  const s2     = (data?.stage2_buy         as Record<string,unknown>[]) ?? []
  const s3     = (data?.stage3_hold        as Record<string,unknown>[]) ?? []
  const s4     = (data?.stage4_reduce      as Record<string,unknown>[]) ?? []
  const summary = (data?.summary as Record<string,number|string>) ?? {}
  const regimes = (data?.constraint_regimes as Record<string,unknown>[]) ?? []

  const total = Number(summary.total ?? 0) + t1.length + t1w.length

  return (
    <div className="space-y-4">

      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-base font-bold text-slate-100">🌅 Today's Investment Opportunities</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            The one tab you open every morning · {total} stocks · full justification trail · no human intervention
          </p>
        </div>
        {data && (
          <div className="text-[10px] text-slate-600">
            Generated {new Date(String(data.generated_at ?? '')).toLocaleTimeString()}
            {' '}· <button onClick={() => qc.invalidateQueries({queryKey:['todays-opps']})}
              className="text-indigo-500 hover:text-indigo-300 transition-colors">🔄 Refresh</button>
          </div>
        )}
      </div>

      {/* Country + Year */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN','US'] as const).map(c => (
            <button key={c} onClick={() => setLocalCountry(c)}
              className={`px-4 py-1.5 text-xs font-bold transition-colors ${localCountry===c?'bg-indigo-700 text-white':'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
              {c==='IN'?'🇮🇳 India':'🇺🇸 US'}
            </button>
          ))}
        </div>
        <div className="flex gap-1.5 flex-wrap">
          <button onClick={() => setAsOfYear(undefined)}
            className={`px-3 py-1 rounded text-xs font-bold border transition-colors ${!asOfYear?'bg-indigo-600 border-indigo-500 text-white':'bg-slate-800 border-slate-700 text-slate-300'}`}>
            Latest
          </button>
          {YEARS.map(y => (
            <button key={y} onClick={() => setAsOfYear(y)}
              className={`px-3 py-1 rounded text-xs font-bold border transition-colors ${asOfYear===y?'bg-indigo-600 border-indigo-500 text-white':'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'}`}>
              {y}
            </button>
          ))}
        </div>
      </div>

      {isLoading && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Spinner />
          <p className="text-sm text-slate-400">Scanning constraint signals across all companies…</p>
          <p className="text-xs text-slate-600">This runs the full investment funnel</p>
        </div>
      )}

      {isError && (
        <div className="bg-red-950/30 border border-red-800/50 rounded-xl px-4 py-3 text-sm text-red-300">
          ❌ Failed to load opportunities. Ensure the pipeline has been run for this year.
        </div>
      )}

      {data && !isLoading && (
        <>
          {/* Summary stats */}
          <div className="grid grid-cols-4 gap-2">
            {[
              { label: 'Stage 1 🔴', count: s1.length, color: 'text-red-400', bg: 'bg-red-950/40 border-red-800/40' },
              { label: 'Stage 2 🟡', count: s2.length, color: 'text-amber-400', bg: 'bg-amber-950/30 border-amber-800/40' },
              { label: 'Stage 3 🟢', count: s3.length, color: 'text-emerald-400', bg: 'bg-emerald-950/20 border-emerald-800/30' },
              { label: 'Stage 4 ⚫', count: s4.length, color: 'text-slate-400', bg: 'bg-slate-900/30 border-slate-700/30' },
            ].map(({ label, count, color, bg }) => (
              <div key={label} className={`rounded-xl border px-4 py-3 text-center ${bg}`}>
                <div className={`text-2xl font-black ${color}`}>{count}</div>
                <div className={`text-[11px] font-semibold ${color}`}>{label}</div>
              </div>
            ))}
          </div>

          {/* Active constraint regimes */}
          {regimes.length > 0 && (
            <div className="bg-slate-900/50 border border-slate-800 rounded-xl p-4">
              <div className="text-xs font-bold text-slate-300 mb-2">
                ⚡ Active Constraint Regimes ({regimes.length}) — themes driving these stocks
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-1.5">
                {regimes.slice(0,9).map((r, i) => {
                  const comps = (r.constrained_components as Record<string,unknown>[] ?? [])
                  return (
                    <div key={i} className="bg-slate-800/50 rounded-lg px-2.5 py-1.5">
                      <p className="text-[11px] text-slate-200 font-medium leading-snug">{String(r.theme_name??'')}</p>
                      {comps[0] && <span className="text-[10px] text-amber-400">🔩 {String(comps[0].component??'')}</span>}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {total === 0 ? (
            <EmptyState>
              No investment opportunities found for this period.<br/>
              Run the pipeline (NLP → Themes) to build signal data.
            </EmptyState>
          ) : (
            <div className="space-y-4">
              {/* Tier 1 — Quality Compounders (5-20 year holds) */}
              {(t1.length + t1w.length) > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-3 px-4 py-2.5 rounded-xl border bg-purple-950/40 border-purple-700/50">
                    <div className="flex-1">
                      <div className="text-sm font-black text-purple-100">🏆 Tier 1 — QUALITY COMPOUNDERS</div>
                      <div className="text-[11px] text-slate-400">Hold 5-20 years · ROIC ≥20% · durable moat · expanding TAM · The Titan/HDFC/Buffett pattern</div>
                    </div>
                    <div className="text-xl font-black px-3 py-1 rounded-lg bg-purple-900/60 text-purple-200">{t1.length + t1w.length}</div>
                  </div>
                  {t1.length > 0 && (
                    <div className="ml-2 space-y-2">
                      {t1.map((s, i) => (
                        <div key={i} className="bg-purple-950/30 border border-purple-800/40 rounded-xl px-4 py-3" style={{ borderLeft: '4px solid #a855f7' }}>
                          <div className="flex items-start gap-3">
                            <div className="flex-shrink-0 w-12 text-center">
                              <div className="text-lg font-black text-purple-300">{(Number(s.quality_score ?? 0)*100).toFixed(0)}</div>
                              <div className="text-[9px] text-slate-600">quality</div>
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 mb-1">
                                <span className="text-sm font-black text-slate-100">{String(s.ticker ?? '')}</span>
                                <span className="text-xs text-slate-300">{String(s.company ?? '').slice(0,35)}</span>
                                <span className="text-[10px] text-purple-400 ml-auto">🏆 {String(s.suggested_hold ?? '5-20y')}</span>
                              </div>
                              <div className="flex gap-3 text-[10px] text-slate-500 mb-1">
                                {Number(s.roic_score ?? 0) > 0.5 && <span className="text-emerald-400">ROIC ✓</span>}
                                {Number(s.moat_score ?? 0) > 0.5 && <span className="text-blue-400">Moat ✓</span>}
                                {Number(s.tam_score ?? 0) > 0.4 && <span className="text-amber-400">TAM ✓</span>}
                                <span>Confirmed {Number(s.signal_quarters ?? 1)}Q</span>
                              </div>
                              <p className="text-[10px] text-slate-400 leading-relaxed">{String(s.quality_thesis ?? '').slice(0,200)}</p>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {/* Tier 2 — Constraint Plays */}
              <div className="text-xs font-bold text-slate-400 uppercase tracking-wide px-1">
                Tier 2 — Constraint Cycle Winners (2-5 year holds)
              </div>
              <StageSection stage={1} stocks={s1} defaultOpen={true} />
              <StageSection stage={2} stocks={s2} defaultOpen={true} />
              <StageSection stage={3} stocks={s3} defaultOpen={false} />
              <StageSection stage={4} stocks={s4} defaultOpen={false} />
            </div>
          )}
        </>
      )}
    </div>
  )
}
