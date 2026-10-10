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
  const seen = { entered: '', ingestMode: '' }

  on('process.run', async (_$, e) => {
    const sent = JSON.parse(e.init?.stdin ?? '{}')
    const isSpeech = typeof sent.prompt === 'string' && sent.prompt.endsWith(MARKER)
    const text = isSpeech ? sent.prompt.slice(0, -MARKER.length) : sent.prompt
    if (e.argv.includes('ingest')) seen.ingestMode = sent.mode

    return {
      value: {
        exitCode: 0,
        stdout: e.argv.includes('prepare')
          ? JSON.stringify({ coach: true, text, mode: isSpeech ? 'dictated' : 'written', strip: isSpeech, system: 'system', user: 'user' })
          : e.argv.includes('ingest')
            ? ingested
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
  on('model.complete', async () => ({ value: { isAnswered: true as const, text: '{"fixes":[]}', usage: USAGE } }))
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
})
