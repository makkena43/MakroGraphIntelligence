import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchHighVolumeStocks } from '../../api'
import { SectionHeader, Spinner, EmptyState } from '../ui'

const today = new Date().toISOString().slice(0, 10)

interface Row {
  exchange: string
  symbol: string
  series: string | null
  trade_date: string
  volume: number
  value: number | null
  close: number | null
  delivery_pct: number | null
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

export default function HighVolumeTab() {
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [exchange, setExchange] = useState<'nse' | 'bse' | 'both'>('both')
  const [minVolume, setMinVolume] = useState(1_000_000)
  const [limit, setLimit] = useState(100)
  const [searchTrigger, setSearchTrigger] = useState(0)

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['high-volume-stocks', startDate, endDate, exchange, minVolume, limit, searchTrigger],
    queryFn: () => fetchHighVolumeStocks(startDate, endDate, exchange, minVolume, limit),
    enabled: searchTrigger > 0,
  })

  const results = (data?.results ?? []) as Row[]

  return (
    <div className="space-y-4">
      <SectionHeader>🔥 Highest Volume Days</SectionHeader>
      <p className="text-xs text-slate-500 -mt-2">
        For each symbol, finds its all-time (lifetime) highest-volume trading day across the entire
        history table, then keeps only symbols whose lifetime-peak day falls within the selected date
        range and exceeds the minimum volume — i.e. stocks that set a new all-time volume record during
        this window.
      </p>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Start date</label>
          <input type="date" value={startDate} max={today}
            onChange={e => setStartDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">End date</label>
          <input type="date" value={endDate} max={today}
            onChange={e => setEndDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Exchange</label>
          <select value={exchange} onChange={e => setExchange(e.target.value as 'nse' | 'bse' | 'both')} className="select">
            <option value="both">Both</option>
            <option value="nse">NSE</option>
            <option value="bse">BSE</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min volume</label>
          <input type="number" min={0} step={100_000} value={minVolume}
            onChange={e => setMinVolume(+e.target.value)} className="input w-32" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Limit</label>
          <input type="number" min={1} max={500} value={limit}
            onChange={e => setLimit(+e.target.value)} className="input w-20" />
        </div>
        <button
          onClick={() => setSearchTrigger(t => t + 1)}
          disabled={isFetching}
          className="btn-primary flex items-center gap-2"
        >
          {isFetching ? <Spinner size="sm" /> : '🔍'}
          {isFetching ? 'Searching…' : 'Search'}
        </button>
      </div>

      {error && (
        <div className="text-sm text-red-400">Error: {String((error as Error).message)}</div>
      )}

      {searchTrigger === 0 && !isLoading && (
        <EmptyState>Set a date range and minimum volume, then click Search.</EmptyState>
      )}

      {searchTrigger > 0 && !isLoading && results.length === 0 && !error && (
        <EmptyState>No symbols found with peak-day volume ≥ {fmtNum(minVolume)} in this range.</EmptyState>
      )}

      {results.length > 0 && (
        <>
          <div className="text-xs text-slate-500">
            Showing {data.count} of {data.total_matches} matches
          </div>
          <div className="overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-800 border-b border-slate-700">
                  {['#', 'Exchange', 'Symbol', 'Series', 'Peak Volume Date', 'Volume', 'Value (₹)', 'Close', 'Delivery %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={`${r.exchange}-${r.symbol}`} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="px-3 py-2 text-slate-500">{i + 1}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        r.exchange === 'NSE' ? 'bg-indigo-900/50 text-indigo-300' : 'bg-amber-900/50 text-amber-300'
                      }`}>{r.exchange}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-200 font-semibold">{r.symbol}</td>
                    <td className="px-3 py-2 text-slate-400">{r.series ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.trade_date}</td>
                    <td className="px-3 py-2 text-emerald-400 font-medium">{fmtNum(r.volume)}</td>
                    <td className="px-3 py-2 text-slate-300">{fmtNum(r.value)}</td>
                    <td className="px-3 py-2 text-slate-300">{r.close ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.delivery_pct !== null ? `${r.delivery_pct}%` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
