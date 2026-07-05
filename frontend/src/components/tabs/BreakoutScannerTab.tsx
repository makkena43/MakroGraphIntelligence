import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchBreakoutScan } from '../../api'

const YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]

const SETUP_META: Record<string, { label: string; sub: string; color: string; bg: string }> = {
  breakout: { label: '🚀 FRESH BREAKOUTS', sub: 'Cleared the base on expanded volume in the last ~7 sessions — the move has started', color: 'text-green-300', bg: 'bg-green-950/30 border-green-700/50' },
  coiling:  { label: '🌀 COILING — ABOUT TO BREAK OUT', sub: 'Near 52w high, tight range, volume drying up / delivery rising — the spring is loading', color: 'text-amber-300', bg: 'bg-amber-950/30 border-amber-700/50' },
  basing:   { label: '🏗 BASING', sub: 'Constructive structure, not yet tight — watch for contraction', color: 'text-sky-300', bg: 'bg-sky-950/30 border-sky-700/40' },
  no_setup: { label: '💤 NO PRICE SETUP', sub: 'Fundamentals qualified, chart not ready — patience', color: 'text-slate-400', bg: 'bg-slate-900/40 border-slate-700/40' },
  extended: { label: '⚠ EXTENDED', sub: 'Large 1-year run already — the easy part may be over', color: 'text-orange-300', bg: 'bg-orange-950/30 border-orange-700/40' },
}

function Sparkline({ close, vol }: { close: number[]; vol: number[] }) {
  if (!close?.length) return null
  const w = 180, h = 44, vh = 12
  const mn = Math.min(...close), mx = Math.max(...close)
  const pts = close.map((c, i) => `${(i / (close.length - 1)) * w},${h - ((c - mn) / (mx - mn || 1)) * (h - 4) - 2}`).join(' ')
  const vmax = Math.max(...(vol ?? [1]))
  const up = close[close.length - 1] >= close[0]
  return (
    <svg width={w} height={h + vh + 2} className="flex-shrink-0">
      <polyline points={pts} fill="none" stroke={up ? '#4ade80' : '#f87171'} strokeWidth="1.5" />
      {(vol ?? []).map((v, i) => (
        <rect key={i} x={(i / (vol.length - 1)) * w - 1} y={h + 2 + (1 - v / (vmax || 1)) * vh}
              width={2} height={(v / (vmax || 1)) * vh} fill="#475569" />
      ))}
    </svg>
  )
}

function StockRow({ s }: { s: Record<string, any> }) {
  const [open, setOpen] = useState(false)
  const p = s.price ?? {}
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 hover:border-slate-700 transition-colors">
      <button onClick={() => setOpen(o => !o)} className="w-full text-left px-3 py-2">
        <div className="flex items-center gap-3">
          <div className="w-16 flex-shrink-0 text-center">
            <div className="text-lg font-black text-amber-400 leading-none">{s.jump_score ?? '—'}</div>
            <div className="text-[9px] text-slate-600">jump score</div>
          </div>
          <Sparkline close={p.spark_close ?? []} vol={p.spark_vol ?? []} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-black text-slate-100">{s.ticker}</span>
              <span className="text-xs text-slate-400 truncate">{String(s.company ?? '').slice(0, 28)}</span>
              {s.list_tier === 'conviction' && <span className="text-[9px] px-1.5 py-0.5 rounded bg-red-950/60 border border-red-700/50 text-red-300 font-bold">🎯 CONVICTION</span>}
              {Boolean(s.explosion) && <span className="text-[10px]">💥</span>}
              {Boolean(p.split_suspect) && <span className="text-[9px] text-slate-500" title="split/bonus detected in window — metrics use post-split data only">✂ split-adj</span>}
            </div>
            <div className="flex gap-3 text-[10px] text-slate-500 mt-1 flex-wrap">
              <span>₹{p.last_close}</span>
              <span>{p.dist_from_52w_high_pct}% from 52w-high</span>
              <span>20d range {p.range_20d_pct}%</span>
              {p.runup_1y_pct != null && <span className={p.runup_1y_pct > 100 ? 'text-orange-400' : ''}>1y {p.runup_1y_pct > 0 ? '+' : ''}{p.runup_1y_pct}%</span>}
              {p.breakout_day && <span className="text-green-400 font-bold">broke out {p.breakout_day}</span>}
            </div>
          </div>
          <div className="text-right flex-shrink-0">
            <div className="text-xs font-bold text-slate-300">S{s.stage} · {String(s.trajectory ?? '')}</div>
            <div className="text-[10px] text-slate-500">fund {s.fundamental_score} · ready {p.readiness}</div>
          </div>
        </div>
      </button>
      {open && (
        <div className="px-4 pb-3 pt-1 border-t border-slate-800/50 grid grid-cols-2 md:grid-cols-4 gap-2 text-[10px]">
          <div>Volume dry-up: <strong className={p.volume_dryup ? 'text-amber-300' : 'text-slate-400'}>{p.volume_dryup ? 'YES' : 'no'}</strong></div>
          <div>Range contraction: <strong className={p.range_contraction ? 'text-amber-300' : 'text-slate-400'}>{p.range_contraction ? 'YES' : 'no'}</strong></div>
          <div>Accumulation (up/down vol): <strong className={Number(p.accumulation_ratio) >= 1.3 ? 'text-green-400' : 'text-slate-400'}>{p.accumulation_ratio}</strong></div>
          <div>Delivery 20d: <strong className={p.delivery_rising ? 'text-green-400' : 'text-slate-400'}>{p.delivery_pct_20d}%{p.delivery_rising ? ' ↑' : ''}</strong></div>
          <div className="col-span-2 md:col-span-4 text-slate-500">
            📌 {String(s.theme ?? '') || 'no theme'} · shortlisted {String(s.shortlisted_date ?? '?')} · {s.peer_corroboration} peer confirms
          </div>
        </div>
      )}
    </div>
  )
}

export default function BreakoutScannerTab({ country }: { country: 'US' | 'IN'; countryFlag?: string; countryLabel?: string }) {
  const [year, setYear] = useState<number | undefined>(2024)
  const [asOf, setAsOf] = useState<string>('')   // '' = latest price date

  const { data, isLoading, isError } = useQuery({
    queryKey: ['breakout-scan', year, asOf],
    queryFn: () => fetchBreakoutScan('IN', year, asOf || undefined),
    staleTime: 10 * 60_000,
    enabled: true,
  })

  const stocks = (data?.stocks as Record<string, any>[]) ?? []
  const groups: Record<string, Record<string, any>[]> = {}
  for (const s of stocks) {
    const k = s.price?.setup ?? 'no_setup'
    ;(groups[k] = groups[k] ?? []).push(s)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-base font-bold text-slate-100">🚀 Breakout Scanner — 🇮🇳 India</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Fundamentals pick the names (constraint engine) · price + volume + delivery pick the moment · as of {String(data?.as_of ?? '…')}
          </p>
        </div>
        {country === 'US' && (
          <div className="text-[10px] text-amber-400">Price data available for India only — showing 🇮🇳 scan</div>
        )}
      </div>

      <div className="flex gap-1.5 flex-wrap items-center">
        <span className="text-[10px] text-slate-500 font-bold">SHORTLIST YEAR:</span>
        {YEARS.map(y => (
          <button key={y} onClick={() => setYear(y)}
            className={`px-3 py-1 rounded text-xs font-bold border transition-colors ${year === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:text-white'}`}>
            {y}
          </button>
        ))}
        <span className="text-[10px] text-slate-500 font-bold ml-4">SCAN AS OF:</span>
        <input
          type="date"
          value={asOf}
          min="2019-07-01"
          max={new Date().toISOString().slice(0, 10)}
          onChange={e => setAsOf(e.target.value)}
          className="px-2 py-1 rounded text-xs font-bold border bg-slate-800 border-slate-700 text-slate-200 [color-scheme:dark]"
        />
        {asOf && (
          <button onClick={() => setAsOf('')}
            className="px-2 py-1 rounded text-xs font-bold border bg-slate-800 border-slate-700 text-slate-400 hover:text-white"
            title="Reset to latest available price date">
            ✕ latest
          </button>
        )}
      </div>

      {isLoading && <div className="text-slate-500 text-sm py-10 text-center">Scanning price structures…</div>}
      {isError && <div className="text-red-400 text-sm py-10 text-center">Scan failed — is the backend running?</div>}

      {!isLoading && !isError && (
        <div className="space-y-5">
          {['breakout', 'coiling', 'basing', 'extended', 'no_setup'].map(k => {
            const list = groups[k] ?? []
            if (!list.length) return null
            const m = SETUP_META[k]
            return (
              <div key={k} className="space-y-2">
                <div className={`px-4 py-2 rounded-xl border ${m.bg}`}>
                  <span className={`text-sm font-black ${m.color}`}>{m.label}</span>
                  <span className="text-lg font-black float-right text-slate-300">{list.length}</span>
                  <div className="text-[11px] text-slate-500">{m.sub}</div>
                </div>
                <div className="space-y-1.5 ml-2">
                  {list.map((s, i) => <StockRow key={i} s={s} />)}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
