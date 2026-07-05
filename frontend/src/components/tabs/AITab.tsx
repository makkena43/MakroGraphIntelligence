/**
 * AI Investment Brief Tab
 *
 * One-click comprehensive investment brief powered by Claude.
 * Gathers ALL year-specific data internally (focus themes, constraint
 * companies, causal chains, capex signals, PLI policies) and returns:
 *   - Executive summary
 *   - Top 5-7 themes with full investment thesis
 *   - Top 5 industries to position in
 *   - Top 50 companies ranked by investability
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchSavedInvestmentBrief, runInvestmentBrief } from '../../api'
import { Spinner } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

const ACTION_COLOR: Record<string, string> = {
  act_now:  'text-red-300 bg-red-950/50 border-red-700/60',
  research: 'text-amber-300 bg-amber-950/40 border-amber-700/50',
  watch:    'text-sky-300 bg-sky-950/30 border-sky-800/40',
}
const FOCUS_COLOR: Record<string, string> = {
  new:        'text-emerald-400',
  escalating: 'text-red-400',
  no_prior:   'text-amber-400',
}
const CONV_DOT: Record<string, string> = {
  high: 'bg-emerald-400', medium: 'bg-amber-400', low: 'bg-slate-500',
}

type BriefResult = {
  year: number
  country: string
  market: string
  data_summary: {
    actionable_themes: number
    companies_analysed: number
    companies_with_evidence: number
    causal_chains: number
  }
  brief: {
    executive_summary?: string
    validation_notes?: string
    top_themes?: Record<string, unknown>[]
    top_industries?: Record<string, unknown>[]
    top_companies?: Record<string, unknown>[]
    excluded_companies?: Record<string, unknown>[]
    key_risks?: string[]
    contrarian_view?: string
    raw?: string
  }
  generated_at: string
  from_cache?: boolean
}

export default function AITab({ country, countryFlag, countryLabel }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear, setSelectedYear] = useState<number>(new Date().getFullYear())
  const [minConstraint, setMinConstraint] = useState(1)
  const [loading, setLoading]     = useState(false)
  const [result, setResult]       = useState<BriefResult | null>(null)
  const [error, setError]         = useState<string | null>(null)
  const [activeSection, setActiveSection] = useState<'themes' | 'industries' | 'companies' | 'risks'>('themes')

  const queryClient = useQueryClient()

  // Auto-load saved brief on mount / when year+country changes
  const { data: savedBrief, isLoading: savedLoading } = useQuery({
    queryKey: ['saved-brief', localCountry, selectedYear],
    queryFn:  () => fetchSavedInvestmentBrief(localCountry, selectedYear),
    staleTime: 5 * 60_000,
  })

  // Use saved brief if no fresh result has been generated this session
  const displayResult: BriefResult | null = result ?? (
    savedBrief && Object.keys(savedBrief).length > 0 ? savedBrief as BriefResult : null
  )

  const run = async (forceRefresh = false) => {
    setLoading(true)
    setResult(null)
    setError(null)
    try {
      const res = await runInvestmentBrief({
        country: localCountry,
        year: selectedYear,
        min_constraint: minConstraint,
        top_n_companies: 50,
        force_refresh: forceRefresh,
      })
      setResult(res as BriefResult)
      // Invalidate so the saved query reflects the new result
      queryClient.invalidateQueries({ queryKey: ['saved-brief', localCountry, selectedYear] })
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } }; message?: string })
        ?.response?.data?.detail ?? String(e)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  const brief = displayResult?.brief

  return (
    <div className="space-y-4">

      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-base font-bold text-slate-100">🤖 AI Investment Brief</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Claude-powered comprehensive brief — top themes, industries & 50 companies from real concall data
          </p>
        </div>
      </div>

      {/* How it works */}
      <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl px-4 py-3 text-xs text-indigo-200 leading-relaxed">
        <strong>How it works:</strong> The system gathers {localCountry === 'IN' ? 'India' : 'US'} constraint intelligence
        for {selectedYear} — NEW/ESCALATING themes (YoY delta), companies with supply_bottleneck + capex signals,
        active causal chains{localCountry === 'IN' ? ', PLI scheme context' : ''} — and feeds it to Claude Sonnet.
        Claude reasons over the data and returns a structured investment brief: thesis, industries, and 50 ranked companies.
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); setResult(null) }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>

        <div className="flex gap-1.5 flex-wrap">
          {YEARS.map(y => (
            <button key={y} onClick={() => { setSelectedYear(y); setResult(null) }}
              className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>{y}{y === new Date().getFullYear() ? ' YTD' : ''}</button>
          ))}
        </div>

        <div>
          <label className="text-xs text-slate-400 mr-2">Min ⚠️ signals</label>
          <select value={minConstraint} onChange={e => setMinConstraint(Number(e.target.value))} className="select">
            <option value={1}>≥ 1</option>
            <option value={2}>≥ 2</option>
            <option value={3}>≥ 3</option>
            <option value={5}>≥ 5</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <button onClick={() => run(false)} disabled={loading || savedLoading}
            className="flex items-center gap-2 px-5 py-2 rounded-xl bg-gradient-to-r from-indigo-700 to-purple-700 hover:from-indigo-600 hover:to-purple-600 disabled:opacity-50 text-white text-sm font-bold transition-all shadow-lg shadow-indigo-900/40">
            {loading ? <><Spinner /> Analysing…</> : displayResult ? '✨ Re-run Analysis' : '✨ Run AI Investment Brief'}
          </button>
          {displayResult && !loading && (
            <button onClick={() => run(true)} disabled={loading}
              className="px-3 py-2 rounded-xl bg-slate-800 border border-slate-700 hover:border-amber-600 text-slate-400 hover:text-amber-300 text-xs font-medium transition-colors">
              🔄 Force refresh
            </button>
          )}
        </div>
      </div>

      {/* Loading state */}
      {loading && (
        <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl p-6 text-center space-y-3">
          <div className="flex justify-center"><Spinner /></div>
          <p className="text-sm text-indigo-300 font-medium">Claude is analysing {selectedYear} constraint intelligence…</p>
          <p className="text-xs text-slate-500">
            Gathering themes · companies · causal chains → sending to Claude Sonnet → structuring results
          </p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-950/40 border border-red-700/50 rounded-xl px-4 py-3">
          <div className="text-sm font-bold text-red-300 mb-1">❌ Analysis Failed</div>
          <p className="text-xs text-red-400">{error}</p>
          {error.includes('API key') && (
            <p className="text-xs text-slate-500 mt-2">
              Add your Anthropic API key to <code className="text-slate-300">config/secrets.json</code> under <code className="text-slate-300">"anthropic": {'"api_key": "sk-ant-..."'}</code>
            </p>
          )}
        </div>
      )}

      {/* Results */}
      {displayResult && displayResult.brief && !loading && (
        <div className="space-y-4">

          {/* Meta banner */}
          <div className="bg-slate-800/60 border border-slate-700 rounded-xl px-4 py-2.5 flex items-center gap-4 flex-wrap text-xs">
            <span className="font-bold text-slate-200">{displayResult!.market} · {displayResult!.year}</span>
            <span className="text-indigo-400">{displayResult!.data_summary.actionable_themes} actionable themes</span>
            <span className="text-red-400">{displayResult!.data_summary.companies_analysed} companies scored</span>
            <span className="text-slate-500">{displayResult!.data_summary.causal_chains} causal chains</span>
            <span className="text-slate-600 ml-auto">Generated {new Date(displayResult!.generated_at).toLocaleTimeString()}</span>
          </div>

          {/* Executive Summary */}
          {brief?.executive_summary && (
            <div className="bg-gradient-to-r from-indigo-950/60 to-purple-950/40 border border-indigo-800/40 rounded-xl px-5 py-4">
              <div className="text-xs font-bold text-indigo-400 uppercase tracking-wider mb-2">
                📊 Executive Summary — {displayResult!.year}
              </div>
              <p className="text-sm text-slate-200 leading-relaxed">{brief?.executive_summary}</p>
              {brief?.validation_notes && (
                <p className="text-xs text-slate-500 mt-2 italic border-t border-slate-800/60 pt-2">
                  ✓ Validation: {brief?.validation_notes}
                </p>
              )}
            </div>
          )}

          {/* Raw fallback if JSON parsing failed */}
          {brief?.raw && (
            <div className="bg-slate-900 border border-slate-700 rounded-xl p-4">
              <div className="text-xs text-slate-500 mb-2">Raw Claude response (JSON parse failed):</div>
              <pre className="text-xs text-slate-300 whitespace-pre-wrap leading-relaxed">{brief?.raw}</pre>
            </div>
          )}

          {/* Section tabs */}
          {!brief?.raw && (
            <div className="flex gap-1 border-b border-slate-800 flex-wrap">
              {([
                { key: 'themes',    label: `🎯 Top Themes (${(brief?.top_themes ?? []).length})` },
                { key: 'industries',label: `🏭 Industries (${(brief?.top_industries ?? []).length})` },
                { key: 'companies', label: `📈 Companies (${(brief?.top_companies ?? []).length})` },
                { key: 'risks',     label: '⚠️ Risks & View' },
              ] as const).map(s => (
                <button key={s.key} onClick={() => setActiveSection(s.key)}
                  className={`px-4 py-2 text-xs font-medium rounded-t transition-colors ${
                    activeSection === s.key
                      ? 'bg-slate-800 text-indigo-300 border-b-2 border-indigo-500'
                      : 'text-slate-500 hover:text-slate-300'
                  }`}>{s.label}</button>
              ))}
            </div>
          )}

          {/* THEMES */}
          {activeSection === 'themes' && !brief?.raw && (
            <div className="space-y-3">
              {(brief?.top_themes ?? []).length === 0 && (
                <p className="text-sm text-slate-500 italic">No themes in response.</p>
              )}
              {(brief?.top_themes as Record<string, unknown>[] ?? []).map((t, i) => {
                const fc = String(t.focus ?? '')
                return (
                  <div key={i} className="bg-slate-900/70 border border-slate-700 rounded-xl p-4">
                    <div className="flex items-start justify-between gap-3 mb-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-slate-500 text-xs font-bold">#{Number(t.rank ?? i+1)}</span>
                        <span className={`text-[10px] font-bold ${FOCUS_COLOR[fc] ?? 'text-slate-400'}`}>
                          {fc.toUpperCase()}
                        </span>
                        <span className="text-sm font-bold text-slate-100">{String(t.name ?? '')}</span>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <div className={`w-2 h-2 rounded-full ${CONV_DOT[String(t.conviction ?? 'low')] ?? 'bg-slate-500'}`} />
                        <span className="text-[10px] text-slate-400 capitalize">{String(t.conviction ?? '')}</span>
                        <span className="text-[10px] bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5 text-slate-400">
                          {String(t.time_horizon ?? '')}
                        </span>
                      </div>
                    </div>
                    {!!t.constrained_component && (
                      <div className="text-[10px] text-amber-400 bg-amber-950/20 rounded px-2 py-1 mb-2">
                        🔩 Constrained: <strong>{String(t.constrained_component)}</strong>
                        {!!t.strength_delta && <span className="ml-2 text-red-400">{String(t.strength_delta)}</span>}
                      </div>
                    )}
                    <p className="text-xs text-slate-300 leading-relaxed mb-2">{String(t.investment_thesis ?? '')}</p>
                    <div className="grid grid-cols-2 gap-2 text-[11px]">
                      {!!t.key_catalyst && (
                        <div className="bg-emerald-950/30 rounded-lg px-2 py-1.5">
                          <span className="text-emerald-500 font-bold">⚡ Catalyst: </span>
                          <span className="text-slate-300">{String(t.key_catalyst)}</span>
                        </div>
                      )}
                      {!!t.key_risk && (
                        <div className="bg-red-950/20 rounded-lg px-2 py-1.5">
                          <span className="text-red-500 font-bold">⚠️ Risk: </span>
                          <span className="text-slate-400">{String(t.key_risk)}</span>
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* INDUSTRIES */}
          {activeSection === 'industries' && !brief?.raw && (
            <div className="space-y-2">
              {(brief?.top_industries as Record<string, unknown>[] ?? []).map((ind, i) => (
                <div key={i} className="bg-slate-900/70 border border-slate-700 rounded-xl px-4 py-3">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-slate-500 text-xs">#{Number(ind.rank ?? i+1)}</span>
                    <span className="text-sm font-bold text-slate-100">{String(ind.industry ?? '')}</span>
                  </div>
                  <p className="text-xs text-slate-300 leading-relaxed mb-1.5">{String(ind.rationale ?? '')}</p>
                  {(ind.leading_themes as string[] ?? []).length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {(ind.leading_themes as string[]).map((tn, ti) => (
                        <span key={ti} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-950/40 border border-indigo-800/40 text-indigo-300">
                          {tn}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* COMPANIES */}
          {activeSection === 'companies' && !brief?.raw && (
            <div className="space-y-1.5">
              {(brief?.top_companies as Record<string, unknown>[] ?? []).length === 0 && (
                <p className="text-sm text-slate-500 italic">No companies in response.</p>
              )}

              {/* Summary counts */}
              {(brief?.top_companies ?? []).length > 0 && (
                <div className="flex gap-3 text-xs text-slate-500 mb-2">
                  {(['act_now','research','watch'] as const).map(a => {
                    const n = (brief?.top_companies as Record<string,unknown>[] ?? []).filter(c => c.action === a).length
                    if (!n) return null
                    const label = a === 'act_now' ? '🔴 Act Now' : a === 'research' ? '🟡 Research' : '🔵 Watch'
                    return <span key={a}>{label}: <strong className="text-slate-300">{n}</strong></span>
                  })}
                </div>
              )}

              {(brief?.top_companies as Record<string, unknown>[] ?? [])
                .map((co, i) => {
                const action   = String(co.action ?? 'watch')
                const acMeta   = ACTION_COLOR[action] ?? ACTION_COLOR.watch
                const capex    = Boolean(co.capex_responding ?? co.capex_investing)
                const tier     = String(co.evidence_tier ?? 'theme_mapped')
                const theme_match = String(co.theme ?? '')
                const evidence = String(co.constraint_evidence_used ?? co.constraint_evidence ?? '')
                return (
                  <div key={i} className={`rounded-xl border px-4 py-2.5 ${acMeta}`}>
                    <div className="flex items-start gap-3">
                      <span className="text-slate-600 text-xs w-5 flex-shrink-0 pt-0.5">{Number(co.rank ?? i+1)}</span>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap mb-0.5">
                          <span className="text-sm font-black text-slate-100">{String(co.ticker ?? '')}</span>
                          <span className="text-xs text-slate-300">{String(co.company ?? '').slice(0,38)}</span>
                          {capex && <span className="text-[10px] text-amber-400 font-bold">🔨 Capex</span>}
                          <span className={`text-[10px] px-1 rounded font-medium ${ tier === 'strong' ? 'text-emerald-400 bg-emerald-950/40' : tier === 'moderate' ? 'text-blue-400 bg-blue-950/30' : tier === 'weak' ? 'text-amber-500 bg-amber-950/20' : 'text-slate-600 bg-slate-800/30' }`}>{tier}</span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded border font-bold ml-auto ${acMeta}`}>
                            {action.replace('_',' ').toUpperCase()}
                          </span>
                        </div>
                        {theme_match && (
                          <div className="text-[10px] text-indigo-400 mb-0.5">
                            📌 {theme_match}
                          </div>
                        )}
                        <p className="text-[11px] text-slate-300 leading-snug">
                          {String(co.thesis ?? co.one_line_thesis ?? '')}
                        </p>
                        {evidence && (
                          <p className="text-[10px] text-slate-500 italic mt-0.5 leading-relaxed border-l-2 border-slate-700 pl-1.5">
                            "{evidence.slice(0,180)}"
                          </p>
                        )}
                      </div>
                      <div className="flex-shrink-0 text-right">
                        <div className="text-sm font-black text-amber-400">{Number(co.investability_score ?? 0)}</div>
                        <div className="text-[9px] text-slate-600">score</div>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* RISKS + EXCLUDED */}
          {activeSection === 'risks' && !brief?.raw && (
            <div className="space-y-3">

              {(brief?.key_risks ?? []).length > 0 && (
                <div className="bg-red-950/20 border border-red-900/30 rounded-xl p-4">
                  <div className="text-xs font-bold text-red-400 uppercase tracking-wide mb-3">Key Risks</div>
                  <div className="space-y-2">
                    {(brief?.key_risks as string[]).map((risk, i) => (
                      <div key={i} className="flex items-start gap-2">
                        <span className="text-red-500 font-bold text-xs flex-shrink-0">{i+1}.</span>
                        <p className="text-xs text-slate-300">{risk}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {brief?.contrarian_view && (
                <div className="bg-amber-950/20 border border-amber-900/30 rounded-xl p-4">
                  <div className="text-xs font-bold text-amber-400 uppercase tracking-wide mb-2">
                    💡 Contrarian View
                  </div>
                  <p className="text-sm text-slate-200 leading-relaxed">{String(brief?.contrarian_view)}</p>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Saved brief notice */}
      {savedLoading && !loading && (
        <div className="text-xs text-slate-600 flex items-center gap-2 py-1">
          <Spinner /> Checking for saved analysis…
        </div>
      )}
      {displayResult?.from_cache && !loading && !result && (
        <div className="flex items-center gap-2 text-xs text-slate-500 bg-slate-900/40 border border-slate-800 rounded-lg px-3 py-1.5">
          <span className="text-emerald-500">✓ Loaded from cache</span>
          <span>— Generated {new Date(displayResult.generated_at).toLocaleDateString()}</span>
          <span className="text-slate-600">·</span>
          <span>Click <strong className="text-slate-300">Re-run Analysis</strong> to regenerate with latest data</span>
        </div>
      )}

      {/* Empty state */}
      {!displayResult && !savedLoading && !loading && !error && (
        <div className="border border-dashed border-slate-700 rounded-xl p-8 text-center space-y-2">
          <div className="text-3xl">✨</div>
          <p className="text-sm text-slate-400 font-medium">Select year + country and click Run AI Investment Brief</p>
          <p className="text-xs text-slate-600 leading-relaxed max-w-lg mx-auto">
            Claude will analyse all constraint themes, YoY escalations, company concall evidence,
            and causal chains for {selectedYear} — then produce a complete investment brief with
            top themes, industries, and 50 companies ranked by investability.
          </p>
        </div>
      )}
    </div>
  )
}
