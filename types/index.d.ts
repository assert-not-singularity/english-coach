export type Fix = {
  category: string
  transfer: boolean
  original: string
  fix: string
  explanation: string
  severity: 'error' | 'style'
  reminder?: string | null
  parts?: { original: [string, boolean][]; fix: [string, boolean][] }
  id?: number | null
}

export type AuditSuspect = {
  id: number
  verdict: 'not-error' | 'style' | 'wrong-fix' | 'wrong-category'
  category: string
  current: string
  reason: string
  original: string
  fix: string
  core: { wrong: string; right: string }
}

export type AuditInfo = { scope: 'off' | 'flagged' | 'all'; audited: number; agree: number; suspects: AuditSuspect[]; misses: number }

export type Phrase = { id: string; phrase: string; meaning: string; example: string; original: string }

export type Review = {
  fixes: Fix[]
  phrase: Phrase | null
  btw: string | null
  icon: string
}

export type Mode = 'both' | 'written' | 'dictated'

export type Example = { original: string; fix: string; explanation: string; transfer?: boolean; core?: { wrong: string; right: string } }

export type CategoryStats = {
  category: string
  total: number
  weekly: (number | null)[]
  trend: 'down' | 'up' | 'flat' | 'n/a'
  examples: Example[]
  summary: { headline: string; detail: string } | null
  summary_stale: boolean
}

export type WatchEntry = {
  wrong: string
  right: string
  hits: number
  streak: number
  status: string
  note: string
  manual: boolean
}

export type Overview = {
  audit: AuditInfo
  mode: Mode
  since: string | null
  messages: number
  words: number
  errors: number
  slips: number
  styles: number
  clean_weekly: (number | null)[]
  clean_now: number | null
  categories: CategoryStats[]
  transfer: { native: string | null; weekly: (number | null)[]; trend: 'down' | 'up' | 'flat' | 'n/a'; total: number; share: number | null }
  weeks_with_data: number
  focus: string[]
  focus_stale: boolean
  summary_state: 'not_enough' | 'none' | 'stale' | 'ready'
  errors_needed: number
  errors_per_100_words: number | null
  rate_weekly: (number | null)[]
  rate_trend: 'down' | 'up' | 'flat' | 'n/a'
  sentences: number
  error_free_sentences: number | null
  length_buckets: { label: string; messages: number; clean_share: number; per_100_words: number | null }[]
  watchlist: WatchEntry[]
  slip_examples: Example[]
  style_examples: Example[]
  messages_by_mode: Record<string, number>
}

export type OverviewState = { loadedAt: number; data: Record<Mode, Overview> } | null

declare module 'claude-code' {
  interface PluginState {
    'english-coach': { reviews: Record<string, Review>; tab: Mode; overview: OverviewState; expanded: string[]; summarizing: number }
  }
}
