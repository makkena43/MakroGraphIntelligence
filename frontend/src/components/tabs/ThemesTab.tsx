import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  fetchThemes, fetchBeneficiaries, fetchSnapshots, fetchQuarterly,
  fetchSourceCompanies, fetchEvidence, fetchMacroContext,
  fetchCausalChains, fetchContradictions,
  fetchPendingCanonical, approveCanonical, dismissCanonical, canonicalAIResolve,
  fetchIndiaChainBeneficiaries,
} from '../../api'
import { CountryBanner, ConvictionBadge, SectionHeader, EmptyState, Spinner, DIR_COLOR } from '../ui'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

const ROLE_ICON: Record<string, string> = {
  infrastructure_provider: '🏗️', supplier: '🔧', bottleneck_player: '⚡',
  beneficiary: '💚', downstream_user: '📥', hidden_enabler: '🔦',
}
const LINK_ICON: Record<string, string> = {
  corroborates: '✅', amplifies: '🚀', constrains: '⚠️', reduces: '🔻',
}
const LINK_COLOR: Record<string, string> = {
  corroborates: '#22c55e', amplifies: '#16a34a', constrains: '#ef4444', reduces: '#f97316',
}

export default function ThemesTab({ country, countryFlag, countryLabel }: Props) {
  const [useLive, setUseLive] = useState(true)
  const [fromDate, setFromDate] = useState('2020-01-01')
  const [toDate, setToDate] = useState(today)
  const [minStrength, setMinStrength] = useState(0)
  const [convFilter, setConvFilter] = useState('All')
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<'beneficiaries' | 'source' | 'evidence' | 'macro'>('beneficiaries')
  const [chainFilter, setChainFilter] = useState('')
  const [selectedChain, setSelectedChain] = useState<string | null>(null)
  const [canonicalAiResult, setCanonicalAiResult] = useState<string | null>(null)
  const [canonicalAiLoading, setCanonicalAiLoading] = useState(false)
  const [pendingApprovals, setPendingApprovals] = useState<Record<string, string>>({})

  const { data: themes = [], isLoading } = useQuery({
    queryKey: ['themes', country, useLive, fromDate, toDate, minStrength],
    queryFn: () => useLive
      ? fetchThemes(country, { min_strength: minStrength })
      : fetchThemes(country, { as_of: toDate, from_date: fromDate, min_strength: minStrength }),
  })

  const { data: causalChains = [] } = useQuery({
    queryKey: ['causal-chains', country, useLive ? null : toDate, useLive ? null : fromDate],
    queryFn: () => fetchCausalChains(country, useLive ? undefined : { as_of: toDate, from_date: fromDate }),
  })

  const { data: indiaChainBens = [] } = useQuery({
    queryKey: ['india-chain-bens', useLive ? null : toDate],
    queryFn: () => fetchIndiaChainBeneficiaries(useLive ? undefined : toDate),
    enabled: country === 'IN',
  })

  const { data: contradictions = [] } = useQuery({
    queryKey: ['contradictions', country],
    queryFn: () => fetchContradictions(country),
  })

  const { data: pendingCanonical = [] } = useQuery({
    queryKey: ['canonical-pending'],
    queryFn: fetchPendingCanonical,
  })

  const { data: bens = [], isLoading: bensLoading } = useQuery({
    queryKey: ['beneficiaries', selectedId, useLive ? null : toDate],
    queryFn: () => fetchBeneficiaries(selectedId!, useLive ? undefined : toDate),
    enabled: selectedId !== null && detailTab === 'beneficiaries',
  })

  const { data: snapshots = [] } = useQuery({
    queryKey: ['snapshots', selectedId, useLive ? null : fromDate, useLive ? null : toDate],
    queryFn: () => fetchSnapshots(selectedId!, useLive ? undefined : fromDate, useLive ? undefined : toDate),
    enabled: selectedId !== null,
  })

  const { data: quarterly = [] } = useQuery({
    queryKey: ['quarterly', selectedId, toDate],
    queryFn: () => fetchQuarterly(selectedId!, toDate),
    enabled: selectedId !== null,
  })

  const { data: srcCos = [], isLoading: srcLoading } = useQuery({
    queryKey: ['source-companies', selectedSlug, toDate, fromDate],
    queryFn: () => fetchSourceCompanies(selectedSlug!, toDate, fromDate),
    enabled: selectedSlug !== null && detailTab === 'source',
  })

  const { data: evidence = [], isLoading: evLoading } = useQuery({
    queryKey: ['evidence', selectedSlug, toDate, fromDate],
    queryFn: () => fetchEvidence(selectedSlug!, toDate, fromDate),
    enabled: selectedSlug !== null && detailTab === 'evidence',
  })

  const { data: macroCtx = [], isLoading: macroLoading } = useQuery({
    queryKey: ['macro-context', selectedSlug, toDate],
    queryFn: () => fetchMacroContext(selectedSlug!, toDate),
    enabled: selectedSlug !== null && detailTab === 'macro',
  })

  const filtered = (themes as Record<string, unknown>[]).filter(t => {
    if (convFilter !== 'All' && String(t.conviction ?? '').toLowerCase() !== convFilter.toLowerCase()) return false
    return true
  })

  const selectedTheme = (themes as Record<string, unknown>[]).find(t => t.theme_slug === selectedSlug)

  const filteredChains = (causalChains as Record<string, unknown>[]).filter(c => {
    if (!chainFilter.trim()) return true
    const kw = chainFilter.toLowerCase()
    return String(c.chain_name ?? '').toLowerCase().includes(kw)
      || String(c.terminal_effect ?? '').toLowerCase().includes(kw)
  })

  const handleApproveCanonical = async () => {
    await approveCanonical(pendingApprovals)
    setPendingApprovals({})
  }

  const handleAIResolveCanonical = async () => {
    if ((pendingCanonical as unknown[]).length === 0) return
    setCanonicalAiLoading(true)
    try {
      const clusters = (pendingCanonical as Record<string, unknown>[]).slice(0, 20).map((c: Record<string, unknown>) =>
        `Cluster ${c.cluster_id}: [${(c.variant_names as string[] || []).join(' | ')}]`
      ).join('\n')
      const prompt = `Review these theme name clusters and suggest a clean canonical name for each:\n\n${clusters}\n\nReturn as JSON: {"cluster_id": "canonical_name"}`
      const res = await canonicalAIResolve(prompt)
      setCanonicalAiResult(res.result)
    } catch (e) {
      setCanonicalAiResult(`Error: ${e}`)
    } finally {
      setCanonicalAiLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <CountryBanner flag={countryFlag} label={countryLabel} />

      {/* Canonical Review Panel */}
      {(pendingCanonical as unknown[]).length > 0 && (
        <div className="bg-amber-950/30 border border-amber-800/40 rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <span className="font-semibold text-amber-300 text-sm">
              🔤 Canonical Name Review — {(pendingCanonical as unknown[]).length} pending clusters
            </span>
            <div className="flex gap-2">
              <button onClick={handleAIResolveCanonical} disabled={canonicalAiLoading}
                className="btn-secondary text-xs flex items-center gap-1">
                {canonicalAiLoading ? <Spinner size="sm" /> : '🤖'} AI Suggest
              </button>
              {Object.keys(pendingApprovals).length > 0 && (
                <button onClick={handleApproveCanonical} className="btn-primary text-xs">
                  ✅ Approve {Object.keys(pendingApprovals).length}
                </button>
              )}
            </div>
          </div>
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {(pendingCanonical as Record<string, unknown>[]).slice(0, 10).map((c: Record<string, unknown>) => (
              <div key={String(c.cluster_id)} className="flex items-center gap-3 bg-slate-900 rounded-lg p-2">
                <div className="flex-1">
                  <div className="text-xs text-slate-400 mb-1">
                    Variants: {(c.variant_names as string[] || []).slice(0, 5).join(' | ')}
                  </div>
                  <input
                    value={pendingApprovals[String(c.cluster_id)] ?? String(c.suggested_canonical ?? '')}
                    onChange={e => setPendingApprovals(p => ({ ...p, [String(c.cluster_id)]: e.target.value }))}
                    className="input text-xs"
                    placeholder="Canonical name..."
                  />
                </div>
                <button onClick={() => dismissCanonical(String(c.cluster_id))}
                  className="text-xs text-slate-500 hover:text-red-400 transition-colors">✕</button>
              </div>
            ))}
          </div>
          {canonicalAiResult && (
            <pre className="mt-3 text-xs text-emerald-300 bg-slate-950 rounded-lg p-3 overflow-auto max-h-40">
              {canonicalAiResult}
            </pre>
          )}
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-end">
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={useLive} onChange={e => setUseLive(e.target.checked)} className="accent-indigo-500" />
          <span className="text-sm text-slate-300">Live (latest)</span>
        </label>
        {!useLive && (
          <>
            <div>
              <label className="text-xs text-slate-400 mb-1 block">From</label>
              <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input w-36" />
            </div>
            <div>
              <label className="text-xs text-slate-400 mb-1 block">To (as-of)</label>
              <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input w-36" />
            </div>
          </>
        )}
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min strength: {minStrength}</label>
          <input type="range" min={0} max={200} step={5} value={minStrength}
            onChange={e => setMinStrength(+e.target.value)} className="w-32 accent-indigo-500" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Conviction</label>
          <select value={convFilter} onChange={e => setConvFilter(e.target.value)} className="select">
            {['All', 'confirmed', 'developing', 'emerging'].map(c => <option key={c}>{c}</option>)}
          </select>
        </div>
      </div>

      {isLoading && <div className="flex justify-center py-12"><Spinner /></div>}

      {!isLoading && (
        <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
          {/* Theme list */}
          <div className="xl:col-span-2 space-y-2">
            <p className="text-xs text-slate-500">
              <strong className="text-slate-300">{filtered.length}</strong> themes
            </p>
            <div className="space-y-1 max-h-[70vh] overflow-y-auto pr-1">
              {filtered.map((t: Record<string, unknown>, i) => {
                const isSelected = t.theme_slug === selectedSlug
                const conv = String(t.conviction ?? 'emerging')
                const convColors: Record<string, string> = {
                  confirmed: 'border-emerald-600/50 bg-emerald-950/20',
                  developing: 'border-yellow-600/50 bg-yellow-950/20',
                  emerging: 'border-indigo-600/50 bg-indigo-950/20',
                }
                return (
                  <div
                    key={i}
                    onClick={() => {
                      setSelectedSlug(String(t.theme_slug ?? ''))
                      setSelectedId(Number(t.id ?? 0))
                      setDetailTab('beneficiaries')
                    }}
                    className={`rounded-xl p-3 cursor-pointer border transition-all ${
                      isSelected
                        ? 'border-indigo-500 bg-indigo-950/40'
                        : `${convColors[conv] ?? 'border-slate-700 bg-slate-800/50'} hover:border-slate-500`
                    }`}
                  >
                    <div className="flex justify-between items-start gap-2">
                      <span className="font-semibold text-sm text-white leading-snug">
                        {String(t.theme_name ?? '')}
                      </span>
                      <ConvictionBadge conviction={conv} />
                    </div>
                    <div className="flex gap-3 mt-1.5 text-xs text-slate-400">
                      <span>💪 {Number(t.strength_score ?? 0).toFixed(0)}</span>
                      <span>🏢 {String(t.company_count ?? 0)}</span>
                      {Number(t.confirmed_quarters ?? 0) > 0 && (
                        <span className="text-indigo-300">{String(t.confirmed_quarters)}Q</span>
                      )}
                    </div>
                  </div>
                )
              })}
              {filtered.length === 0 && (
                <EmptyState>No themes found. Run the pipeline to detect themes.</EmptyState>
              )}
            </div>
          </div>

          {/* Theme detail */}
          <div className="xl:col-span-3 space-y-4">
            {selectedTheme ? (
              <>
                <ThemeDetail
                  theme={selectedTheme}
                  snapshots={snapshots as Record<string, unknown>[]}
                  quarterly={quarterly as Record<string, unknown>[]}
                  useLive={useLive}
                  toDate={toDate}
                />
                <div className="bg-slate-800 border border-slate-700 rounded-xl overflow-hidden">
                  <div className="flex border-b border-slate-700">
                    {(['beneficiaries', 'source', 'evidence', 'macro'] as const).map(tab => (
                      <button key={tab} onClick={() => setDetailTab(tab)}
                        className={`flex-1 py-2 text-xs font-medium transition-colors ${
                          detailTab === tab ? 'text-indigo-400 border-b-2 border-indigo-500 bg-slate-900/40' : 'text-slate-500 hover:text-slate-300'
                        }`}>
                        {tab === 'beneficiaries' ? '🏢 Beneficiaries'
                          : tab === 'source' ? '📄 Source Cos'
                          : tab === 'evidence' ? '💬 Evidence'
                          : '🌐 Macro'}
                      </button>
                    ))}
                  </div>
                  <div className="p-4 max-h-80 overflow-y-auto">
                    {detailTab === 'beneficiaries' && (
                      bensLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      <BeneficiariesPanel bens={bens as Record<string, unknown>[]} />
                    )}
                    {detailTab === 'source' && (
                      srcLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      <SourceCompaniesPanel rows={srcCos as Record<string, unknown>[]} />
                    )}
                    {detailTab === 'evidence' && (
                      evLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      <EvidencePanel items={evidence as Record<string, unknown>[]} />
                    )}
                    {detailTab === 'macro' && (
                      macroLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      <MacroContextPanel items={macroCtx as Record<string, unknown>[]} />
                    )}
                  </div>
                </div>
              </>
            ) : (
              <EmptyState>← Select a theme to see details, beneficiaries, evidence & macro context</EmptyState>
            )}
          </div>
        </div>
      )}

      {/* Causal Chains */}
      <div className="border-t border-slate-800 pt-4">
        <div className="flex items-center gap-4 mb-3 flex-wrap">
          <SectionHeader>⛓️ Active Causal Chains</SectionHeader>
          <input value={chainFilter} onChange={e => setChainFilter(e.target.value)}
            placeholder="Filter by keyword…" className="input w-56 text-xs" />
          {!useLive && (
            <span className="text-xs text-indigo-400 bg-indigo-900/30 border border-indigo-700/40 rounded px-2 py-0.5">
              Scored as-of {toDate}
            </span>
          )}
        </div>
        {filteredChains.length === 0 ? (
          <p className="text-sm text-slate-500">No causal chains yet. Run causal stage.</p>
        ) : (
          <div className="space-y-2">
            {filteredChains.map((c: Record<string, unknown>, i) => {
              const score    = Number(c.activation_score ?? 0)
              const hits     = Number((c as Record<string, unknown>).signal_hits_in_window ?? 0)
              const cos      = Number((c as Record<string, unknown>).companies_in_window ?? 0)
              const sc       = score >= 70 ? '#22c55e' : score >= 40 ? '#f59e0b' : score >= 20 ? '#6366f1' : '#475569'
              const chainKey = String(c.chain_name ?? i)
              const expanded = selectedChain === chainKey
              // "First appeared" badge: chain's first_detected year matches the selected toDate year
              const firstDetected = c.first_detected ? String(c.first_detected) : null
              const firstYear = firstDetected ? firstDetected.slice(0, 4) : null
              const selectedYear = toDate ? toDate.slice(0, 4) : null
              const isNewThisYear = !useLive && firstYear && selectedYear && firstYear === selectedYear
              // India beneficiaries for this chain
              const chainBens = country === 'IN'
                ? (indiaChainBens as Record<string, unknown>[]).filter(b => {
                    const theme = String(b.theme_name ?? '').toLowerCase()
                    const terminal = String(c.terminal_effect ?? '').toLowerCase()
                    const chainName = chainKey.toLowerCase()
                    return terminal.includes(theme.split(' ')[0]) ||
                           chainName.includes(theme.split(' ')[0]) ||
                           theme.includes(chainName.split('→')[0]?.trim().split(' ')[0] ?? '')
                  }).slice(0, 8)
                : []

              return (
                <div key={i} className="rounded-xl border border-slate-700 overflow-hidden">
                  {/* Chain header row — clickable to expand */}
                  <button
                    className="w-full bg-slate-800 hover:bg-slate-750 px-4 py-3 flex items-center gap-4 text-left transition-colors"
                    onClick={() => setSelectedChain(expanded ? null : chainKey)}
                  >
                    {/* Score badge */}
                    <div className="text-center min-w-[52px] rounded-lg px-2 py-1 shrink-0" style={{ background: `${sc}22` }}>
                      <div className="text-xl font-black leading-none" style={{ color: sc }}>{score.toFixed(0)}</div>
                      <div className="text-[10px] text-slate-500 uppercase tracking-wide">SCORE</div>
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="font-bold text-white text-sm truncate flex items-center gap-2">
                        {chainKey}
                        {isNewThisYear && (
                          <span className="shrink-0 text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 uppercase tracking-wide">
                            First appeared {firstYear}
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                        <span>Depth: {String(c.depth ?? '?')} hops</span>
                        <span>→ Terminal: <span className="text-sky-300">{String(c.terminal_effect ?? '')}</span></span>
                        {hits > 0 && <span className="text-emerald-400">📶 {hits} signals</span>}
                        {cos > 0 && <span className="text-sky-400">🏢 {cos} companies</span>}
                        {firstDetected && !isNewThisYear && <span className="text-slate-500">Since {firstYear}</span>}
                      </div>
                    </div>

                    {/* Expand chevron + beneficiary count badge */}
                    <div className="flex items-center gap-2 shrink-0">
                      {country === 'IN' && chainBens.length > 0 && (
                        <span className="text-xs bg-indigo-900/50 text-indigo-300 border border-indigo-700/40 rounded px-1.5 py-0.5">
                          {chainBens.length} plays
                        </span>
                      )}
                      <span className="text-slate-500 text-xs">{expanded ? '▲' : '▼'}</span>
                    </div>
                  </button>

                  {/* Expanded beneficiary panel */}
                  {expanded && country === 'IN' && (
                    <div className="bg-slate-900 border-t border-slate-700 px-4 py-3">
                      {chainBens.length === 0 ? (
                        <p className="text-xs text-slate-500">No India beneficiaries mapped for this chain yet.</p>
                      ) : (
                        <>
                          <div className="text-xs font-semibold text-indigo-300 mb-2">
                            🇮🇳 India Supply Chain Plays
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                            {chainBens.map((b: Record<string, unknown>, bi) => {
                              const btype = String(b.beneficiary_type ?? '')
                              const typeColor = btype === 'direct' ? 'emerald'
                                : btype === 'localization_play' ? 'amber' : 'sky'
                              const typeLabel = btype === 'direct' ? '🔧 Direct'
                                : btype === 'localization_play' ? '🏭 Localisation'
                                : '🔗 Indirect'
                              const conv = Number(b.conviction_score ?? 0)
                              return (
                                <div key={bi}
                                  className={`rounded-lg border px-3 py-2 bg-${typeColor}-950/20 border-${typeColor}-800/30`}>
                                  <div className="flex items-center justify-between gap-2">
                                    <div>
                                      <span className="text-sm font-semibold text-white">{String(b.company ?? '')}</span>
                                      {!!b.ticker && (
                                        <span className="ml-1.5 text-xs text-slate-400">{String(b.ticker)}</span>
                                      )}
                                    </div>
                                    <div className="text-right shrink-0">
                                      <div className="text-xs font-bold text-emerald-400">{(conv * 100).toFixed(0)}%</div>
                                      <div className="text-[10px] text-slate-500">conviction</div>
                                    </div>
                                  </div>
                                  <div className="flex items-center gap-2 mt-1 flex-wrap">
                                    <span className={`text-[10px] font-medium text-${typeColor}-400`}>{typeLabel}</span>
                                    {!!b.has_order_book_signals && (
                                      <span className="text-[10px] text-amber-400">📋 Order book signals</span>
                                    )}
                                    {!!b.import_substitution_play && (
                                      <span className="text-[10px] text-sky-400">🔄 Import sub</span>
                                    )}
                                    <span className="text-[10px] text-slate-500">{String(b.constrained_product ?? '')}</span>
                                  </div>
                                </div>
                              )
                            })}
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Contradiction Radar */}
      <div className="border-t border-slate-800 pt-4">
        <SectionHeader>🔄 Contradiction Radar</SectionHeader>
        <p className="text-xs text-slate-500 mb-3">
          Detects when management narratives reverse between quarters.
        </p>
        {(contradictions as unknown[]).length === 0 ? (
          <p className="text-sm text-slate-600">No contradictions recorded yet. Run the intelligence pipeline.</p>
        ) : (
          <div className="space-y-2">
            {(contradictions as Record<string, unknown>[]).slice(0, 10).map((c, i) => {
              const ct = String(c.change_type ?? 'general_reversal')
              const colorMap: Record<string, string> = {
                demand_reversal: '#ef4444', margin_reversal: '#f59e0b',
                capex_reversal: '#6366f1', positive_to_negative: '#ef4444',
                negative_to_positive: '#22c55e', general_reversal: '#94a3b8',
              }
              const col = colorMap[ct] ?? '#94a3b8'
              const ev = (c.evidence as Record<string, string[]>) ?? {}
              const from_phrase = (ev.from_phrases ?? ['—'])[0]
              const to_phrase = (ev.to_phrases ?? ['—'])[0]
              return (
                <div key={i} className="bg-slate-800 border border-slate-700 rounded-xl p-3"
                  style={{ borderLeft: `3px solid ${col}` }}>
                  <div className="flex justify-between flex-wrap gap-2">
                    <span className="font-bold text-white text-sm">
                      {String(c.company ?? '?')} — {String(c.theme ?? '?')}
                    </span>
                    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: `${col}22`, color: col }}>
                      {ct.replace(/_/g, ' ')}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    {String(c.from_quarter ?? '?')} → {String(c.to_quarter ?? '?')}
                  </div>
                  <div className="text-xs mt-1.5">
                    <span className="text-slate-500">Before:</span>{' '}
                    <span className="text-sky-300">"{from_phrase}"</span>{' '}
                    →{' '}
                    <span className="text-slate-500">After:</span>{' '}
                    <span className="text-red-300">"{to_phrase}"</span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Theme Detail Card ────────────────────────────────────────────────────────
function ThemeDetail({
  theme, snapshots, quarterly, useLive, toDate,
}: {
  theme: Record<string, unknown>
  snapshots: Record<string, unknown>[]
  quarterly: Record<string, unknown>[]
  useLive: boolean
  toDate: string
}) {
  const strength = Number(theme.strength_score ?? 0)
  const momentum = Number(theme.momentum_score ?? 0)
  const docs = Number(theme.doc_count ?? 0)
  const companies = Number(theme.company_count ?? 0)
  const conv = String(theme.conviction ?? 'emerging')
  const stage = Number(theme.stage ?? 0)

  const chartData = snapshots.map(s => ({
    date: String(s.snapshot_date ?? '').slice(0, 10),
    strength: Number(s.strength_score ?? 0),
    momentum: Number(s.momentum_score ?? 0),
  }))

  const confirmedQ = quarterly.filter(q => q.confirmed).length

  return (
    <div className="bg-slate-800 border border-slate-700 rounded-xl p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-bold text-white text-base leading-snug">
            {String(theme.theme_name ?? '')}
          </div>
          <div className="text-xs text-slate-500 mt-0.5">{String(theme.theme_slug ?? '')}</div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <ConvictionBadge conviction={conv} />
          <span className="text-xs text-slate-500">Stage {stage}</span>
        </div>
      </div>

      {/* Metrics grid */}
      <div className="grid grid-cols-4 gap-2">
        {[
          ['Strength', strength.toFixed(1), '#818cf8'],
          ['Momentum', momentum.toFixed(1), '#f59e0b'],
          ['Docs', String(docs), '#e2e8f0'],
          ['Companies', String(companies), '#e2e8f0'],
        ].map(([label, val, color]) => (
          <div key={String(label)} className="bg-slate-900 rounded-lg p-2 text-center">
            <div className="text-sm font-bold leading-none" style={{ color: String(color) }}>{String(val)}</div>
            <div className="text-xs text-slate-500 mt-1">{String(label)}</div>
          </div>
        ))}
      </div>

      {/* Quarterly persistence */}
      {quarterly.length > 0 && (
        <div>
          <div className="text-xs text-slate-500 mb-1.5">
            📅 Quarterly Persistence
            {confirmedQ >= 3 && (
              <span className="ml-2 text-emerald-400">✅ {confirmedQ} quarters confirmed</span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {quarterly.map((q, i) => (
              <span key={i} className={`text-xs px-2 py-0.5 rounded border font-bold ${
                q.confirmed ? 'bg-emerald-950/40 border-emerald-600/40 text-emerald-300' : 'bg-slate-800 border-slate-700 text-slate-500'
              }`}>
                {q.confirmed ? '✓' : '·'} Q{String(q.quarter)}-{String(q.year)}
                <span className="opacity-60 ml-1 text-xs">{Number(q.max_strength ?? 0).toFixed(0)}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Sparkline */}
      {chartData.length >= 2 && (
        <ResponsiveContainer width="100%" height={130}>
          <LineChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
            <YAxis tick={{ fill: '#64748b', fontSize: 10 }} />
            <Tooltip
              contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
              labelStyle={{ color: '#94a3b8' }}
            />
            <Line type="monotone" dataKey="strength" stroke="#818cf8" strokeWidth={2} dot={false} name="Strength" />
            <Line type="monotone" dataKey="momentum" stroke="#f59e0b" strokeWidth={1.5} dot={false} strokeDasharray="4 4" name="Momentum" />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

// ─── Beneficiaries panel ──────────────────────────────────────────────────────
function BeneficiariesPanel({ bens }: { bens: Record<string, unknown>[] }) {
  if (bens.length === 0) return (
    <p className="text-xs text-slate-600">No beneficiaries mapped yet. Run the intelligence pipeline.</p>
  )
  return (
    <div className="space-y-1.5">
      {bens.slice(0, 20).map((b, i) => {
        const role = String(b.company_role ?? b.beneficiary_type ?? 'beneficiary')
        const icon = ROLE_ICON[role] ?? '⚪'
        return (
          <div key={i} className="flex items-center gap-3 bg-slate-900 rounded-lg px-3 py-2">
            <span className="text-base">{icon}</span>
            <div className="flex-1 min-w-0">
              <div className="text-xs font-bold text-white">
                {String(b.ticker ?? '—')} <span className="font-normal text-slate-400">{String(b.company_name ?? '').slice(0, 30)}</span>
              </div>
              <div className="text-xs text-slate-500">{role.replace(/_/g, ' ')}</div>
            </div>
            <div className="text-right text-xs">
              <div className="text-indigo-400 font-bold">{String(b.relevance_score ?? 0)}</div>
              <div className="text-slate-600">{String(b.signal_count ?? 0)} sigs</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ─── Source Companies panel ───────────────────────────────────────────────────
function SourceCompaniesPanel({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0) return (
    <p className="text-xs text-slate-600">No source companies in this window.</p>
  )
  return (
    <div className="space-y-1.5">
      {rows.slice(0, 20).map((r, i) => (
        <div key={i} className="flex items-center gap-3 bg-slate-900 rounded-lg px-3 py-2">
          <div className="flex-1 min-w-0">
            <div className="text-xs font-bold text-white">
              {String(r.ticker ?? '—')} <span className="font-normal text-slate-400">{String(r.company ?? '').slice(0, 30)}</span>
            </div>
            <div className="text-xs text-slate-500">
              {String(r.doc_count ?? 0)} filings · {String(r.filing_types ?? '').slice(0, 30)}
            </div>
          </div>
          <div className="text-right text-xs">
            <div className="text-indigo-400 font-bold">{String(r.signal_count ?? 0)} sigs</div>
            <div className="text-slate-600">conf: {Number(r.avg_confidence ?? 0).toFixed(2)}</div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ─── Evidence panel ───────────────────────────────────────────────────────────
function EvidencePanel({ items }: { items: Record<string, unknown>[] }) {
  if (items.length === 0) return (
    <p className="text-xs text-slate-600">No evidence snippets found in this window.</p>
  )
  return (
    <div className="space-y-2">
      {items.slice(0, 15).map((ev, i) => {
        const dc = DIR_COLOR[String(ev.direction ?? 'neutral')] ?? '#94a3b8'
        return (
          <div key={i} className="bg-slate-900 rounded-lg p-2.5 text-xs"
            style={{ borderLeft: `3px solid ${dc}` }}>
            <div className="flex justify-between gap-2 flex-wrap">
              <span className="font-bold text-slate-200">
                {String(ev.company ?? '?')}
                {ev.ticker ? ` (${ev.ticker})` : ''}
              </span>
              <span className="text-slate-500">
                {String(ev.filing_type ?? '?')} · {String(ev.filed_at ?? '').slice(0, 10)} · conf:{' '}
                <span className="text-indigo-300">{Number(ev.confidence ?? 0).toFixed(2)}</span>
              </span>
            </div>
            <div className="text-slate-400 italic mt-1 line-clamp-3">
              "{String(ev.context_text ?? '').slice(0, 350)}"
            </div>
            <div className="text-slate-600 mt-1">
              {String(ev.signal_type ?? '?')} · <span style={{ color: dc }}>{String(ev.direction ?? '')}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ─── Macro Context panel ──────────────────────────────────────────────────────
function MacroContextPanel({ items }: { items: Record<string, unknown>[] }) {
  if (items.length === 0) return (
    <p className="text-xs text-slate-600">No macro context. Fetch macro data in 🌐 Macro & Policy tab.</p>
  )
  const tailwind = items.filter(m => ['corroborates', 'amplifies'].includes(String(m.link_type ?? '')))
    .reduce((s, m) => s + Number(m.strength ?? 0), 0)
  const headwind = items.filter(m => ['constrains', 'reduces'].includes(String(m.link_type ?? '')))
    .reduce((s, m) => s + Number(m.strength ?? 0), 0)
  const net = tailwind - headwind
  return (
    <div className="space-y-2">
      <div className="flex gap-4 bg-slate-900 rounded-lg p-3 text-center mb-3">
        <div><div className="text-emerald-400 font-black text-lg">+{tailwind.toFixed(0)}</div><div className="text-xs text-slate-500">tailwinds</div></div>
        <div><div className="text-red-400 font-black text-lg">-{headwind.toFixed(0)}</div><div className="text-xs text-slate-500">headwinds</div></div>
        <div><div className={`font-black text-lg ${net > 0 ? 'text-emerald-400' : 'text-red-400'}`}>{net > 0 ? '+' : ''}{net.toFixed(0)}</div><div className="text-xs text-slate-500">net macro</div></div>
      </div>
      {items.map((ml, i) => {
        const lt = String(ml.link_type ?? 'corroborates')
        const lc = LINK_COLOR[lt] ?? '#94a3b8'
        const li = LINK_ICON[lt] ?? '·'
        return (
          <div key={i} className="bg-slate-900 rounded-lg p-2 text-xs" style={{ borderLeft: `3px solid ${lc}` }}>
            <div className="flex justify-between">
              <span className="font-bold" style={{ color: lc }}>{li} {lt.toUpperCase()}</span>
              <span className="text-slate-500">strength: {Number(ml.strength ?? 0).toFixed(0)}</span>
            </div>
            <div className="text-slate-300 mt-1">
              {String(ml.macro_description ?? ml.policy_title ?? ml.evidence_text ?? '').slice(0, 250)}
            </div>
          </div>
        )
      })}
    </div>
  )
}
