import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = 'english-coach-overview'

const stats = (mode: 'both' | 'written' | 'dictated', summarized = false, audited = false) => {
  const messages = { both: 4, written: 3, dictated: 1 }[mode]

  return {
    mode,
    since: '2026-10-10',
    messages,
    words: messages * 20,
    errors: mode === 'dictated' ? 1 : 2,
    slips: 2,
    styles: 1,
    clean_weekly: [null, null, null, null, null, null, 0.5, 0.75],
    clean_now: 0.75,
    categories: [
      {
        category: mode === 'dictated' ? 'word-choice' : 'preposition',
        total: 3,
        weekly: [null, null, null, null, null, null, 0.4, 0.2],
        trend: 'n/a',
        summary: summarized ? { headline: 'You choose the preposition by its German counterpart.', detail: 'Learn the set phrases.' } : null,
        summary_stale: false,
        examples: [{ original: 'explain me', fix: 'explain to me', explanation: 'Use to.', transfer: true, core: { wrong: 'explain me', right: 'explain to me' } }],
      },
    ],
    transfer: { native: 'German', weekly: [null, null, null, null, null, null, 0.4, 0.2], trend: 'n/a', total: 1, share: 0.5 },
    weeks_with_data: 2,
    summary_state: summarized ? 'ready' : 'none',
    audit: audited
      ? { scope: 'flagged', audited: 4, agree: 3, misses: 1, suspects: [{ id: 83, verdict: 'not-error', category: '', current: 'word-order', reason: 'Natural wording; the fix loses the meaning.', original: 'whether mostly the', fix: 'whether the', core: { wrong: 'whether mostly the', right: 'whether the' } }] }
      : { scope: 'off', audited: 0, agree: 0, suspects: [], misses: 0 },
    errors_needed: 0,
    focus: summarized ? ['Check the preposition after verbs of movement.'] : [],
    focus_stale: false,
    errors_per_100_words: mode === 'dictated' ? 1.8 : 0.8,
    rate_weekly: [null, null, null, null, null, null, 1.0, 0.8],
    rate_trend: 'n/a',
    sentences: messages * 3,
    error_free_sentences: 0.9,
    length_buckets: [{ label: '1-10 words', messages: 1, clean_share: 1, per_100_words: 0.5 }, { label: '61+ words', messages: 1, clean_share: 0.25, per_100_words: 1.2 }],
    watchlist: [{ wrong: 'defer', right: 'derive', hits: 2, streak: 1, status: 'active', note: 'derive = get from', manual: false }],
    slip_examples: [{ original: 'If have', fix: 'If I have', explanation: '' }],
    style_examples: [{ original: 'I start to like', fix: "I'm starting to like", explanation: '' }],
    messages_by_mode: { written: 3, dictated: 1 },
  }
}

const world = (on: On) => {
  const clock = mock.clock(on)
  const seen = { clock, due: [] as string[], summarized: false, audited: false, calls: [] as string[], models: [] as string[], prompts: [] as string[], stored: '' }
  on('model.complete', async (_$, e) => {
    seen.models.push(e.model)
    seen.prompts.push(e.prompt)

    return {
      value: {
        isAnswered: true as const,
        text: '{"categories":{"preposition":"You choose the preposition by its German counterpart."},"focus":["Check the preposition after verbs of movement."]}',
        usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      },
    }
  })
  on('process.run', async (_$, e) => {
    if (e.argv.includes('dismiss') || e.argv.includes('ack')) {
      seen.calls.push(e.argv.slice(e.argv.indexOf('dismiss') >= 0 ? e.argv.indexOf('dismiss') : e.argv.indexOf('audit')).join(' '))
      seen.audited = false
    }

    return {
    value: {
      exitCode: 0,
      stdout: e.argv.includes('overview')
        ? JSON.stringify(stats(e.argv[e.argv.length - 1] as 'both' | 'written' | 'dictated', seen.summarized, seen.audited))
        : e.argv.includes('summary-due')
          ? JSON.stringify({ modes: seen.due })
          : e.argv.includes('summary-input')
          ? JSON.stringify({ model: 'sonnet', system: 'SYSTEM', prompt: '{"preposition":[]}', categories: ['preposition'] })
          : e.argv.includes('summary-store')
            ? ((seen.summarized = true), (seen.stored = e.init?.stdin ?? ''), JSON.stringify({ ok: true, stored: 1, focus: 1 }))
            : '',
      stderr: '',
      isStdoutTruncated: false,
      isStderrTruncated: false,
    },
  }})
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))

  return seen
}

const mountPane = ($: Engine) =>
  $.ui.mount({
    plugin: 'english-coach',
    surface: 'terminal',
    component: 'Pane',
    requestId: PANE,
    props: { title: 'English coach', isFocused: true, bodyColumns: 72, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  })

const run = ($: Engine) =>
  $.command.run({ command: 'english-coach-overview', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })

describe('overview pane', () => {
  test('opens from its command and shows the headline, categories, watchlist and a button per mode', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /English coach/ })).toBeDefined()
    expect(await ui.find({ text: /0\.8/ })).toBeDefined()
    expect(await ui.find({ text: /90%/ })).toBeDefined()
    expect(await ui.find({ text: /preposition/ })).toBeDefined()
    expect(await ui.find({ text: /explain to me/ })).toBeDefined()
    expect(await ui.find({ text: /German pattern/ })).toBeDefined()
    expect(await ui.find({ text: /50% of your errors/ })).toBeDefined()
    expect(await ui.find({ text: /derive/ })).toBeDefined()
    for (const key of ['tab-both', 'tab-written', 'tab-dictated', 'refresh']) expect(await ui.find({ key })).toBeDefined()
    await ui.unmount()
  })

  test('the mode buttons switch the whole view between spoken and written', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /4 messages/ })).toBeDefined()
    await ui.press({ key: 'tab-dictated' })
    expect(await ui.find({ text: /1 messages/ })).toBeDefined()
    expect(await ui.find({ text: /word-choice/ })).toBeDefined()
    expect(await ui.find({ text: /preposition/ })).toBeUndefined()
    await ui.press({ key: 'tab-written' })
    expect(await ui.find({ text: /3 messages/ })).toBeDefined()
    expect(await ui.find({ text: /preposition/ })).toBeDefined()
    await ui.unmount()
  })

  test('slips and style items are collapsed until their button is pressed', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /If have/ })).toBeUndefined()
    await ui.press({ key: 'toggle-slips' })
    expect(await ui.find({ text: /If have/ })).toBeDefined()
    await ui.press({ key: 'toggle-slips' })
    expect(await ui.find({ text: /If have/ })).toBeUndefined()
    await ui.press({ key: 'toggle-style' })
    expect(await ui.find({ text: /starting to like/ })).toBeDefined()
    await ui.unmount()
  })

  test('the length section explains why the headline is per word and per sentence', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /Longer messages are clean less often/ })).toBeUndefined()
    await ui.press({ key: 'toggle-lengths' })
    expect(await ui.find({ text: /Longer messages are clean less often/ })).toBeDefined()
    expect(await ui.find({ text: /61\+ words/ })).toBeDefined()
    await ui.unmount()
  })

  test('the headline follows the selected mode', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    await ui.press({ key: 'tab-dictated' })
    expect(await ui.find({ text: /1\.8/ })).toBeDefined()
    await ui.press({ key: 'tab-written' })
    expect(await ui.find({ text: /0\.8/ })).toBeDefined()
    await ui.unmount()
  })

  test('the second opinion lists doubted tips with a button to dismiss or keep each', async ($, on) => {
    const seen = world(on)
    seen.audited = true
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /Second opinion/ })).toBeDefined()
    expect(await ui.find({ text: /75%/ })).toBeDefined()
    expect(await ui.find({ text: /the fix loses the meaning/ })).toBeDefined()
    await ui.press({ key: 'dismiss-83' })
    expect(seen.calls).toEqual(['dismiss 83 taste'])
    expect(await ui.find({ text: /the fix loses the meaning/ })).toBeUndefined()
    await ui.unmount()
  })

  test('the second opinion stays out of the pane while the audit is off', async ($, on) => {
    world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /Second opinion/ })).toBeUndefined()
    await ui.unmount()
  })

  test('the summarize button asks the larger model, stores its answer and shows the pattern and the focus', async ($, on) => {
    const seen = world(on)
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /German counterpart/ })).toBeUndefined()
    expect(await ui.find({ text: /What to work on/ })).toBeUndefined()
    await ui.press({ key: 'summarize' })
    expect(seen.models).toEqual(['sonnet'])
    expect(seen.prompts[0]).toBe('{"preposition":[]}')
    expect(seen.stored).toContain('German counterpart')
    expect(await ui.find({ text: /German counterpart/ })).toBeDefined()
    expect(await ui.find({ text: /What to work on/ })).toBeDefined()
    expect(await ui.find({ text: /after verbs of movement/ })).toBeDefined()
    expect(await ui.find({ text: /Learn the set phrases/ })).toBeUndefined()
    await ui.press({ key: 'detail-preposition' })
    expect(await ui.find({ text: /Learn the set phrases/ })).toBeDefined()
    expect(await ui.find({ text: /explain to me/ })).toBeUndefined()
    await ui.press({ key: 'examples-preposition' })
    expect(await ui.find({ text: /explain to me/ })).toBeDefined()
    await ui.unmount()
  })

  test('opening the overview prepares a missing report right away and shows it without a button press', async ($, on) => {
    const seen = world(on)
    seen.due = ['both', 'written', 'dictated']
    await run($)

    const ui = await mountPane($)
    expect(await ui.find({ text: /German counterpart/ })).toBeUndefined()
    for (let tries = 0; tries < 20 && seen.models.length === 0; tries++) await seen.clock.advance(10)
    expect(seen.models.length).toBe(3)
    expect(await ui.find({ text: /German counterpart/ })).toBeDefined()
    expect(await ui.find({ text: /Preparing the report/ })).toBeUndefined()
    await ui.unmount()
  })

  test('nothing is prepared when the backend says no mode is due', async ($, on) => {
    const seen = world(on)
    await run($)
    for (let tries = 0; tries < 5; tries++) await seen.clock.advance(10)
    expect(seen.models).toEqual([])
  })
})
