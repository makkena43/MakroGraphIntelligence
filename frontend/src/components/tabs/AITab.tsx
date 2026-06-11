import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchThemes, fetchShortlisted, fetchAICache, runAIAnalysis } from '../../api'
import { CountryBanner, EmptyState, Spinner } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

const MODES = [
  { key: 'theme',       label: '🔍 Theme Focus',      desc: 'In-depth analysis of all active themes + connections' },
  { key: 'bottleneck',  label: '⚠️ Bottleneck Focus', desc: 'Supply constraint, risk & second-order effects' },
  { key: 'portfolio',   label: '🏆 Portfolio Focus',  desc: 'Ranked stocks, positioning & construction guidance' },
  { key: 'master',      label: '🌐 Master Analysis',  desc: 'All themes + bottlenecks + stocks (comprehensive brief)' },
]

export default function AITab({ country, countryFlag, countryLabel }: Props) {
  const [fromDate, setFromDate] = useState(
    country === 'IN' ? '2020-01-01' : new Date(Date.now() - 365 * 86400_000).toISOString().slice(0, 10)
  )
  const [toDate, setToDate] = useState(today)
  const [mode, setMode] = useState('master')
  const [result, setResult] = useState<{
    result: string; mode: string; market: string
    themes_count: number; sl_count: number; bottlenecks_count: number; stocks_count: number; generated_at: string
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [cacheYear, setCacheYear] = useState<string>('')

  const { data: allThemes = [] } = useQuery({
    queryKey: ['ai-themes', country, fromDate, toDate],
    queryFn: () => fetchThemes(country, { as_of: toDate, from_date: fromDate, min_strength: 0 }),
  })

  const { data: slThemes = [] } = useQuery({
    queryKey: ['ai-sl', country],
    queryFn: () => fetchShortlisted(country, 2),
  })

  const { data: aiCache = {} } = useQuery({
    queryKey: ['ai-cache', country],
    queryFn: () => fetchAICache(country),
  })

  const themes = allThemes as Record<string, unknown>[]
  const sl = slThemes as Record<string, unknown>[]
  const cache = aiCache as Record<string, Record<string, unknown>>

  const bottlenecks = themes.filter(t => {
    const m = (t.metadata && typeof t.metadata === 'string') ? JSON.parse(t.metadata) : (t.metadata ?? {}) as Record<string, unknown>
    return m.theme_type === 'bottleneck' || m.is_bottleneck || Number(m.constraint_kw_count ?? 0) >= 3
  })

  const market = country === 'IN' ? 'India (NSE/BSE)' : 'USA (NYSE/NASDAQ)'
  const window = `${fromDate} – ${toDate} | Market: ${market}`

  const buildPrompt = (): string => {
    const themeLines = themes.slice(0, 25).map((t, i) => {
      const m = (t.metadata && typeof t.metadata === 'string') ? JSON.parse(String(t.metadata)) : (t.metadata ?? {}) as Record<string, unknown>
      const ttype = String(m.theme_type ?? 'auto')
      const bn = (m.theme_type === 'bottleneck' || m.is_bottleneck || Number(m.constraint_kw_count ?? 0) >= 3) ? ' 🔴[BOTTLENECK]' : ''
      return `${i + 1}. ${t.theme_name} [${String(t.conviction ?? 'emerging').toUpperCase()}]${bn} | Score:${Number(t.strength_score ?? 0).toFixed(0)} | Q:${t.confirmed_quarters ?? 0} | Cos:${t.company_count ?? 0} | Type:${ttype}`
    }).join('\n') || 'No themes detected.'

    const slLines = sl.slice(0, 15).map((t, i) =>
      `${i + 1}. ${t.theme_name} [${String(t.conviction ?? 'emerging').toUpperCase()}] | Score:${Number(t.strength_score ?? 0).toFixed(0)} | ${t.confirmed_quarters ?? 0} quarters | ${t.company_count ?? 0} companies`
    ).join('\n') || 'No shortlisted themes yet.'

    const bnLines = bottlenecks.slice(0, 10).map((t, i) =>
      `${i + 1}. ${t.theme_name} | Score:${Number(t.strength_score ?? 0).toFixed(0)} | Companies:${t.company_count ?? 0}`
    ).join('\n') || 'No bottleneck themes detected.'

    if (mode === 'theme') return `You are an expert macro investment analyst covering ${market}.\nAnalysis window: ${window}\n\nALL ACTIVE INVESTMENT THEMES (${themes.length}):\n${themeLines}\n\nSHORTLISTED THEMES (≥2 sustained quarters, ${sl.length}):\n${slLines}\n\nProvide a detailed theme analysis:\n1. **Top 5 Themes** with the strongest multi-year investment case (2-3 sentences each)\n2. **Cross-Theme Connections** — amplifying or conflicting forces\n3. **Emerging Themes** — just appeared or accelerating fast\n4. **Key Macro Risks** across these themes\n5. **Sector Rotation Implications**\n\nBe specific, data-driven, and actionable. Write for a professional equity investor.`

    if (mode === 'bottleneck') return `You are an expert macro analyst specializing in supply constraints for ${market}.\nAnalysis window: ${window}\n\nALL ACTIVE THEMES (${themes.length}):\n${themeLines}\n\nIDENTIFIED BOTTLENECK / SUPPLY-CONSTRAINT THEMES (${bottlenecks.length}):\n${bnLines}\n\nProvide a supply constraint analysis:\n1. **Critical Bottlenecks** — why each matters and expected duration\n2. **Second-Order Effects** — which downstream sectors are most exposed\n3. **Resolution Timeline** — which constraints ease vs. persist (6–18 month view)\n4. **Beneficiaries** — companies/sectors that profit from constraint resolution\n5. **Hedging Strategies** — how to protect portfolios exposed to these constraints\n\nBe specific and data-driven. Write for a professional risk manager.`

    if (mode === 'portfolio') return `You are an expert thematic portfolio manager covering ${market}.\nAnalysis window: ${window}\n\nACTIVE THEMES (${themes.length}):\n${themeLines}\n\nSHORTLISTED THEMES (${sl.length}):\n${slLines}\n\nProvide:\n1. **Top 10 Stock Picks** with brief rationale (1-2 sentences each)\n2. **Portfolio Construction**:\n   - Tier 1 Core (high conviction, full position)\n   - Tier 2 Tactical (medium conviction, half position)\n   - Tier 3 Speculative (asymmetric upside, small position)\n3. **Concentration Risks** — key theme overlaps to hedge\n4. **Sizing Guidance** — suggested % weights per tier\n5. **Contrarian View** — one idea the market is underpricing\n\nBe concise and actionable. Write for a professional portfolio manager.`

    return `You are an elite macro investment research team covering ${market}. Produce a comprehensive investment brief.\nAnalysis window: ${window}\n\n=== PIPELINE DATA ===\n\nALL ACTIVE THEMES (${themes.length}):\n${themeLines}\n\nSUSTAINED SHORTLISTED THEMES (≥2 quarters, ${sl.length}):\n${slLines}\n\nSUPPLY-CHAIN BOTTLENECKS (${bottlenecks.length}):\n${bnLines}\n\n=== COMPREHENSIVE INVESTMENT BRIEF ===\n\n**1. MACRO LANDSCAPE** (3-4 sentences)\n   Overall structural forces and investment environment.\n\n**2. TOP 5 CONVICTION THEMES**\n   For each: thesis (2 sentences) | key sectors | time horizon | key risk.\n\n**3. BOTTLENECK & CONSTRAINT ANALYSIS**\n   Critical supply constraints, second-order downstream effects, duration estimates.\n\n**4. TOP 10 STOCK RECOMMENDATIONS**\n   For each: role (supply/demand/direct) | 1-sentence rationale | conviction level.\n\n**5. PORTFOLIO CONSTRUCTION**\n   Tier 1 core | Tier 2 tactical | Tier 3 speculative. Suggested weight ranges.\n\n**6. KEY RISKS & HEDGES**\n   Top 3 macro risks and suggested hedges.\n\n**7. CONTRARIAN VIEW**\n   One underappreciated angle the consensus is missing.\n\nUse markdown headers and bullet points. Be specific, data-driven, actionable.`
  }

  const runAnalysis = async () => {
    setLoading(true)
    setResult(null)
    try {
      const prompt = buildPrompt()
      const res = await runAIAnalysis({
        prompt, mode, market,
        themes_count: themes.length, sl_count: sl.length,
        bottlenecks_count: bottlenecks.length, stocks_count: 0,
      })
      setResult(res)
    } catch (e) {
      alert(`Gemini API error: ${e}`)
    } finally {
      setLoading(false)
    }
  }

  const cachedYears = Object.keys(cache).sort().reverse()
  const cachedEntry = cacheYear ? cache[cacheYear] : null

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        AI Analysis for <strong>{countryLabel}</strong>
      </CountryBanner>

      <div className="bg-purple-950/30 border-l-4 border-purple-600 rounded-r-lg px-4 py-2 text-sm text-purple-200">
        🤖 <strong>AI Analysis</strong> — Comprehensive Gemini Flash analysis covering all detected themes,
        supply-chain bottlenecks, shortlisted multi-quarter themes, and ranked stocks. One click → full professional investment brief.
      </div>

      {/* Date range */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 items-end">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">From date</label>
          <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">To date</label>
          <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input" />
        </div>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-4 gap-3">
        {[
          ['Active Themes', themes.length, '#818cf8'],
          ['Bottleneck Themes', bottlenecks.length, '#f59e0b'],
          ['Shortlisted Themes', sl.length, '#22c55e'],
          ['Gemini Model', 'Flash', '#a855f7'],
        ].map(([label, val, color]) => (
          <div key={String(label)} className="kpi-card">
            <div className="text-xl font-black leading-none" style={{ color: String(color) }}>{String(val)}</div>
            <div className="text-xs text-slate-500 mt-1">{String(label)}</div>
          </div>
        ))}
      </div>

      {/* Cached analyses */}
      {cachedYears.length > 0 && (
        <div className="bg-emerald-950/30 border border-emerald-800/30 rounded-xl p-4">
          <div className="text-sm text-emerald-300 font-semibold mb-2">
            📂 Pre-generated analyses — {cachedYears.length} year(s) cached ({cachedYears.join(', ')})
          </div>
          <div className="flex items-center gap-3">
            <select value={cacheYear} onChange={e => setCacheYear(e.target.value)} className="select">
              <option value="">Select year…</option>
              {cachedYears.map(y => <option key={y}>{y}</option>)}
            </select>
            {cachedEntry && (
              <span className="text-xs text-slate-500">
                Window: {String(cachedEntry.from_date)} → {String(cachedEntry.to_date)} ·{' '}
                {String(cachedEntry.themes_count ?? 0)} themes · {String(cachedEntry.sl_count ?? 0)} shortlisted ·
                Generated: {String(cachedEntry.generated_at ?? '—')}
              </span>
            )}
          </div>
          {cachedEntry && (
            <div className="mt-3 bg-emerald-950/20 border border-emerald-800/20 rounded-xl p-4">
              <div className="text-xs text-emerald-400 font-bold uppercase tracking-wider mb-2">
                📂 Pre-generated — {country} {cacheYear} — Gemini Flash
              </div>
              <div className="text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">
                {String(cachedEntry.analysis ?? '')}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Mode selector */}
      <div>
        <label className="text-xs text-slate-400 mb-2 block font-semibold">Analysis mode</label>
        <div className="grid grid-cols-2 gap-2">
          {MODES.map(m => (
            <button key={m.key} onClick={() => setMode(m.key)}
              className={`text-left p-3 rounded-xl border transition-all ${
                mode === m.key
                  ? 'border-indigo-500 bg-indigo-950/40 text-indigo-200'
                  : 'border-slate-700 bg-slate-800/50 text-slate-400 hover:border-slate-500'
              }`}>
              <div className="font-semibold text-sm">{m.label}</div>
              <div className="text-xs opacity-70 mt-0.5">{m.desc}</div>
            </button>
          ))}
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex gap-3">
        <button onClick={runAnalysis} disabled={loading || (themes.length === 0 && sl.length === 0)}
          className="btn-primary flex items-center gap-2">
          {loading ? <Spinner size="sm" /> : '✨'} Run AI Analysis
        </button>
        {result && (
          <button onClick={() => setResult(null)} className="btn-secondary">🗑 Clear result</button>
        )}
      </div>

      {(themes.length === 0 && sl.length === 0) && (
        <div className="bg-amber-950/30 border border-amber-700/30 rounded-xl p-4 text-sm text-amber-300">
          ⚠️ No themes detected yet. Run the pipeline (🚀 Pipeline Runner tab) to populate themes first.
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="space-y-3">
          <div className="bg-purple-950/20 border border-purple-800/30 rounded-xl px-4 py-2.5 flex justify-between items-center flex-wrap gap-3">
            <div>
              <span className="font-bold text-slate-100">🤖 Gemini Flash — AI Investment Analysis</span>
              <span className="text-xs text-purple-400 ml-3">{result.mode}</span>
            </div>
            <div className="text-xs text-slate-500">
              {result.market} · {result.themes_count} themes · {result.sl_count} shortlisted ·
              {result.bottlenecks_count} bottlenecks · Generated {result.generated_at}
            </div>
          </div>
          <div className="bg-purple-950/10 border border-purple-900/30 rounded-xl p-6">
            <div className="text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">
              {result.result}
            </div>
          </div>
        </div>
      )}

      {/* Data preview */}
      {(themes.length > 0 || sl.length > 0 || bottlenecks.length > 0) && (
        <details>
          <summary className="text-sm text-slate-400 cursor-pointer hover:text-slate-200 transition-colors py-2 border-t border-slate-800">
            📊 Data preview — {themes.length} themes · {bottlenecks.length} bottlenecks · {sl.length} shortlisted
          </summary>
          <div className="grid grid-cols-3 gap-4 mt-3">
            <div>
              <div className="text-xs font-bold text-slate-300 mb-2">All Active Themes ({themes.length})</div>
              <div className="space-y-1">
                {themes.slice(0, 12).map((t, i) => (
                  <div key={i} className="text-xs text-slate-400">
                    — {String(t.theme_name ?? '')} <span className="text-indigo-400">`{String(t.conviction ?? 'emerging').toUpperCase()}`</span> · {Number(t.strength_score ?? 0).toFixed(0)}pts
                  </div>
                ))}
                {themes.length > 12 && <div className="text-xs text-slate-600">…and {themes.length - 12} more</div>}
              </div>
            </div>
            <div>
              <div className="text-xs font-bold text-slate-300 mb-2">Shortlisted (≥2Q, {sl.length})</div>
              <div className="space-y-1">
                {sl.slice(0, 12).map((t, i) => (
                  <div key={i} className="text-xs text-slate-400">
                    — {String(t.theme_name ?? '')} · {String(t.confirmed_quarters ?? 0)}Q · {String(t.company_count ?? 0)} cos
                  </div>
                ))}
                {sl.length > 12 && <div className="text-xs text-slate-600">…and {sl.length - 12} more</div>}
              </div>
            </div>
            <div>
              <div className="text-xs font-bold text-slate-300 mb-2">Bottleneck Themes ({bottlenecks.length})</div>
              <div className="space-y-1">
                {bottlenecks.slice(0, 12).map((t, i) => (
                  <div key={i} className="text-xs text-red-400">
                    🔴 {String(t.theme_name ?? '')} · {Number(t.strength_score ?? 0).toFixed(0)}pts
                  </div>
                ))}
                {bottlenecks.length === 0 && <div className="text-xs text-slate-600">No bottleneck themes detected yet.</div>}
              </div>
            </div>
          </div>
        </details>
      )}
    </div>
  )
}
