import { useState } from 'react'
import { runRankings, runAIAnalysis } from '../../api'
import { CountryBanner, ConvictionBadge, EmptyState, Spinner } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

const ROLE_COLOR: Record<string, { bg: string; fg: string }> = {
  supply:      { bg: '#14532d', fg: '#4ade80' },
  beneficiary: { bg: '#1e3a5f', fg: '#93c5fd' },
  direct:      { bg: '#3b1f6e', fg: '#c4b5fd' },
}
const ROLE_LABEL: Record<string, string> = {
  supply: 'Supply', beneficiary: 'Beneficiary', direct: 'Direct',
}

export default function RankingTab({ country, countryFlag, countryLabel }: Props) {
  const [fromDate, setFromDate] = useState('2020-01-01')
  const [toDate, setToDate] = useState(today)
  const [topN, setTopN] = useState(15)
  const [roleFilter, setRoleFilter] = useState('All roles')
  const [minThemeCQ, setMinThemeCQ] = useState(0)
  const [themeFilter, setThemeFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{
    themes: Record<string, unknown>[]
    stocks: Record<string, unknown>[]
    date_from: string
    date_to: string
  } | null>(null)
  const [geminiLoading, setGeminiLoading] = useState(false)
  const [geminiResult, setGeminiResult] = useState<string | null>(null)

  const runRanking = async () => {
    setLoading(true)
    setResult(null)
    setGeminiResult(null)
    try {
      const res = await runRankings({ from_date: fromDate, to_date: toDate, top_n_themes: topN, country })
      setResult(res)
    } catch (e) {
      alert(`Ranking error: ${e}`)
    } finally {
      setLoading(false)
    }
  }

  const runGemini = async () => {
    if (!result) return
    setGeminiLoading(true)
    try {
      const market = country === 'IN' ? 'India (NSE/BSE)' : 'USA (NYSE/NASDAQ)'
      const themeLines = result.themes.slice(0, 15).map((t, i) =>
        `${i + 1}. ${t.theme_name} [${String(t.conviction ?? '').toUpperCase()}] | RankScore: ${Number(t.rank_score_pct ?? 0).toFixed(1)} | Momentum: ${Number(t.momentum ?? 0).toFixed(3)} | ${t.company_count} companies`
      ).join('\n')
      const stockLines = displayStocks.slice(0, 20).map(s =>
        `  #${s.rank} ${s.ticker} (${s.company_name}) | ${s.company_role} | Score: ${Number(s.final_score ?? 0).toFixed(4)} | BestEdgeCQ: ${Number(s.effective_theme ?? 0).toFixed(3)} | Themes: ${(s.themes as string[]).slice(0, 3).join(', ')}`
      ).join('\n')
      const prompt = `You are an expert thematic portfolio manager. The following stocks were ranked using multi-factor thematic analysis of ${market} company filings.\n\nACTIVE THEMES (${result.themes.length} themes):\n${themeLines}\n\nTOP RANKED STOCKS (by Best-Edge CQ score):\n${stockLines}\n\nProvide:\n1. Top 5 high-conviction positions with brief rationale (1-2 sentences each)\n2. Portfolio construction guidance (supply chain vs end beneficiary vs direct plays)\n3. Key theme concentration risks to hedge\n4. One contrarian view worth considering\n\nBe concise and actionable. Write for a professional portfolio manager.`
      const res = await runAIAnalysis({ prompt, mode: 'portfolio', market, themes_count: result.themes.length, stocks_count: result.stocks.length })
      setGeminiResult(res.result)
    } catch (e) {
      setGeminiResult(`Error: ${e}`)
    } finally {
      setGeminiLoading(false)
    }
  }

  const roleMap: Record<string, string> = {
    'Supply only': 'supply', 'Beneficiary only': 'beneficiary', 'Direct only': 'direct',
  }
  const roleKey = roleMap[roleFilter]
  const _themeKw = themeFilter.trim().toLowerCase()
  const displayStocks = result
    ? (result.stocks as Record<string, unknown>[])
        .filter(s => !roleKey || s.company_role === roleKey)
        .filter(s => {
          if (!minThemeCQ || minThemeCQ <= 0) return true
          const cq = (s.cq_breakdown as Record<string, unknown>) ?? {}
          return Number(cq['Theme CQ'] ?? 0) >= minThemeCQ
        })
        .filter(s => {
          if (!_themeKw) return true
          return (s.themes as string[]).some(t => t.toLowerCase().includes(_themeKw))
        })
    : []

  const supplyCnt = displayStocks.filter(s => s.company_role === 'supply').length
  const beneCnt   = displayStocks.filter(s => s.company_role === 'beneficiary').length
  const dirCnt    = displayStocks.filter(s => s.company_role === 'direct').length

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel} />

      <div className="bg-amber-950/30 border-l-4 border-amber-500 rounded-r-lg px-4 py-2 text-sm text-amber-200">
        🏆 <strong>Stock Rankings</strong> — post-detection ranking layer. Theme Strength × Supplier Quality × Confluence × Category Weight
        <span className="text-xs text-slate-500 ml-2">(v6: best-edge dominant · Final = 0.55×BestEdgeCQ + 0.25×AvgTop3 + 0.20×SupplierQ × Confluence × CatWt)</span>
      </div>

      {/* Controls */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 items-end">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Date from</label>
          <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Date to</label>
          <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Top N themes (momentum)</label>
          <select value={topN} onChange={e => setTopN(+e.target.value)} className="select w-full">
            {[10, 15, 20, 30].map(n => <option key={n}>{n}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Role filter</label>
          <select value={roleFilter} onChange={e => setRoleFilter(e.target.value)} className="select w-full">
            {['All roles', 'Supply only', 'Beneficiary only', 'Direct only'].map(r => <option key={r}>{r}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min ThemeCQ ≥</label>
          <input
            type="number" min={0} max={1} step={0.05}
            value={minThemeCQ}
            onChange={e => { const v = parseFloat(e.target.value); setMinThemeCQ(isNaN(v) ? 0 : Math.max(0, Math.min(1, v))) }}
            className="input w-full"
            placeholder="0.00"
          />
        </div>
        <button onClick={runRanking} disabled={loading} className="btn-primary flex items-center gap-2">
          {loading ? <Spinner size="sm" /> : '▶'} {loading ? 'Running…' : 'Run Ranking'}
        </button>
      </div>

      {/* Stock filters row */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500 text-sm">🔍</span>
          <input
            type="text"
            value={themeFilter}
            onChange={e => setThemeFilter(e.target.value)}
            placeholder="Filter stocks by theme keyword…"
            className="input pl-8 w-full"
          />
        </div>
        {themeFilter && (
          <button onClick={() => setThemeFilter('')} className="text-xs text-slate-500 hover:text-red-400 transition-colors">
            ✕ Clear
          </button>
        )}
        {themeFilter && (
          <span className="text-xs text-slate-500">
            {displayStocks.length} stock{displayStocks.length !== 1 ? 's' : ''} matching <span className="text-indigo-400 font-medium">"{themeFilter}"</span>
          </span>
        )}
      </div>

      {loading && <div className="flex justify-center py-12"><Spinner /></div>}

      {!result && !loading && (
        <EmptyState>
          <div className="text-3xl mb-2">🏆</div>
          Select a date range and click <strong>▶ Run Ranking</strong>
          <div className="text-xs text-slate-600 mt-2">Any theme with ThemeCQ ≥ 0.45 is also included automatically (bottleneck quality floor), so critical themes always enter the pool even outside top-N.</div>
        </EmptyState>
      )}

      {result && !loading && (
        <>
          {/* Meta banner */}
          <div className="bg-slate-800/80 border border-slate-700 rounded-xl px-4 py-2 text-sm text-slate-400 flex flex-wrap gap-3">
            <span>📅 {result.date_from} → {result.date_to}</span>
            <span>· Top <strong className="text-amber-400">{topN}</strong> themes</span>
            <span>· <strong className="text-emerald-400">{displayStocks.length}</strong> stocks</span>
            <span className="text-emerald-400">({supplyCnt} supply</span>
            <span className="text-sky-400">· {beneCnt} bene</span>
            <span className="text-violet-400">· {dirCnt} direct)</span>
          </div>

          {/* ── Section A: Theme Scores ─────────────────────────────────────── */}
          <details open>
            <summary className="cursor-pointer text-sm font-semibold text-slate-200 py-2 hover:text-white">
              📊 Theme Scores — rank_score + ThemeCQ (dual-criterion pool)
            </summary>
            <p className="text-xs text-slate-500 mb-2 mt-1">
              Themes enter by <strong>rank_score</strong> (momentum quota) OR <strong>ThemeCQ ≥ 0.45</strong> (bottleneck quality floor).
              <span className="text-amber-400 ml-1">★ = CQ-floor entry (would be excluded under old top-N-only selection)</span>
            </p>
            <div className="overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['★', 'Theme', 'ThemeCQ', 'Co.', 'Conviction', 'RankScore', 'Momentum', 'Persist.', 'Novelty', 'Sig.Int', 'First Detected', 'Freshness'].map(h => (
                      <th key={h} className="px-2 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(result.themes as Record<string, unknown>[]).map((t, i) => {
                    const cqFloor = Boolean(t.from_cq_floor)
                    const fd = String(t.first_detected ?? '').slice(0, 10)
                    const fdAge = fd ? Math.floor((Date.now() - new Date(fd).getTime()) / 86400_000) : null
                    const fresh = !fdAge ? '❓' : fdAge <= 90 ? '🟢 Fresh' : fdAge <= 365 ? '🟡 Active' : '🔴 Mature'
                    return (
                      <tr key={i} className={`border-b border-slate-800 hover:bg-slate-800/50 ${cqFloor ? 'bg-amber-950/10' : ''}`}>
                        <td className="px-2 py-2 text-amber-400 font-bold text-center">{cqFloor ? '★' : ''}</td>
                        <td className="px-2 py-2 text-white font-semibold max-w-[200px] truncate">{String(t.theme_name ?? '')}</td>
                        <td className="px-2 py-2">
                          <div className="flex items-center gap-1">
                            <div className="h-1.5 rounded bg-indigo-600/30 w-16 overflow-hidden">
                              <div className="h-full bg-indigo-500 rounded" style={{ width: `${Math.min(100, Number(t.theme_cq ?? 0) * 100)}%` }} />
                            </div>
                            <span className="text-indigo-400 font-bold">{Number(t.theme_cq ?? 0).toFixed(3)}</span>
                          </div>
                        </td>
                        <td className="px-2 py-2 text-slate-300 text-center">{String(t.company_count ?? 0)}</td>
                        <td className="px-2 py-2"><ConvictionBadge conviction={String(t.conviction ?? 'emerging')} /></td>
                        <td className="px-2 py-2">
                          <div className="flex items-center gap-1">
                            <div className="h-1.5 rounded bg-amber-600/30 w-16 overflow-hidden">
                              <div className="h-full bg-amber-500 rounded" style={{ width: `${Math.min(100, Number(t.rank_score_pct ?? 0))}%` }} />
                            </div>
                            <span className="text-amber-400">{Number(t.rank_score_pct ?? 0).toFixed(1)}</span>
                          </div>
                        </td>
                        <td className="px-2 py-2 text-slate-300">{Number(t.momentum ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-2 text-slate-300">{Number(t.persistence ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-2 text-slate-300">{Number(t.novelty ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-2 text-slate-300">{Number(t.signal_intensity ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-2 text-slate-400">{fd || '—'}</td>
                        <td className="px-2 py-2 text-slate-400">{fresh}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </details>

          {/* ── Section B: Stock Ranking Cards ────────────────────────────── */}
          <div>
            <h3 className="text-sm font-bold text-slate-100 mb-3">🏅 Ranked Stock Candidates</h3>
            {displayStocks.length === 0 ? (
              <EmptyState>No ranked stocks found for this period. Run the pipeline across the selected date range first.</EmptyState>
            ) : (
              <div className="space-y-2">
                {displayStocks.slice(0, 50).map((s, i) => (
                  <StockCard key={i} s={s} />
                ))}
              </div>
            )}
          </div>

          {/* ── Section C: Full table ─────────────────────────────────────── */}
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-slate-400 py-2 hover:text-slate-200 border-t border-slate-800">
              📋 Full ranking table — {displayStocks.length} stocks
            </summary>
            <div className="mt-2 overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['Rank','Ticker','Company','First Seen','Fresh','Role','Conf.','Cat.Wt','BestEdgeCQ','ThemeCQ','Decay','SigFactor','Supplier','N Constr.','Conf Bonus','Final Score','Themes'].map(h => (
                      <th key={h} className="px-2 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {displayStocks.map((s, i) => {
                    const cq = (s.cq_breakdown as Record<string, unknown>) ?? {}
                    const fd = String(s.first_seen_at ?? '').slice(0, 10)
                    const fdAge = fd ? Math.floor((Date.now() - new Date(fd).getTime()) / 86400_000) : null
                    const fresh = !fdAge ? '❓' : fdAge <= 90 ? '🟢' : fdAge <= 365 ? '🟡' : '🔴'
                    return (
                      <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                        <td className="px-2 py-1.5 text-amber-400 font-black">#{String(s.rank ?? i + 1)}</td>
                        <td className="px-2 py-1.5 text-indigo-300 font-bold">{String(s.ticker ?? '—')}</td>
                        <td className="px-2 py-1.5 text-slate-300 max-w-[120px] truncate">{String(s.company_name ?? '')}</td>
                        <td className="px-2 py-1.5 text-slate-500">{fd || '—'}</td>
                        <td className="px-2 py-1.5 text-center">{fresh}</td>
                        <td className="px-2 py-1.5 text-slate-400 capitalize">{String(s.company_role ?? '')}</td>
                        <td className="px-2 py-1.5 text-slate-400">{Number(s.role_confidence ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-slate-300">{Number(s.category_weight ?? 1).toFixed(1)}×</td>
                        <td className="px-2 py-1.5 text-indigo-400 font-bold">{Number(s.constraint_quality ?? 0).toFixed(4)}</td>
                        <td className="px-2 py-1.5 text-indigo-300">{Number(cq['Theme CQ'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-slate-400">{Number(cq['Role Decay'] ?? 0).toFixed(2)}</td>
                        <td className="px-2 py-1.5 text-slate-400">{Number(cq['Signal Factor'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-emerald-400">{Number(s.supplier_quality ?? 0).toFixed(4)}</td>
                        <td className="px-2 py-1.5 text-center text-sky-300">{String(cq['N Constraints'] ?? 0)}</td>
                        <td className="px-2 py-1.5 text-violet-400">{Number(cq['Conf Bonus'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-amber-400 font-black">{Number(s.final_score ?? 0).toFixed(4)}</td>
                        <td className="px-2 py-1.5 text-slate-500 max-w-[200px] truncate">{(s.themes as string[]).slice(0, 3).join('; ')}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </details>

          {/* ── Supplier Quality Breakdown ────────────────────────────────── */}
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-slate-400 py-2 hover:text-slate-200 border-t border-slate-800">
              🔧 Supplier Quality Breakdown — top 20
            </summary>
            <div className="mt-2 overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['Ticker','Company','Avg Quality','Moat Score','Supplier Focus','Capex Intensity','Margin Proxy'].map(h => (
                      <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {displayStocks.slice(0, 20).map((s, i) => {
                    const qb = (s.quality_breakdown as Record<string, number>) ?? {}
                    return (
                      <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                        <td className="px-3 py-1.5 text-indigo-300 font-bold">{String(s.ticker ?? '—')}</td>
                        <td className="px-3 py-1.5 text-slate-300 max-w-[140px] truncate">{String(s.company_name ?? '')}</td>
                        <td className="px-3 py-1.5 text-emerald-400 font-bold">{Number(s.supplier_quality ?? 0).toFixed(3)}</td>
                        {['moat_score','supplier_focus','capex_intensity','gross_margin_proxy'].map(k => (
                          <td key={k} className="px-3 py-1.5 text-slate-300">{(qb[k] ?? 0).toFixed(2)}</td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </details>

          {/* ── Best-Edge CQ Breakdown ────────────────────────────────────── */}
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-slate-400 py-2 hover:text-slate-200 border-t border-slate-800">
              ⚡ Best-Edge CQ Breakdown — top 20
            </summary>
            <p className="text-xs text-slate-500 mt-1 mb-2">
              v6 architecture: Final = (0.55×BestEdgeCQ + 0.25×AvgTop3 + 0.20×SupplierQ) × (1+ConfluenceBonus) × CategoryWeight
            </p>
            <div className="overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['Rank','Ticker','Company','Role','Best Theme','Theme CQ','Role Decay','Sig Factor','Best Edge CQ','N Constraints','Conf Bonus','Final Score'].map(h => (
                      <th key={h} className="px-2 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {displayStocks.slice(0, 20).map((s, i) => {
                    const cq = (s.cq_breakdown as Record<string, unknown>) ?? {}
                    const roleStyle = ROLE_COLOR[String(s.company_role ?? '')] ?? { fg: '#94a3b8' }
                    return (
                      <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                        <td className="px-2 py-1.5 text-amber-400 font-black">#{String(s.rank ?? i + 1)}</td>
                        <td className="px-2 py-1.5 text-indigo-300 font-bold">{String(s.ticker ?? '—')}</td>
                        <td className="px-2 py-1.5 text-slate-300 max-w-[100px] truncate">{String(s.company_name ?? '')}</td>
                        <td className="px-2 py-1.5 font-medium" style={{ color: roleStyle.fg }}>{String(s.company_role ?? '').replace(/_/g, ' ')}</td>
                        <td className="px-2 py-1.5 text-violet-300 max-w-[140px] truncate">{String(cq['Best Theme'] ?? '—').slice(0, 35)}</td>
                        <td className="px-2 py-1.5 text-indigo-400 font-bold">{Number(cq['Theme CQ'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-slate-400">{Number(cq['Role Decay'] ?? 0).toFixed(2)}</td>
                        <td className="px-2 py-1.5 text-slate-400">{Number(cq['Signal Factor'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-emerald-400 font-bold">{Number(cq['Best Edge CQ'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-sky-300 text-center">{String(cq['N Constraints'] ?? 0)}</td>
                        <td className="px-2 py-1.5 text-violet-400">{Number(cq['Conf Bonus'] ?? 0).toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-amber-400 font-black">{Number(s.final_score ?? 0).toFixed(4)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </details>

          {/* ── Gemini Analysis ───────────────────────────────────────────── */}
          <div className="border-t border-slate-800 pt-4">
            <div className="bg-purple-950/30 border-l-4 border-purple-600 rounded-r-lg px-4 py-2 text-sm text-purple-200 mb-3">
              🤖 <strong>Gemini AI Analysis</strong> · Google Gemini Flash · {countryLabel} ·{' '}
              {result.themes.length} themes · {displayStocks.length} stocks
            </div>
            <div className="flex gap-3">
              <button onClick={runGemini} disabled={geminiLoading} className="btn-primary flex items-center gap-2">
                {geminiLoading ? <Spinner size="sm" /> : '✨'} Run Gemini Analysis
              </button>
              {geminiResult && <button onClick={() => setGeminiResult(null)} className="btn-secondary">🗑 Clear</button>}
            </div>
            {geminiResult && (
              <div className="mt-4 bg-purple-950/20 border border-purple-900/40 rounded-xl p-5">
                <div className="text-xs text-purple-400 font-bold uppercase tracking-wider mb-3">🤖 Gemini Flash — Portfolio Analysis</div>
                <div className="text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">{geminiResult}</div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Rich Stock Card ──────────────────────────────────────────────────────────
function StockCard({ s }: { s: Record<string, unknown> }) {
  const role = String(s.company_role ?? '')
  const roleStyle = ROLE_COLOR[role] ?? { bg: '#1e293b', fg: '#94a3b8' }
  const roleLabel = ROLE_LABEL[role] ?? role
  const roleConf = Math.round(Number(s.role_confidence ?? 0) * 100)
  const catWt = Number(s.category_weight ?? 1)
  const finalScore = Number(s.final_score ?? 0)
  const effectiveTheme = Number(s.effective_theme ?? 0)
  const supplierQ = Number(s.supplier_quality ?? 0)
  const confluenceScore = Number(s.confluence_score ?? 0)
  const freshness = String(s.freshness ?? '❓')
  const scoreBarPct = Math.min(100, Math.round(finalScore * 35))
  const cq = (s.cq_breakdown as Record<string, unknown>) ?? {}
  const themes = (s.themes as string[]) ?? []
  const themeSlugs = (s.theme_slugs as string[]) ?? []
  const perEdges = (s.per_theme_edges as Record<string, number>) ?? {}
  const signals = (s.signal_highlights as string[]) ?? []

  const freshColor = freshness.startsWith('🟢') ? '#22c55e'
    : freshness.startsWith('🟡') ? '#f59e0b'
    : freshness.startsWith('🔴') ? '#ef4444'
    : '#475569'

  return (
    <div className="bg-slate-800 border border-slate-700 rounded-xl p-4" style={{ borderLeft: `3px solid ${roleStyle.fg}` }}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="text-slate-500 text-xs font-bold min-w-[28px]">#{String(s.rank ?? '')}</span>
        <span className="text-amber-400 font-black text-lg leading-none">{String(s.ticker ?? '')}</span>
        <span className="text-slate-200 text-sm">{String(s.company_name ?? '').slice(0, 50)}</span>
        <span className="ml-auto text-xs font-bold px-2 py-0.5 rounded border"
          style={{ background: `${roleStyle.bg}`, color: roleStyle.fg, borderColor: `${roleStyle.fg}33` }}>
          {roleLabel} · {catWt}× <span className="opacity-60">({roleConf}% conf)</span>
        </span>
        <span className="text-xs" style={{ color: freshColor }}>{freshness}</span>
      </div>

      {/* Score bar */}
      <div className="h-1.5 bg-slate-900 rounded-full mb-3 overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${scoreBarPct}%`, background: 'linear-gradient(90deg,#f59e0b,#fbbf24)' }} />
      </div>

      {/* Score breakdown row */}
      <div className="flex gap-4 mb-2 flex-wrap">
        {[
          ['Final Score',     finalScore.toFixed(4),        '#f59e0b'],
          ['Best Edge CQ 55%',effectiveTheme.toFixed(3),    '#818cf8'],
          ['Supplier 20%',    supplierQ.toFixed(3),          '#22c55e'],
          ['Constraints',     `${confluenceScore.toFixed(1)} (${themes.length}T)`, '#38bdf8'],
          ['Cat.Wt',          `${catWt}×`,                   roleStyle.fg],
        ].map(([label, val, color]) => (
          <div key={String(label)}>
            <div className="text-xs text-slate-500 uppercase tracking-wider leading-none">{String(label)}</div>
            <div className="font-black text-sm mt-0.5" style={{ color: String(color) }}>{String(val)}</div>
          </div>
        ))}
      </div>

      {/* Best Constraint block */}
      <div className="bg-slate-900 rounded-lg px-3 py-2 mb-2 flex items-center gap-3 flex-wrap">
        <span className="text-xs text-slate-500 whitespace-nowrap uppercase tracking-wide">Best Constraint</span>
        <span className="text-violet-300 text-xs flex-1 truncate">{String(cq['Best Theme'] ?? '—')}</span>
        <span className="text-pink-400 text-xs whitespace-nowrap">
          ThemeCQ={Number(cq['Theme CQ'] ?? 0).toFixed(3)} · Decay={Number(cq['Role Decay'] ?? 0).toFixed(1)} · Sig={Number(cq['Signal Factor'] ?? 0).toFixed(2)} → EdgeCQ=<strong>{Number(cq['Best Edge CQ'] ?? 0).toFixed(3)}</strong>
        </span>
      </div>

      {/* Theme + edge pills */}
      <div className="flex flex-wrap gap-1.5 mb-1.5">
        <span className="text-xs text-slate-500 mr-1 self-center uppercase tracking-wide">Themes + Edge</span>
        {themes.slice(0, 4).map((tn, ti) => {
          const slug = themeSlugs[ti] ?? ''
          const edge = perEdges[slug] ?? 0
          return (
            <span key={ti} className="text-xs px-1.5 py-0.5 rounded border border-sky-900 bg-sky-950/40 text-sky-300">
              {tn.slice(0, 28)}<span className="text-slate-500 ml-1">e={edge.toFixed(2)}</span>
            </span>
          )
        })}
      </div>

      {/* Signal highlights */}
      <div className="flex flex-wrap gap-1.5">
        <span className="text-xs text-slate-500 mr-1 self-center uppercase tracking-wide">Signals</span>
        {signals.length > 0
          ? signals.map((sh, i) => (
            <span key={i} className="text-xs px-1.5 py-0.5 rounded border border-emerald-900 bg-emerald-950/30 text-emerald-300">{sh}</span>
          ))
          : <span className="text-xs text-slate-600">no signal highlights</span>
        }
      </div>
    </div>
  )
}
