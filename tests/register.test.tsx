import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const PROMPT = 'Can you explain me why this fails, please?'

const INGESTED = JSON.stringify({
  ok: true,
  careless: false,
  btw: null,
  hint: null,
  fixes: [
    {
      category: 'preposition',
      transfer: true,
      original: 'explain me',
      fix: 'explain to me',
      explanation: "'Explain' takes 'to' before the person.",
      severity: 'error',
      parts: { original: [['Can you', false], ['explain me', true], ['why this', false]], fix: [['Can you', false], ['explain to me', true], ['why this', false]] },
    },
  ],
})
const CARELESS = JSON.stringify({ ok: true, careless: true, btw: null, hint: null, fixes: [] })

const USAGE = { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }

const MARKER = ' [dictated]'

// The world beneath the plugin: the python script answers canned JSON (a prepare that knows the marker), the model
// answers once, the engine accepts the prompt and draws a bare row. `seen` records what the mod handed on.
const world = (on: On, ingested: string) => {
  const clock = mock.clock(on)
  const seen = { entered: '', ingestMode: '', ingestContext: '', ingestReply: '', models: [] as string[], prompts: [] as string[], escalate: false, auditStored: '', prepareContext: '' }

  on('process.run', async (_$, e) => {
    const sent = JSON.parse(e.init?.stdin ?? '{}')
    const isSpeech = typeof sent.prompt === 'string' && sent.prompt.endsWith(MARKER)
    const text = isSpeech ? sent.prompt.slice(0, -MARKER.length) : sent.prompt
    if (e.argv.includes('prepare')) seen.prepareContext = sent.context ?? ''
    if (e.argv.includes('audit-store')) seen.auditStored = e.init?.stdin ?? ''
    if (e.argv.includes('ingest')) {
      seen.ingestMode = sent.mode
      seen.ingestContext = sent.context
      seen.ingestReply = sent.reply
    }

    return {
      value: {
        exitCode: 0,
        stdout: e.argv.includes('prepare')
          ? JSON.stringify({ coach: true, text, mode: isSpeech ? 'dictated' : 'written', strip: isSpeech, system: 'system', user: 'user' })
          : e.argv.includes('check')
            ? JSON.stringify({ escalate: seen.escalate, model: 'sonnet', note: ' NOTE-ABOUT-MISHEARING' })
            : e.argv.includes('ingest')
              ? JSON.stringify({ ...JSON.parse(ingested), icon: sent.mode === 'dictated' ? '\u{1F399}\uFE0E' : '\u270E' })
              : '',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  on('prompt.submit', async (_$, e) => {
    seen.entered = e.text

    return { text: e.text, origin: e.origin }
  })
  on('session.messages', async () => ({
    value: [
      { role: 'user' as const, content: 'earlier question' },
      { role: 'assistant' as const, content: [{ type: 'text' as const, text: 'The case speech-wording-is-taste checks dictated wording.' }] },
    ],
  }) as never)
  on('model.complete', async (_$, e) => {
    seen.models.push(e.model)
    seen.prompts.push(e.prompt)

    return { value: { isAnswered: true as const, text: `{"fixes":[],"model":"${e.model}"}`, usage: USAGE } }
  })
  on('ui.render', { component: 'UserMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>{e.props.text}</Text>
  })

  return { clock, seen }
}

const submit = ($: Engine, text: string) => $.prompt.submit({ text, wait: false, origin: { kind: 'composer' } })

const draw = ($: Engine, text: string) =>
  $.ui.mount({
    plugin: 'english-coach',
    surface: 'terminal',
    component: 'UserMessage',
    requestId: 'row:1',
    props: { text, origin: { kind: 'composer' }, isExpanded: false },
  })

// Moves the mock clock so the mod's timer fires, then redraws the row until `pattern` shows: the mounted drawing, or
// undefined when it never does. A caller unmounts the drawing it gets.
const drawnWith = async ($: Engine, clock: ReturnType<typeof mock.clock>, text: string, pattern: RegExp, tries = 40) => {
  for (let attempt = 0; attempt < tries; attempt++) {
    await clock.advance(10)
    const ui = await draw($, text)
    if ((await ui.find({ text: pattern })) !== undefined) return ui
    await ui.unmount()
  }

  return undefined
}

describe('inline fixes', () => {
  test('a reviewed prompt shows its fixes under it', async ($, on) => {
    const { clock } = world(on, INGESTED)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(ui).toBeDefined()
    expect(await ui!.find({ type: 'Text', text: /'Explain' takes 'to'/ })).toBeDefined()
    await ui!.unmount()
  })

  test('a careless prompt shows nothing extra', async ($, on) => {
    const { clock } = world(on, CARELESS)
    await submit($, PROMPT)

    expect(await drawnWith($, clock, PROMPT, /explain to me/, 15)).toBeUndefined()
  })

  test('another prompt is left as the engine drew it', async ($, on) => {
    const { clock } = world(on, INGESTED)
    await submit($, PROMPT)
    const reviewed = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(reviewed).toBeDefined()
    await reviewed!.unmount()

    const ui = await draw($, 'Something else entirely, with no review.')
    expect(await ui.find({ text: /explain to me/ })).toBeUndefined()
    await ui.unmount()
  })

  test('a prompt that is not the user\'s own is not reviewed', async ($, on) => {
    const { clock } = world(on, INGESTED)
    await $.prompt.submit({ text: PROMPT, wait: false, origin: { kind: 'task-notification' } } as never)

    expect(await drawnWith($, clock, PROMPT, /explain to me/, 15)).toBeUndefined()
  })

  test('a dictated prompt reaches the model without the marker and is reviewed as dictated', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    await submit($, PROMPT + MARKER)

    const ui = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(ui).toBeDefined()
    await ui!.unmount()
    expect(seen.entered).toBe(PROMPT)
    expect(seen.ingestMode).toBe('dictated')
  })

  test('a written prompt is passed on unchanged and reviewed as written', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(ui).toBeDefined()
    await ui!.unmount()
    expect(seen.entered).toBe(PROMPT)
    expect(seen.ingestMode).toBe('written')
  })

  test('fixes in dictated text carry a studio microphone, in typed text a pen', async ($, on) => {
    const spoken = world(on, INGESTED)
    await submit($, PROMPT + MARKER)
    const dictated = await drawnWith($, spoken.clock, PROMPT, /explain to me/)
    expect(dictated).toBeDefined()
    expect(await dictated!.find({ text: /\u{1F399}/u })).toBeDefined()
    expect(await dictated!.find({ text: /\u270E/ })).toBeUndefined()
    await dictated!.unmount()
  })

  test('fixes in typed text carry a pen', async ($, on) => {
    const typed = world(on, INGESTED)
    await submit($, PROMPT)
    const written = await drawnWith($, typed.clock, PROMPT, /explain to me/)
    expect(written).toBeDefined()
    expect(await written!.find({ text: /\u270E/ })).toBeDefined()
    expect(await written!.find({ text: /\u{1F399}/u })).toBeUndefined()
    await written!.unmount()
  })

  test('the assistant\'s recent text goes to the review as context, for the quote guard', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(ui).toBeDefined()
    await ui!.unmount()
    expect(seen.ingestContext).toContain('speech-wording-is-taste')
  })

  test('the recent chat goes to the prepare step, so names and identifiers in it can reach the reviewer', async ($, on) => {
    const { seen } = world(on, INGESTED)
    await submit($, PROMPT)

    expect(seen.prepareContext).toContain('speech-wording-is-taste')
  })

  test('a style item is drawn quietly and labelled as not an error', async ($, on) => {
    const styled = JSON.stringify({
      ok: true, careless: false, btw: null, hint: null,
      fixes: [{ category: 'phrasing', transfer: false, original: 'it rather makes sense', fix: 'it makes more sense', explanation: 'More natural.', severity: 'style' }],
    })
    const { clock } = world(on, styled)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /not an error/)
    expect(ui).toBeDefined()
    await ui!.unmount()
  })

  test('a mistake that keeps coming back carries a reminder line', async ($, on) => {
    const recurring = JSON.stringify({
      ok: true, careless: false, btw: null, hint: null,
      fixes: [{ category: 'word-choice', transfer: false, original: 'defer this', fix: 'derive this', explanation: 'derive = get from.', severity: 'error', reminder: 'This one keeps coming back: "derive" instead of "defer".' }],
    })
    const { clock } = world(on, recurring)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /keeps coming back/)
    expect(ui).toBeDefined()
    await ui!.unmount()
  })

  test('a phrase suggestion is drawn on its own when the message has no correction', async ($, on) => {
    const suggested = JSON.stringify({
      ok: true, careless: false, btw: null, hint: null, fixes: [],
      phrase: { id: 'cut-to-the-chase', phrase: 'cut to the chase', meaning: 'get to the main point', example: "Let's cut to the chase.", original: 'get to the point quickly' },
    })
    const { clock } = world(on, suggested)
    await submit($, PROMPT)

    const ui = await drawnWith($, clock, PROMPT, /cut to the chase/)
    expect(ui).toBeDefined()
    expect(await ui!.find({ text: /get to the point quickly/ })).toBeDefined()
    await ui!.unmount()
  })

  test('with the audit on, the larger model is asked in the background and only its verdicts are stored', async ($, on) => {
    const audited = JSON.stringify({
      ok: true, careless: false, btw: null, hint: null, phrase: null,
      fixes: [{ id: 7, category: 'preposition', transfer: true, original: 'explain me', fix: 'explain to me', explanation: 'Use to.', severity: 'error' }],
      audit: { model: 'sonnet', scope: 'flagged', prompt_id: 3, system: 'SECOND-OPINION-SYSTEM', user: '{"message":"the whole message"}' },
    })
    const { clock, seen } = world(on, audited)
    await submit($, PROMPT)
    expect(await drawnWith($, clock, PROMPT, /explain to me/)).toBeDefined()
    for (let tries = 0; tries < 20 && seen.auditStored === ''; tries++) await clock.advance(10)

    expect(seen.models).toEqual(['haiku', 'sonnet'])
    expect(seen.prompts[1]).toBe('{"message":"the whole message"}')
    const stored = JSON.parse(seen.auditStored)
    expect(stored.prompt_id).toBe(3)
    expect(stored.prompt).toBe(PROMPT)
    expect(stored.reply).toContain('"model":"sonnet"')
  })

  test('without an audit request nothing but the review is asked', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    await submit($, PROMPT)
    expect(await drawnWith($, clock, PROMPT, /explain to me/)).toBeDefined()
    await clock.advance(50)
    expect(seen.models).toEqual(['haiku'])
    expect(seen.auditStored).toBe('')
  })

  test('one review by default, and a second by the larger model only when the check asks for it', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    await submit($, PROMPT)
    expect(await drawnWith($, clock, PROMPT, /explain to me/)).toBeDefined()
    expect(seen.models).toEqual(['haiku'])
  })

  test('an unclear dictated passage gets a second review and its answer is the one stored', async ($, on) => {
    const { clock, seen } = world(on, INGESTED)
    seen.escalate = true
    await submit($, PROMPT + MARKER)

    const ui = await drawnWith($, clock, PROMPT, /explain to me/)
    expect(ui).toBeDefined()
    await ui!.unmount()
    expect(seen.models).toEqual(['haiku', 'sonnet'])
    expect(seen.ingestReply).toContain('"model":"sonnet"')
    expect(seen.prompts[1]).toContain('NOTE-ABOUT-MISHEARING')
    expect(seen.prompts[0]).not.toContain('NOTE-ABOUT-MISHEARING')
  })
})
