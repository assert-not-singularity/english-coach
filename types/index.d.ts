export type Fix = {
  category: string
  transfer: boolean
  original: string
  fix: string
  explanation: string
  severity: 'error' | 'style'
  reminder?: string | null
}

export type Review = {
  fixes: Fix[]
  btw: string | null
  icon: string
}

declare module 'claude-code' {
  interface PluginState {
    'english-coach': { reviews: Record<string, Review> }
  }
}
