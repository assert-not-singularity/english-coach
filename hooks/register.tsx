import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Fix, Review } from '../types'
import { COMMAND, registerOverview } from './overview'

const MODEL = 'haiku'
const KEEP = 50
const CONTEXT_CHARS = 8_000 // how much of the assistant's recent text the quote guard may compare against
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

// What the assistant said last, for the quote guard: a phrase the user repeats from it is a quotation, not their writing.
// Read when the prompt is submitted, handed to the script for one call, never stored.
const recentAssistantText = async ($: EngineInterface): Promise<string> => {
  try {
    const messages = await $.session.messages({ as: 'api' })
    const texts: string[] = []
    for (let index = messages.length - 1; index >= 0 && texts.length < 2; index--) {
      const message = messages[index]
      if (message === undefined || message.role !== 'assistant') continue
      const content = message.content
      const text =
        typeof content === 'string'
          ? content
          : content.map(block => ('text' in block && typeof block.text === 'string' ? block.text : '')).join('\n')
      if (text.trim() !== '') texts.push(text)
    }

    return texts.reverse().join('\n').slice(-CONTEXT_CHARS)
  } catch {
    return ''
  }
}

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
// Prepares the overview's written summaries in the background when the backend says they are due, so opening the pane shows them at once.
const refreshSummaries = async ($: EngineInterface) => {
  try {
    const due = JSON.parse((await script($, ['summary-due'], {})).stdout)
    for (const mode of due.modes as string[]) {
      const input = JSON.parse((await script($, ['summary-input', mode], {})).stdout)
      if (input.categories.length === 0) continue
      const reply = await $.model.complete({ model: input.model, system: input.system, prompt: input.prompt, maxTokens: 1500, timeoutMs: 180_000 })
      if (reply.isAnswered) await script($, ['summary-store', mode], { reply: reply.text })
    }
  } catch {
    // The next check tries again.
  }
}

const review = async ($: EngineInterface, prepared: Prepared, shown: string, context: string) => {
  try {
    const reply = await $.model.complete({
      model: MODEL,
      system: prepared.system,
      prompt: prepared.user ?? '',
      maxTokens: 700,
      timeoutMs: 30_000,
    })
    if (!reply.isAnswered) return

    // A dictated passage the first review found garbled is probably a transcription error: ask the larger model once.
    let answer = reply.text
    try {
      const decision = JSON.parse(
        (await script($, ['check'], { prompt: prepared.text, mode: prepared.mode, reply: reply.text, context })).stdout,
      )
      if (decision.escalate && decision.model) {
        const second = await $.model.complete({
          model: decision.model,
          system: prepared.system,
          prompt: (prepared.user ?? '') + (decision.note ?? ''),
          maxTokens: 700,
          timeoutMs: 45_000,
        })
        if (second.isAnswered) answer = second.text
      }
    } catch {
      // Without the second opinion the first review stands.
    }

    const result = JSON.parse(
      (await script($, ['ingest'], { prompt: prepared.text, mode: prepared.mode, reply: answer, context })).stdout,
    )
    if (!result.ok) return
    if (result.hint) $.ui.toast(result.hint)
    if (result.careless || (result.fixes.length === 0 && !result.btw && !result.phrase)) return

    const entry: Review = { fixes: result.fixes as Fix[], phrase: result.phrase ?? null, btw: result.btw ?? null, icon: result.icon ?? '✎' }
    await update($, reviews, held => Object.fromEntries(Object.entries({ ...held, [textKey(shown)]: entry }).slice(-KEEP)))
  } catch (error) {
    $.ui.toast(`english-coach: review failed (${String(error).slice(0, 80)})`)
  }
}

export const register: Register = on => {
  registerOverview(on)

  let isInteractive = true // session.start always runs before the first prompt and sets it

  on('session.start', async ($, e, next) => {
    isInteractive = e.isInteractive
    await $.command.register({ name: COMMAND, description: 'English coach overview: written, spoken or both' })
    if (e.isInteractive) {
      $.clock.after(60_000, () => void refreshSummaries($))
      $.clock.every(86_400_000, () => void refreshSummaries($))
    }

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (!isInteractive || !OWN_WORDS.has(e.origin?.kind ?? 'composer')) return next(e)

    const prepared = await prepare($, e.text)
    const context = prepared.coach ? await recentAssistantText($) : ''
    const isStripped = prepared.strip && prepared.text !== e.text.trim()
    const entered = await next(isStripped ? { ...e, text: prepared.text } : e)
    if (entered.drop !== undefined) return entered

    if (prepared.coach) {
      const shown = isStripped ? prepared.text : e.text.trim()
      $.clock.after(0, () => void review($, prepared, shown, context))
    }

    return entered
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const drawn = await next(e)
    const held = (await read($, reviews))[textKey(e.props.text)]
    if (held === undefined) return drawn

    const { Box, Text } = $.ui.resolve(e)
    const mark = `${held.icon} ` // configurable: one icon for typed text, one for dictated text
    // The correction as word segments: the changed words are coloured, their neighbours stay quiet, and long stretches shrink to "…".
    const pieces = (segments: [string, boolean][], color: 'error' | 'success') =>
      segments.map(([text, changed], index) => (
        <Text key={`piece-${index}`} color={changed ? color : undefined} dimColor={!changed} bold={changed && color === 'success'}>
          {(index > 0 ? ' ' : '') + text}
        </Text>
      ))

    return (
      <Box flexDirection="column">
        {drawn}
        <Box flexDirection="column" paddingLeft={2}>
          {held.fixes.map((fix, index) => (
            <Box key={`fix-${index}`} flexDirection="column">
              <Text wrap="wrap">
                <Text dimColor>{mark}</Text>
                {fix.severity === 'style' ? (
                  <Text dimColor>{`${fix.original} → ${fix.fix}  style, not an error`}</Text>
                ) : fix.parts ? (
                  pieces(fix.parts.original, 'error')
                ) : (
                  <Text color="error">{fix.original}</Text>
                )}
                {fix.severity === 'style' ? null : <Text dimColor>{' → '}</Text>}
                {fix.severity === 'style' ? null : fix.parts ? (
                  pieces(fix.parts.fix, 'success')
                ) : (
                  <Text color="success" bold>
                    {fix.fix}
                  </Text>
                )}
                {fix.severity === 'style' ? null : (
                  <Text dimColor>{`  ${fix.category}${fix.transfer ? ' · transfer' : ''}`}</Text>
                )}
              </Text>
              <Text dimColor italic wrap="wrap">
                {`  ${fix.explanation}`}
              </Text>
              {fix.reminder ? (
                <Text dimColor wrap="wrap">
                  {`  ↻ ${fix.reminder}`}
                </Text>
              ) : null}
            </Box>
          ))}
          {!held.phrase ? null : (
            <Box flexDirection="column">
              <Text wrap="wrap">
                <Text color="suggestion">{'✦ '}</Text>
                <Text color="suggestion" bold>
                  {held.phrase.phrase}
                </Text>
                <Text dimColor>{`  ${held.phrase.meaning}`}</Text>
              </Text>
              <Text dimColor italic wrap="wrap">
                {`  where you wrote "${held.phrase.original}", for example: ${held.phrase.example}`}
              </Text>
            </Box>
          )}
          {held.btw === null ? null : (
            <Text dimColor wrap="wrap">
              {`${held.icon} ${held.btw}`}
            </Text>
          )}
        </Box>
      </Box>
    )
  })
}
