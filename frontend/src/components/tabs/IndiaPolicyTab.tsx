/**
 * India PLI & Policy Intelligence Tab
 *
 * Shows major PLI schemes and Government policies for India, year-by-year.
 * Designed for someone who doesn't track policy daily — each policy shows:
 *   - What the government announced
 *   - Which industry it impacts
 *   - What it means for companies (tailwind / headwind)
 *   - Scale of impact (magnitude)
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchPLIPolicies } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

// Layman explanation of policy types
const POLICY_TYPE_EXPLAIN: Record<string, { short: string; color: string; bg: string }> = {
  PLI:            { short: 'Production incentive for manufacturers',    color: 'text-emerald-300', bg: 'bg-emerald-950/40' },
  BUDGET:         { short: 'Annual budget allocation',                  color: 'text-blue-300',    bg: 'bg-blue-950/40' },
  REGULATION:     { short: 'New rule companies must follow',            color: 'text-amber-300',   bg: 'bg-amber-950/40' },
  INFRASTRUCTURE: { short: 'Govt investment in physical infra',         color: 'text-violet-300',  bg: 'bg-violet-950/40' },
  TRADE:          { short: 'Import/export policy change',               color: 'text-sky-300',     bg: 'bg-sky-950/40' },
  SUBSIDY:        { short: 'Direct financial support to sector',        color: 'text-pink-300',    bg: 'bg-pink-950/40' },
  MANDATE:        { short: 'Govt requirement companies must meet',      color: 'text-orange-300',  bg: 'bg-orange-950/40' },
}

const IMPACT_COLOR: Record<string, string> = {
  positive: 'text-emerald-400',
  negative: 'text-red-400',
  neutral:  'text-slate-400',
  mixed:    'text-amber-400',
}
const IMPACT_ICON: Record<string, string> = {
  positive: '📈 Tailwind',
  negative: '📉 Headwind',
  neutral:  '➡️ Neutral',
  mixed:    '⚖️ Mixed',
}

const MAGNITUDE_LABEL: Record<number, string> = {
  1: 'Minor', 2: 'Moderate', 3: 'Significant', 4: 'Major', 5: 'Transformative',
}

function magnitudeBar(mag: number) {
  const filled = Math.min(5, Math.max(0, mag))
  return (
    <div className="flex gap-0.5">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className={`h-1.5 w-4 rounded-sm ${i < filled ? 'bg-amber-400' : 'bg-slate-700'}`} />
      ))}
      <span className="text-[10px] text-slate-500 ml-1">{MAGNITUDE_LABEL[filled] ?? ''}</span>
    </div>
  )
}

function PolicyCard({ policy }: { policy: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false)
  const pt     = String(policy.policy_type ?? '').toUpperCase()
  const ptMeta = POLICY_TYPE_EXPLAIN[pt] ?? { short: pt, color: 'text-slate-400', bg: 'bg-slate-800/40' }
  const impact = String(policy.impact_direction ?? 'neutral').toLowerCase()
  const mag    = Number(policy.impact_magnitude ?? 0)
  const date   = String(policy.enacted_date || policy.introduced_date || '').slice(0, 10)
  const status = String(policy.status ?? '')
  const techs  = (policy.technologies_affected as string[] | null) ?? []

  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden">
      <button onClick={() => setExpanded(e => !e)}
        className="w-full text-left px-4 py-3 hover:bg-slate-800/40 transition-colors">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              {/* Policy type chip */}
              <span className={`text-[10px] px-2 py-0.5 rounded font-bold ${ptMeta.color} ${ptMeta.bg}`}>
                {pt || 'POLICY'}
              </span>
              {/* Status */}
              {status && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${
                  status.toLowerCase() === 'enacted' ? 'text-emerald-300 border-emerald-800/50 bg-emerald-950/30' :
                  status.toLowerCase() === 'proposed' ? 'text-amber-300 border-amber-800/50 bg-amber-950/30' :
                  'text-slate-400 border-slate-700/50 bg-slate-800/30'
                }`}>{status}</span>
              )}
              {date && <span className="text-[10px] text-slate-500">{date}</span>}
            </div>
            <p className="text-sm font-semibold text-slate-100 leading-snug">
              {String(policy.title ?? '')}
            </p>
            {/* Layman description of policy type */}
            <p className="text-[11px] text-slate-500 mt-0.5">{ptMeta.short}</p>
          </div>
          {/* Impact direction */}
          <div className="flex-shrink-0 text-right">
            <div className={`text-xs font-bold ${IMPACT_COLOR[impact] ?? IMPACT_COLOR.neutral}`}>
              {IMPACT_ICON[impact] ?? '—'}
            </div>
            <div className="mt-1">
              {mag > 0 && magnitudeBar(mag)}
            </div>
          </div>
          <span className="text-slate-600 text-xs flex-shrink-0 ml-1">{expanded ? '▼' : '▶'}</span>
        </div>
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-slate-800/60 pt-3 space-y-3">
              {/* Budget / incentive */}
          {!!(policy.budget_crore || policy.incentive) && (
            <div className="grid grid-cols-2 gap-3 mb-1">
              {!!policy.budget_crore && (
                <div className="bg-emerald-950/30 rounded-lg px-3 py-2">
                  <div className="text-[10px] text-emerald-500 font-bold uppercase tracking-wide">Budget Outlay</div>
                  <div className="text-sm font-black text-emerald-300">
                    ₹{Number(policy.budget_crore).toLocaleString('en-IN')} Cr
                  </div>
                  <div className="text-[9px] text-slate-600">
                    ≈ USD {(Number(policy.budget_crore) / 8300).toFixed(1)} Bn
                  </div>
                </div>
              )}
              {!!policy.incentive && (
                <div className="bg-slate-800/50 rounded-lg px-3 py-2">
                  <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wide">Incentive Structure</div>
                  <div className="text-xs font-semibold text-slate-200">{String(policy.incentive)}</div>
                </div>
              )}
            </div>
          )}

          {/* Layman impact — the most important section */}
          {!!policy.layman_impact && (
            <div className="bg-indigo-950/30 border-l-4 border-indigo-600 rounded-r-lg px-3 py-2.5">
              <div className="text-[10px] text-indigo-400 font-bold uppercase tracking-wide mb-1.5">
                💡 What this means in plain language
              </div>
              <p className="text-xs text-slate-300 leading-relaxed">{String(policy.layman_impact)}</p>
            </div>
          )}

          {/* Key beneficiary companies */}
          {(policy.key_companies as string[] ?? []).length > 0 && (
            <div>
              <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wide mb-1.5">
                🏭 Key Beneficiary Companies
              </div>
              <div className="flex flex-wrap gap-1.5">
                {(policy.key_companies as string[]).map((co, i) => (
                  <span key={i} className="text-[10px] px-2 py-0.5 rounded bg-emerald-950/30 border border-emerald-800/40 text-emerald-300">
                    {co}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Technologies affected */}
          {techs.length > 0 && (
            <div>
              <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wide mb-1.5">
                🔧 Technologies / Products Covered
              </div>
              <div className="flex flex-wrap gap-1.5">
                {techs.map((tech, i) => (
                  <span key={i} className="text-[10px] px-2 py-0.5 rounded bg-indigo-950/40 border border-indigo-800/40 text-indigo-300">
                    {tech}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function IndiaPolicyTab(_props: Props) {
  const [selectedYear, setSelectedYear] = useState<number | undefined>(undefined)
  const [newOnly, setNewOnly]           = useState(false)   // show only schemes launched THIS year
  const [impactFilter, setImpactFilter] = useState('All')

  const { data: grouped = [], isLoading } = useQuery({
    queryKey: ['pli-policies', selectedYear, newOnly],
    queryFn: () => fetchPLIPolicies(
      selectedYear,
      newOnly ? 'new_only' : undefined    // backend filters to year == selected
    ),
  })

  const allGroups = grouped as Record<string, unknown>[]
  const allSectors = ['All', ...allGroups.map(g => String(g.sector ?? ''))]

  const displayGroups = allGroups.filter(g => {
    if (impactFilter === 'All') return true
    const policies = (g.policies as Record<string, unknown>[]) ?? []
    return policies.some(p => String(p.impact_direction ?? '').toLowerCase() === impactFilter.toLowerCase())
  })

  const totalPolicies = allGroups.reduce((s, g) => s + Number((g.policies as unknown[] ?? []).length), 0)

  return (
    <div className="space-y-4">

      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <div>
          <h2 className="text-base font-bold text-slate-100">🇮🇳 India PLI & Policy Intelligence</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Government policies and PLI schemes — what they mean for your portfolio
          </p>
        </div>
      </div>

      {/* What is PLI — for laymen */}
      <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl px-4 py-3 text-sm text-indigo-200">
        <strong>What is PLI?</strong> Production Linked Incentive — the Indian government pays companies
        cash rewards (typically 4–6% of incremental sales) for manufacturing in India.
        It's a <em>game-changer</em>: companies that qualify get directly paid to scale, creating
        structural margin expansion. Key sectors: Electronics, Solar, EV Batteries, Pharma, Specialty Steel.
      </div>

      {/* Controls */}
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Year</label>
          <div className="flex gap-1 flex-wrap">
            <button onClick={() => { setSelectedYear(undefined); setNewOnly(false) }}
              className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
                !selectedYear ? 'bg-indigo-700 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>All</button>
            {YEARS.map(y => (
              <button key={y} onClick={() => setSelectedYear(y)}
                className={`px-3 py-1 rounded text-xs font-semibold border transition-colors ${
                  selectedYear === y ? 'bg-indigo-700 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
                }`}>{y}</button>
            ))}
          </div>
        </div>

        {selectedYear && (
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Scope</label>
            <div className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
              <button onClick={() => setNewOnly(false)}
                className={`px-3 py-1 transition-colors ${!newOnly ? 'bg-slate-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                All active in {selectedYear}
              </button>
              <button onClick={() => setNewOnly(true)}
                className={`px-3 py-1 transition-colors ${newOnly ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                🆕 New launches only
              </button>
            </div>
          </div>
        )}

        {!isLoading && (
          <span className="text-xs text-slate-500 self-end pb-1">
            {totalPolicies} policies · {allGroups.length} sectors
          </span>
        )}
      </div>

      {isLoading && <div className="flex justify-center py-12"><Spinner /></div>}

      {!isLoading && displayGroups.length === 0 && (
        <EmptyState>
          No policy events found for these filters.<br />
          Run the macro pipeline for India to populate policy data.
        </EmptyState>
      )}

      {/* Sector groups */}
      <div className="space-y-4">
        {displayGroups.map((group, gi) => {
          const sector = String(group.sector ?? '')
          const policies = (group.policies as Record<string, unknown>[]) ?? []
          const filtered = impactFilter === 'All'
            ? policies
            : policies.filter(p => String(p.impact_direction ?? '').toLowerCase() === impactFilter.toLowerCase())

          const positiveCount = policies.filter(p => p.impact_direction === 'positive').length
          const negativeCount = policies.filter(p => p.impact_direction === 'negative').length

          return (
            <div key={gi}>
              {/* Sector header */}
              <div className="flex items-center gap-3 mb-2 px-1">
                <h3 className="text-sm font-bold text-slate-200">{sector}</h3>
                <span className="text-[10px] text-emerald-400 font-bold">↑ {positiveCount}</span>
                {negativeCount > 0 && <span className="text-[10px] text-red-400 font-bold">↓ {negativeCount}</span>}
                <span className="text-[10px] text-slate-600">{filtered.length} policies</span>
              </div>

              <div className="space-y-2">
                {filtered.map((policy, pi) => (
                  <PolicyCard key={pi} policy={policy} />
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
