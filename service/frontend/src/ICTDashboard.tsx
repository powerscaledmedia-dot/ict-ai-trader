import { useEffect, useState, useCallback } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, BarChart, Bar, Cell } from 'recharts'

const API = import.meta.env.VITE_API_BASE || 'http://localhost:8000'
const POLL_MS = 10_000  // refresh every 10s

// ── Types ────────────────────────────────────────────────────────
interface ICTStatus {
  killzone: {
    status: 'TRADE' | 'WATCH' | 'BLOCKED'
    session: string | null
    reason: string
    minutes_to_next: number | null
    schedule: Record<string, { utc: string; cst: string }>
  }
  risk: {
    daily_pnl: number
    daily_loss_limit: number
    loss_used_pct: number
    open_positions: number
    max_positions: number
    consecutive_losses: number
    cooldown_active: boolean
    cooldown_until: string | null
    account_equity: number
  }
  news: {
    status: 'CLEAR' | 'CAUTION' | 'HALT'
    reason: string
    headlines: string[]
  }
  setup_weights: Record<string, number>
  tradovate_connected: boolean
}

interface Trade {
  id: number
  trade_uuid: string
  setup_type: string
  direction: string
  instrument: string
  entry_price: number
  exit_price: number | null
  stop_price: number
  target_price: number
  pnl: number
  outcome: string | null
  grade: string
  score: number
  killzone: string
  status: string
  opened_at: string
  closed_at: string | null
  contracts: number
}

// ── Helpers ──────────────────────────────────────────────────────
function statusColor(s: string) {
  if (s === 'TRADE' || s === 'APPROVED' || s === 'CLEAR') return '#22c55e'
  if (s === 'WATCH' || s === 'CAUTION') return '#f59e0b'
  return '#ef4444'
}

function statusBg(s: string) {
  if (s === 'TRADE' || s === 'APPROVED' || s === 'CLEAR') return 'rgba(34,197,94,0.12)'
  if (s === 'WATCH' || s === 'CAUTION') return 'rgba(245,158,11,0.12)'
  return 'rgba(239,68,68,0.12)'
}

function formatPnl(v: number) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${v.toFixed(0)}`
}

// ── Session Clock ────────────────────────────────────────────────
function SessionClock({ status }: { status: ICTStatus | null }) {
  const [now, setNow] = useState(new Date())

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const utcHour = now.getUTCHours()
  const utcMin = now.getUTCMinutes()
  const utcSec = now.getUTCSeconds()
  const utcStr = `${String(utcHour).padStart(2,'0')}:${String(utcMin).padStart(2,'0')}:${String(utcSec).padStart(2,'0')} UTC`

  const sessions = [
    { name: 'Asia', start: 1, end: 5, label: '01–05 UTC / 19–23 CST' },
    { name: 'London', start: 7, end: 11, label: '07–11 UTC / 01–05 CST' },
    { name: 'NY', start: 13, end: 17, label: '13–17 UTC / 07–11 CST' },
  ]

  const frac = utcHour + utcMin / 60
  const active = sessions.find(s => frac >= s.start && frac < s.end)

  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: 20 }}>
      <div style={{ fontSize: 13, color: '#666', marginBottom: 8 }}>SESSION CLOCK</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: '#fff', fontFamily: 'monospace', marginBottom: 16 }}>
        {utcStr}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        {sessions.map(s => {
          const isActive = s === active
          const frac2 = utcHour + utcMin / 60
          const approaching = !isActive && Math.abs(frac2 - s.start) < 0.5
          const bg = isActive ? statusBg('TRADE') : approaching ? statusBg('CAUTION') : 'rgba(255,255,255,0.04)'
          const border = isActive ? statusColor('TRADE') : approaching ? statusColor('CAUTION') : '#333'
          return (
            <div key={s.name} style={{
              flex: 1, padding: '10px 12px', borderRadius: 8,
              background: bg, border: `1px solid ${border}`,
            }}>
              <div style={{ fontWeight: 700, color: isActive ? statusColor('TRADE') : '#aaa', fontSize: 14 }}>
                {s.name} {isActive ? '● LIVE' : approaching ? '◐ SOON' : ''}
              </div>
              <div style={{ fontSize: 11, color: '#555', marginTop: 3 }}>{s.label}</div>
            </div>
          )
        })}
      </div>
      {status?.killzone.minutes_to_next && !active && (
        <div style={{ marginTop: 10, fontSize: 12, color: '#f59e0b' }}>
          Next session in {status.killzone.minutes_to_next} min
        </div>
      )}
    </div>
  )
}

// ── Agent Status Board ────────────────────────────────────────────
function AgentStatusBoard({ status }: { status: ICTStatus | null }) {
  if (!status) return null
  const kz = status.killzone
  const risk = status.risk
  const news = status.news

  const agents = [
    {
      name: 'Killzone Manager',
      status: kz.status,
      detail: kz.reason,
    },
    {
      name: 'ICT Scanner',
      status: 'CLEAR' as const,
      detail: Object.entries(status.setup_weights)
        .map(([k, v]) => `${k}: ${(v * 100).toFixed(0)}%`)
        .join(' · '),
    },
    {
      name: 'Risk Governor',
      status: risk.cooldown_active ? 'BLOCKED' : risk.loss_used_pct > 0.7 ? 'CAUTION' : 'CLEAR' as any,
      detail: `P&L: ${formatPnl(risk.daily_pnl)} · Positions: ${risk.open_positions}/${risk.max_positions} · Streak: ${risk.consecutive_losses} losses`,
    },
    {
      name: 'News Sentinel',
      status: news.status,
      detail: news.reason,
    },
  ]

  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: 20 }}>
      <div style={{ fontSize: 13, color: '#666', marginBottom: 12 }}>AGENT STATUS</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {agents.map(a => (
          <div key={a.name} style={{
            display: 'flex', alignItems: 'center', gap: 12,
            padding: '10px 14px', borderRadius: 8,
            background: statusBg(a.status),
            border: `1px solid ${statusColor(a.status)}22`,
          }}>
            <div style={{
              width: 10, height: 10, borderRadius: '50%',
              background: statusColor(a.status), flexShrink: 0,
              boxShadow: `0 0 6px ${statusColor(a.status)}`,
            }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: '#ddd' }}>{a.name}</div>
              <div style={{ fontSize: 11, color: '#666', marginTop: 2 }}>{a.detail}</div>
            </div>
            <div style={{ fontSize: 12, fontWeight: 700, color: statusColor(a.status) }}>
              {a.status}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Drawdown Meter ────────────────────────────────────────────────
function DrawdownMeter({ risk }: { risk: ICTStatus['risk'] | undefined }) {
  if (!risk) return null
  const usedPct = risk.loss_used_pct * 100
  const dailyLoss = Math.abs(Math.min(risk.daily_pnl, 0))
  const remaining = risk.daily_loss_limit - dailyLoss
  const evalTarget = 3000  // TopStep 50K profit target

  const barColor = usedPct > 70 ? '#ef4444' : usedPct > 40 ? '#f59e0b' : '#22c55e'

  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: 20 }}>
      <div style={{ fontSize: 13, color: '#666', marginBottom: 16 }}>EVAL PROGRESS</div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
        <div>
          <div style={{ fontSize: 11, color: '#666' }}>Daily P&L</div>
          <div style={{ fontSize: 24, fontWeight: 700, color: risk.daily_pnl >= 0 ? '#22c55e' : '#ef4444' }}>
            {formatPnl(risk.daily_pnl)}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 11, color: '#666' }}>Daily Limit Remaining</div>
          <div style={{ fontSize: 24, fontWeight: 700, color: remaining < 500 ? '#ef4444' : '#aaa' }}>
            ${remaining.toFixed(0)}
          </div>
        </div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#666' }}>Daily Loss Used</span>
          <span style={{ fontSize: 12, color: barColor }}>{usedPct.toFixed(1)}%</span>
        </div>
        <div style={{ height: 8, background: '#222', borderRadius: 4, overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${Math.min(usedPct, 100)}%`,
            background: barColor, borderRadius: 4,
            transition: 'width 0.5s ease',
          }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
          <span style={{ fontSize: 10, color: '#555' }}>$0</span>
          <span style={{ fontSize: 10, color: '#f59e0b' }}>Halt at $1,700</span>
          <span style={{ fontSize: 10, color: '#ef4444' }}>${risk.daily_loss_limit.toLocaleString()}</span>
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#666' }}>Eval Profit Target ($3,000)</span>
          <span style={{ fontSize: 12, color: '#22c55e' }}>{((risk.daily_pnl / evalTarget) * 100).toFixed(1)}%</span>
        </div>
        <div style={{ height: 6, background: '#222', borderRadius: 3, overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${Math.min(Math.max((risk.daily_pnl / evalTarget) * 100, 0), 100)}%`,
            background: '#22c55e', borderRadius: 3,
          }} />
        </div>
      </div>
    </div>
  )
}

// ── Trade Log ─────────────────────────────────────────────────────
function TradeLog({ trades }: { trades: Trade[] }) {
  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: 20 }}>
      <div style={{ fontSize: 13, color: '#666', marginBottom: 12 }}>TRADE LOG</div>
      {trades.length === 0 && (
        <div style={{ color: '#555', fontSize: 13, padding: '20px 0', textAlign: 'center' }}>
          No trades yet. Waiting for TradingView signals...
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {trades.slice(0, 20).map(t => {
          const isOpen = t.status === 'open'
          const pnl = t.pnl || 0
          const pnlColor = pnl > 0 ? '#22c55e' : pnl < 0 ? '#ef4444' : '#888'
          return (
            <div key={t.id} style={{
              display: 'grid',
              gridTemplateColumns: '1fr auto auto auto auto',
              gap: 12, alignItems: 'center',
              padding: '10px 14px',
              background: isOpen ? 'rgba(34,197,94,0.06)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${isOpen ? '#22c55e33' : '#1e1e1e'}`,
              borderRadius: 8, fontSize: 12,
            }}>
              <div>
                <div style={{ fontWeight: 600, color: '#ddd' }}>
                  <span style={{ color: t.direction === 'bullish' ? '#22c55e' : '#ef4444' }}>
                    {t.direction === 'bullish' ? '▲' : '▼'}
                  </span>{' '}
                  {t.setup_type} · {t.instrument}
                </div>
                <div style={{ color: '#555', marginTop: 2 }}>
                  {t.killzone?.toUpperCase()} · Grade {t.grade} · {t.contracts}ct
                </div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ color: '#888' }}>Entry</div>
                <div style={{ color: '#ddd', fontWeight: 600 }}>{t.entry_price?.toFixed(2)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ color: '#888' }}>Stop</div>
                <div style={{ color: '#ef4444' }}>{t.stop_price?.toFixed(2)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ color: '#888' }}>Target</div>
                <div style={{ color: '#22c55e' }}>{t.target_price?.toFixed(2)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{
                  fontSize: 11, fontWeight: 700, marginBottom: 2,
                  color: isOpen ? '#f59e0b' : pnlColor,
                }}>
                  {isOpen ? 'OPEN' : t.outcome?.toUpperCase()}
                </div>
                <div style={{ fontWeight: 700, color: pnlColor }}>{formatPnl(pnl)}</div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Setup Weights Chart ───────────────────────────────────────────
function SetupWeightsChart({ weights }: { weights: Record<string, number> }) {
  const data = Object.entries(weights).map(([name, value]) => ({ name: name.replace('_', ' '), value: +(value * 100).toFixed(0) }))
  return (
    <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: 20 }}>
      <div style={{ fontSize: 13, color: '#666', marginBottom: 12 }}>SETUP WEIGHTS (AI-Updated Nightly)</div>
      <ResponsiveContainer width="100%" height={160}>
        <BarChart data={data} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" />
          <XAxis dataKey="name" tick={{ fill: '#666', fontSize: 10 }} />
          <YAxis domain={[0, 100]} tick={{ fill: '#666', fontSize: 10 }} unit="%" />
          <Tooltip
            contentStyle={{ background: '#1a1a1a', border: '1px solid #333', borderRadius: 6 }}
            labelStyle={{ color: '#aaa' }}
            itemStyle={{ color: '#22c55e' }}
          />
          <Bar dataKey="value" radius={[4, 4, 0, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.value >= 80 ? '#22c55e' : entry.value >= 60 ? '#f59e0b' : '#ef4444'} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ── Main ICT Dashboard ────────────────────────────────────────────
export function ICTDashboard() {
  const [status, setStatus] = useState<ICTStatus | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)

  const fetchAll = useCallback(async () => {
    try {
      const [statusRes, tradesRes] = await Promise.all([
        fetch(`${API}/ict/status`),
        fetch(`${API}/ict/trades?limit=30`),
      ])
      if (statusRes.ok) setStatus(await statusRes.json())
      if (tradesRes.ok) {
        const data = await tradesRes.json()
        setTrades(data.trades || [])
      }
      setLastUpdate(new Date())
    } catch (e) {
      console.error('ICT status fetch failed:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const t = setInterval(fetchAll, POLL_MS)
    return () => clearInterval(t)
  }, [fetchAll])

  const overallStatus = status
    ? status.killzone.status === 'BLOCKED' || status.news.status === 'HALT'
      ? 'HALT'
      : status.killzone.status === 'TRADE' && status.news.status === 'CLEAR'
        ? 'ACTIVE'
        : 'WATCH'
    : 'LOADING'

  const headerColor = overallStatus === 'ACTIVE' ? '#22c55e' : overallStatus === 'HALT' ? '#ef4444' : '#f59e0b'

  return (
    <div style={{
      minHeight: '100vh', background: '#0a0a0a', color: '#fff',
      fontFamily: "'Inter', system-ui, sans-serif", padding: '24px',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24 }}>
        <div>
          <div style={{ fontSize: 24, fontWeight: 800, letterSpacing: '-0.5px' }}>
            ICT Trading Platform
          </div>
          <div style={{ fontSize: 13, color: '#555', marginTop: 2 }}>
            TopStep + Lucid · ICT Methodology · Asia / London / NY
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {lastUpdate && (
            <div style={{ fontSize: 11, color: '#444' }}>
              Updated {lastUpdate.toLocaleTimeString()}
            </div>
          )}
          <div style={{
            padding: '8px 16px', borderRadius: 20,
            background: `${headerColor}22`,
            border: `1px solid ${headerColor}`,
            color: headerColor, fontWeight: 700, fontSize: 13,
          }}>
            {overallStatus}
          </div>
        </div>
      </div>

      {loading && (
        <div style={{ textAlign: 'center', color: '#555', padding: 60 }}>
          Connecting to AI-Trader backend...
        </div>
      )}

      {!loading && (
        <div style={{ display: 'grid', gap: 20 }}>
          {/* Row 1: Session Clock + Agent Status */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
            <SessionClock status={status} />
            <AgentStatusBoard status={status} />
          </div>

          {/* Row 2: Drawdown Meter + Setup Weights */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
            <DrawdownMeter risk={status?.risk} />
            <SetupWeightsChart weights={status?.setup_weights || {}} />
          </div>

          {/* Row 3: News Headlines */}
          {status?.news.headlines && status.news.headlines.length > 0 && (
            <div style={{ background: '#111', border: `1px solid ${statusColor(status.news.status)}44`, borderRadius: 12, padding: 20 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
                <div style={{ fontSize: 13, color: '#666' }}>NEWS SENTINEL</div>
                <div style={{ fontSize: 12, fontWeight: 700, color: statusColor(status.news.status) }}>
                  {status.news.status} — {status.news.reason}
                </div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {status.news.headlines.map((h, i) => (
                  <div key={i} style={{ fontSize: 12, color: '#888', paddingLeft: 10, borderLeft: `2px solid #333` }}>
                    {h}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Row 4: Trade Log */}
          <TradeLog trades={trades} />
        </div>
      )}
    </div>
  )
}
