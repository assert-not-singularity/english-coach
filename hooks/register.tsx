import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Fix, Review } from '../types'

const MODEL = 'haiku'
const KEEP = 50
const OWN_WORDS = new Set(['composer', 'bridge'])

const reviews = atom({ plugin: 'english-coach', key: 'reviews' } as const, {} as Record<string, Review>)

const textKey = (text: string) => {
  let hash = 0x811c9dc5
  for (const char of text.trim().replace(/\s+/g, ' ')) {
    hash = Math.imul(hash ^ char.codePointAt(0)!, 0x01000193)
  }

  return (hash >>> 0).toString(16)
}

const script = ($: EngineInterface, args: string[], payload: unknown) =>
  $.process.run(['python3', '-I', `${$.plugin.root}/scripts/english-coach.py`, ...args], {
    stdin: JSON.stringify(payload),
    timeoutMs: 15_000,
  })

type Prepared = {
  coach: boolean
  text: string
  mode: 'written' | 'dictated'
  strip: boolean
  system?: string
  user?: string
}

const prepare = async ($: EngineInterface, text: string): Promise<Prepared> => {
  try {
    return JSON.parse((await script($, ['prepare'], { prompt: text })).stdout) as Prepared
  } catch {
    return { coach: false, text, mode: 'written', strip: false }
  }
}

// `shown` is the text as the message row will draw it, which is the key the fixes are looked up by.
const review = async ($: EngineInterface, prepared: Prepared, shown: string) => {
  try {
    const reply = await $.model.complete({
      model: MODEL,
      system: prepared.system,
      prompt: prepared.user ?? '',
      maxTokens: 700,
      timeoutMs: 30_000,
    })
    if (!reply.isAnswered) return

    const result = JSON.parse(
      (await script($, ['ingest'], { prompt: prepared.text, mode: prepared.mode, reply: reply.text })).stdout,
    )
    if (!result.ok) return
    if (result.hint) $.ui.toast(result.hint)
    if (result.careless || (result.fixes.length === 0 && !result.btw)) return

    const entry: Review = { fixes: result.fixes as Fix[], btw: result.btw ?? null, icon: result.icon ?? '✎' }
    await update($, reviews, held => Object.fromEntries(Object.entries({ ...held, [textKey(shown)]: entry }).slice(-KEEP)))
  } catch (error) {
    $.ui.toast(`english-coach: review failed (${String(error).slice(0, 80)})`)
  }
}

export const register: Register = on => {
  let isInteractive = true // session.start always runs before the first prompt and sets it

  on('session.start', async ($, e, next) => {
    isInteractive = e.isInteractive

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (!isInteractive || !OWN_WORDS.has(e.origin?.kind ?? 'composer')) return next(e)

    const prepared = await prepare($, e.text)
    const isStripped = prepared.strip && prepared.text !== e.text.trim()
    const entered = await next(isStripped ? { ...e, text: prepared.text } : e)
    if (entered.drop !== undefined) return entered

    if (prepared.coach) {
      const shown = isStripped ? prepared.text : e.text.trim()
      $.clock.after(0, () => void review($, prepared, shown))
    }

    return entered
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const drawn = await next(e)
    const held = (await read($, reviews))[textKey(e.props.text)]
    if (held === undefined) return drawn

    const { Box, Text } = $.ui.resolve(e)
    const mark = `${held.icon} ` // configurable: one icon for typed text, one for dictated text

    return (
      <Box flexDirection="column">
        {drawn}
        <Box flexDirection="column" paddingLeft={2}>
          {held.fixes.map((fix, index) => (
            <Box key={`fix-${index}`} flexDirection="column">
              <Text wrap="wrap">
                <Text dimColor>{mark}</Text>
                <Text color="error">{fix.original}</Text>
                <Text dimColor>{' → '}</Text>
                <Text color="success" bold>
                  {fix.fix}
                </Text>
                <Text dimColor>{`  ${fix.category}${fix.transfer ? ' · transfer' : ''}`}</Text>
              </Text>
              <Text dimColor italic wrap="wrap">
                {`  ${fix.explanation}`}
              </Text>
            </Box>
          ))}
          {held.btw === null ? null : (
            <Text dimColor wrap="wrap">
              {`✎ ${held.btw}`}
            </Text>
          )}
        </Box>
      </Box>
    )
  })
}
