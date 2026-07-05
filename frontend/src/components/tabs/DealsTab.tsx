import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchDeals } from '../../api'
import { SectionHeader, Spinner, EmptyState } from '../ui'

const today = new Date().toISOString().slice(0, 10)

interface Row {
  deal_type: 'bulk' | 'block' | 'insider'
  exchange: string
  trade_date: string
  symbol: string
  security_name: string | null
  party_name: string | null
  designation: string | null
  buy_sell: string | null
  quantity: number | null
  trade_price: number | null
  value_traded: number | null
  post_transaction_holdings: number | null
  post_transaction_percentage: number | null
  remarks: string | null
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function dealTypeBadge(t: string) {
  const map: Record<string, string> = {
    bulk: 'bg-indigo-900/50 text-indigo-300',
    block: 'bg-purple-900/50 text-purple-300',
    insider: 'bg-amber-900/50 text-amber-300',
  }
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${map[t] ?? 'bg-slate-700 text-slate-300'}`}>
      {t}
    </span>
  )
}

function buySellBadge(v: string | null) {
  if (!v) return <span className="text-slate-500">—</span>
  const upper = v.toUpperCase()
  const isBuy = upper.includes('BUY') || upper.includes('ACQ') || upper.includes('B')
  const isSell = upper.includes('SELL') || upper.includes('DISP') || upper.includes('S')
  const cls = isBuy && !isSell ? 'text-emerald-400' : isSell && !isBuy ? 'text-red-400' : 'text-slate-300'
  return <span className={`font-semibold ${cls}`}>{v}</span>
}

export default function DealsTab() {
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [dealType, setDealType] = useState<'all' | 'bulk' | 'block' | 'insider'>('all')
  const [exchange, setExchange] = useState<'nse' | 'bse' | 'both'>('both')
  const [symbol, setSymbol] = useState('')
  const [limit, setLimit] = useState(200)
  const [searchTrigger, setSearchTrigger] = useState(0)

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['deals', startDate, endDate, dealType, exchange, symbol, limit, searchTrigger],
    queryFn: () => fetchDeals(startDate, endDate, dealType, exchange, symbol.trim() || undefined, limit),
    enabled: searchTrigger > 0,
  })

  const results = (data?.results ?? []) as Row[]

  return (
    <div className="space-y-4">
      <SectionHeader>🤝 Bulk / Block / Insider Deals</SectionHeader>
      <p className="text-xs text-slate-500 -mt-2">
        Browse bulk deals, block deals, and insider trading disclosures migrated from the Algo_Test
        database. Filter by date range, deal type, exchange, and optionally a specific symbol.
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
          <label className="text-xs text-slate-400 mb-1 block">Deal type</label>
          <select value={dealType} onChange={e => setDealType(e.target.value as typeof dealType)} className="select">
            <option value="all">All</option>
            <option value="bulk">Bulk deals</option>
            <option value="block">Block deals</option>
            <option value="insider">Insider trades</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Exchange</label>
          <select value={exchange} onChange={e => setExchange(e.target.value as typeof exchange)} className="select">
            <option value="both">Both</option>
            <option value="nse">NSE</option>
            <option value="bse">BSE</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Symbol (optional)</label>
          <input type="text" value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())}
            placeholder="RELIANCE" className="input w-32" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Limit</label>
          <input type="number" min={1} max={1000} value={limit}
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
        <EmptyState>Set a date range and filters, then click Search.</EmptyState>
      )}

      {searchTrigger > 0 && !isLoading && results.length === 0 && !error && (
        <EmptyState>No deals found matching these filters.</EmptyState>
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
                  {['#', 'Type', 'Exch', 'Date', 'Symbol', 'Security', 'Party / Insider', 'Designation',
                    'B/S', 'Quantity', 'Price', 'Value Traded', 'Post Holdings', 'Post %', 'Remarks'].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={`${r.deal_type}-${r.exchange}-${r.symbol}-${r.trade_date}-${i}`}
                    className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="px-3 py-2 text-slate-500">{i + 1}</td>
                    <td className="px-3 py-2">{dealTypeBadge(r.deal_type)}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        r.exchange === 'NSE' ? 'bg-sky-900/50 text-sky-300' : 'bg-orange-900/50 text-orange-300'
                      }`}>{r.exchange}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-300 whitespace-nowrap">{r.trade_date}</td>
                    <td className="px-3 py-2 text-slate-200 font-semibold">{r.symbol}</td>
                    <td className="px-3 py-2 text-slate-400 max-w-[160px] truncate" title={r.security_name ?? ''}>{r.security_name ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300 max-w-[200px] truncate" title={r.party_name ?? ''}>{r.party_name ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-400">{r.designation ?? '—'}</td>
                    <td className="px-3 py-2">{buySellBadge(r.buy_sell)}</td>
                    <td className="px-3 py-2 text-slate-300">{fmtNum(r.quantity)}</td>
                    <td className="px-3 py-2 text-slate-300">{r.trade_price !== null ? fmtNum(r.trade_price) : '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.value_traded !== null ? fmtNum(r.value_traded) : '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.post_transaction_holdings !== null ? fmtNum(r.post_transaction_holdings) : '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.post_transaction_percentage !== null ? `${r.post_transaction_percentage}%` : '—'}</td>
                    <td className="px-3 py-2 text-slate-500 max-w-[200px] truncate" title={r.remarks ?? ''}>{r.remarks ?? '—'}</td>
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
