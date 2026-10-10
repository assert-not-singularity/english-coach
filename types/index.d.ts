export type Fix = {
  category: string
  transfer: boolean
  original: string
  fix: string
  explanation: string
}

export type Review = {
  fixes: Fix[]
  btw: string | null
  mode: 'written' | 'dictated'
}

declare module 'claude-code' {
  interface PluginState {
    'english-coach': { reviews: Record<string, Review> }
  }
}
