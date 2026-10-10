import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { CategoryStats, Mode, Overview, OverviewState } from '../types'

export const PANE = 'english-coach-overview'
export const COMMAND = 'english-coach-overview'

const MODES: Mode[] = ['both', 'written', 'dictated']
const LABEL: Record<Mode, string> = { both: 'Both', written: 'Written', dictated: 'Spoken' }
const HOTKEY: Record<Mode, string> = { both: 'b', written: 'w', dictated: 's' }
const BARS = '▁▂▃▄▅▆▇█'

const tab = atom({ plugin: 'english-coach', key: 'tab' } as const, 'both' as Mode)
const overview = atom({ plugin: 'english-coach', key: 'overview' } as const, null as OverviewState)
const expanded = atom({ plugin: 'english-coach', key: 'expanded' } as const, [] as string[])
const summarizing = atom({ plugin: 'english-coach', key: 'summarizing' } as const, 0)

// Runs the Python backend and returns what it printed (declared here: the module analyzer only lets `$` reach functions of its own file).
const backend = async ($: EngineInterface, args: string[], stdin: unknown = {}) =>
  (
    await $.process.run(['python3', '-I', `${$.plugin.root}/scripts/english-coach.py`, ...args], {
      stdin: JSON.stringify(stdin),
      timeoutMs: 15_000,
    })
  ).stdout

const load = async ($: EngineInterface) => {
  const loadedAt = await $.clock.now()
  const entries = await Promise.all(MODES.map(async mode => [mode, JSON.parse(await backend($, ['overview', mode])) as Overview] as const))
  const data = Object.fromEntries(entries) as Record<Mode, Overview>
  await update($, overview, () => ({ loadedAt, data }))
}

// Asks the larger model for the general pattern behind each category's corrections, stores the answer, and redraws.
// `modes` are the views to write for; left out, the ones the backend says are due because the pane was just opened.
const summarize = async ($: EngineInterface, modes?: Mode[]) => {
  try {
    const wanted = modes ?? ((JSON.parse(await backend($, ['summary-due', '--open'])).modes ?? []) as Mode[])
    await update($, summarizing, () => wanted.length)
    // The views are written in parallel, and each one redraws the pane as soon as it is stored.
    await Promise.all(
      wanted.map(async mode => {
        try {
          const input = JSON.parse(await backend($, ['summary-input', mode]))
          if (input.categories.length > 0) {
            const reply = await $.model.complete({ model: input.model, system: input.system, prompt: input.prompt, maxTokens: 1500, timeoutMs: 180_000 })
            if (reply.isAnswered) await backend($, ['summary-store', mode], { reply: reply.text })
          }
          await load($)
        } catch (error) {
          $.ui.toast(`english-coach: summary failed (${String(error).slice(0, 80)})`)
        } finally {
          await update($, summarizing, n => Math.max(0, n - 1))
        }
      }),
    )
  } catch (error) {
    $.ui.toast(`english-coach: summary failed (${String(error).slice(0, 80)})`)
    await update($, summarizing, () => 0)
  }
}

const spark = (values: (number | null)[], scale: number) =>
  values
    .map(value => (value === null ? '·' : value === 0 ? BARS.charAt(0) : BARS.charAt(Math.min(7, 1 + Math.floor((value / scale) * 6.999)))))
    .join('')

const TREND = {
  down: { arrow: '↓', color: 'success' },
  up: { arrow: '↑', color: 'error' },
  flat: { arrow: '→', color: undefined },
  'n/a': { arrow: '', color: undefined },
} as const

const shareColor = (share: number) => (share >= 0.75 ? 'success' : share >= 0.5 ? 'warning' : 'error')
const percent = (part: number, whole: number) => (whole === 0 ? 0 : Math.round((100 * part) / whole))

export const registerOverview = (on: On) => {
  on('command.run', { command: COMMAND }, async $ => {
    await load($)
    await $.ui.open({ id: PANE, title: 'English coach', focus: true, closeOnEscape: true })
    $.clock.after(0, () => void summarize($)) // a missing or outdated report is prepared right away; the pane shows its status meanwhile

    return { text: 'English coach overview opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const state = await read($, overview)
    const selected = await read($, tab)
    const open = await read($, expanded)
    const busy = (await read($, summarizing)) > 0
    if (state === null) return <Text dimColor>Loading…</Text>

    const data = state.data[selected]
    const width = Math.max(30, e.props.bodyColumns - 2)
    const barWidth = Math.max(10, Math.min(24, width - 24))
    const rule = '─'.repeat(Math.min(width, 60))
    const sentenceFilled = data.error_free_sentences === null ? 0 : Math.round(data.error_free_sentences * barWidth)
    const showTrends = data.weeks_with_data >= 2
    const topTotal = Math.max(1, ...data.categories.map(c => c.total))
    const catBar = Math.max(8, Math.min(16, width - 40))
    const scale = Math.max(0.1, ...data.categories.flatMap(c => c.weekly.filter((v): v is number => v !== null)))
    const toggle = (id: string) => update($, expanded, list => (list.includes(id) ? list.filter(x => x !== id) : [...list, id]))
    const loaded = new Date(state.loadedAt)
    const counts: Record<Mode, number> = {
      both: data.messages_by_mode['written']! + data.messages_by_mode['dictated']!,
      written: data.messages_by_mode['written']!,
      dictated: data.messages_by_mode['dictated']!,
    }

    const Heading = ({ title, note }: { title: string; note?: string }) => (
      <Box flexDirection="column">
        <Text bold color="suggestion">
          {title}
        </Text>
        {note ? <Text dimColor>{note}</Text> : null}
      </Box>
    )

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="column">
          <Text>
            <Text bold color="suggestion">
              English coach
            </Text>
            <Text dimColor>{data.since ? `   since ${data.since} · ${data.messages} messages · ${data.words} words` : ''}</Text>
          </Text>
          <Text dimColor>{rule}</Text>
        </Box>

        <Box columnGap={2}>
          {MODES.map(mode => (
            <Button
              key={`tab-${mode}`}
              plain
              label={`${mode === selected ? '●' : '○'} ${LABEL[mode]} (${counts[mode]})`}
              hotkey={HOTKEY[mode]}
              dimColor={mode !== selected}
              onPress={() => update($, tab, () => mode)}
            />
          ))}
        </Box>

        <Box flexDirection="column">
          <Heading title="Accuracy" note="measured per word and per sentence, so message length does not skew it" />
          {data.errors_per_100_words === null ? (
            <Text dimColor>No messages in this view yet.</Text>
          ) : (
            <Text>
              <Text bold>{data.errors_per_100_words.toFixed(1)}</Text>
              <Text dimColor>{' errors per 100 words'}</Text>
              {showTrends ? <Text color={TREND[data.rate_trend].color}>{`  ${TREND[data.rate_trend].arrow}`}</Text> : null}
              {showTrends ? <Text dimColor>{`   ${spark(data.rate_weekly, Math.max(0.1, ...data.rate_weekly.filter((v): v is number => v !== null)))}`}</Text> : null}
            </Text>
          )}
          {data.error_free_sentences === null ? null : (
            <Text>
              <Text color={shareColor(data.error_free_sentences)}>{'█'.repeat(sentenceFilled)}</Text>
              <Text dimColor>{'░'.repeat(barWidth - sentenceFilled)}</Text>
              <Text bold>{`  ${Math.round(data.error_free_sentences * 100)}%`}</Text>
              <Text dimColor>{`  of ${data.sentences} sentences without an error`}</Text>
            </Text>
          )}
          <Text>
            <Text color="error">{`${data.errors} errors`}</Text>
            <Text dimColor>{'   '}</Text>
            <Text color="warning">{`${data.slips} slips`}</Text>
            <Text dimColor>{'   '}</Text>
            <Text color="suggestion">{`${data.styles} style items`}</Text>
          </Text>
          <Button
            key="toggle-lengths"
            plain
            dimColor
            label={`${open.includes('lengths') ? '▾' : '▸'} Clean messages by length`}
            onPress={() => toggle('lengths')}
          />
          {open.includes('lengths') ? (
            <Box flexDirection="column">
              {data.length_buckets.map(bucket => (
                <Text key={`len-${bucket.label}`} dimColor>
                  {`    ${bucket.label.padEnd(12)} ${String(Math.round(bucket.clean_share * 100)).padStart(3)}% clean   ${bucket.per_100_words === null ? '-' : bucket.per_100_words.toFixed(1)} per 100 words   (${bucket.messages} messages)`}
                </Text>
              ))}
              <Text dimColor italic wrap="wrap">{'    Longer messages are clean less often, simply because they offer more places for a slip. That is why the numbers above are per word and per sentence.'}</Text>
            </Box>
          ) : null}
        </Box>

        {busy && (
          <Text color="suggestion">{`✦ ${data.summary_state === 'none' ? 'Preparing' : 'Renewing'} the report… 15 to 30 seconds; the overview fills in by itself.`}</Text>
        )}
        {!busy && data.summary_state === 'stale' && (
          <Text dimColor>{'The report below predates your latest corrections. It renews in the background, or press p.'}</Text>
        )}
        {!busy && data.summary_state === 'none' && (
          <Text dimColor>{'No report yet. Press p to write one.'}</Text>
        )}
        {!busy && data.summary_state === 'not_enough' && (
          <Text dimColor>{`A written report of your patterns appears after 10 corrections (${data.errors_needed} more).`}</Text>
        )}

        {data.focus.length > 0 && (
          <Box flexDirection="column">
            <Heading title="What to work on" />
            {data.focus.map((line, index) => (
              <Text key={`focus-${index}`} wrap="wrap">{`${index + 1}. ${line}`}</Text>
            ))}
          </Box>
        )}

        {data.categories.length > 0 && (
          <Box flexDirection="column">
            <Heading title="Where your errors are" note={showTrends ? 'bar = share of your errors; strip = last 8 weeks, ↓ is improving' : 'bar = share of your errors'} />
            <Box flexDirection="column" gap={1}>
            {data.categories.map((cat: CategoryStats) => {
              const bar = Math.max(1, Math.round((catBar * cat.total) / topTotal))

              return (
                <Box key={`cat-${cat.category}`} flexDirection="column">
                  <Box columnGap={2}>
                  <Text>
                    <Text bold>{cat.category.padEnd(14)}</Text>
                    <Text color="permission">{'█'.repeat(bar)}</Text>
                    <Text dimColor>{'░'.repeat(catBar - bar)}</Text>
                    <Text>{`  ${String(cat.total).padStart(3)}`}</Text>
                    <Text dimColor>{`  ${percent(cat.total, data.errors)}%`}</Text>
                    {showTrends ? <Text color={TREND[cat.trend].color}>{`  ${spark(cat.weekly, scale)} ${TREND[cat.trend].arrow}`}</Text> : null}
                  </Text>
                  {cat.summary?.detail ? (
                    <Button
                      key={`detail-${cat.category}`}
                      plain
                      dimColor
                      label={open.includes(`detail-${cat.category}`) ? '▾ details' : '▸ details'}
                      onPress={() => toggle(`detail-${cat.category}`)}
                    />
                  ) : null}
                  {cat.examples.length > (cat.summary ? 0 : 1) ? (
                    <Button
                      key={`examples-${cat.category}`}
                      plain
                      dimColor
                      label={open.includes(`examples-${cat.category}`) ? '▾ examples' : '▸ examples'}
                      onPress={() => toggle(`examples-${cat.category}`)}
                    />
                  ) : null}
                  </Box>
                  {cat.summary ? (
                    <Text wrap="wrap">{`  ${cat.summary.headline}`}</Text>
                  ) : null}
                  {cat.summary && open.includes(`detail-${cat.category}`) && cat.summary.detail ? (
                    <Text dimColor wrap="wrap">{`  ${cat.summary.detail}`}</Text>
                  ) : null}
                  {cat.examples.slice(0, open.includes(`examples-${cat.category}`) ? 3 : cat.summary ? 0 : 1).map((example, index) => (
                    <Text key={`ex-${cat.category}-${index}`} wrap="wrap">
                      <Text dimColor>{'  '}</Text>
                      <Text color="error">{example.core?.wrong ?? example.original}</Text>
                      <Text dimColor>{' → '}</Text>
                      <Text color="success">{example.core?.right ?? example.fix}</Text>
                      {example.transfer ? <Text dimColor italic>{`   ${data.transfer.native ?? ''} pattern`}</Text> : null}
                    </Text>
                  ))}
                </Box>
              )
            })}
            </Box>
            {data.transfer.native !== null && data.transfer.share !== null && (
              <Text>
                <Text color="suggestion" bold>{`${data.transfer.native} patterns  `}</Text>
                <Text>{`${Math.round(data.transfer.share * 100)}% of your errors`}</Text>
                <Text dimColor>{`  (${data.transfer.total} of ${data.errors})`}</Text>
              </Text>
            )}
          </Box>
        )}

        {data.watchlist.length > 0 && (
          <Box flexDirection="column">
            <Heading title="Watchlist" note="mistakes that keep coming back" />
            {data.watchlist.map(entry => (
              <Box key={`watch-${entry.wrong}-${entry.right}`} borderStyle="round" borderColor="subtle" paddingX={1} flexDirection="column">
                <Text>
                  <Text color="error">{entry.wrong}</Text>
                  <Text dimColor>{' → '}</Text>
                  <Text color="success" bold>
                    {entry.right}
                  </Text>
                </Text>
                <Text color={entry.status === 'resolved' || entry.streak > 0 ? 'success' : 'warning'}>
                  {`${entry.hits}× flagged · ${entry.status === 'resolved' ? 'resolved' : entry.streak > 0 ? `improving: ${entry.streak} right in a row` : 'active'}${entry.manual ? ' · added by you' : ''}`}
                </Text>
                {entry.note ? (
                  <Text dimColor italic wrap="wrap">
                    {entry.note}
                  </Text>
                ) : null}
              </Box>
            ))}
          </Box>
        )}

        {(data.slips > 0 || data.styles > 0) && (
          <Box flexDirection="column">
            {data.slips > 0 && (
              <Button
                key="toggle-slips"
                plain
                dimColor
                label={`${open.includes('slips') ? '▾' : '▸'} Slips (${data.slips}): forgotten or misrecognized small words`}
                onPress={() => toggle('slips')}
              />
            )}
            {open.includes('slips')
              ? data.slip_examples.map((example, index) => (
                  <Text key={`slip-${index}`} dimColor wrap="wrap">{`    ${example.core?.wrong ?? example.original} → ${example.core?.right ?? example.fix}`}</Text>
                ))
              : null}
            {data.styles > 0 && (
              <Button
                key="toggle-style"
                plain
                dimColor
                label={`${open.includes('style') ? '▾' : '▸'} Native polish (${data.styles}): correct, but a native would say it differently`}
                onPress={() => toggle('style')}
              />
            )}
            {open.includes('style')
              ? data.style_examples.map((example, index) => (
                  <Text key={`style-${index}`} color="suggestion" wrap="wrap">{`    ${example.core?.wrong ?? example.original} → ${example.core?.right ?? example.fix}`}</Text>
                ))
              : null}
          </Box>
        )}

        <Box columnGap={2}>
          <Button key="refresh" plain dimColor label="↻ Refresh" hotkey="r" onPress={() => load($)} />
          <Button
            key="summarize"
            plain
            dimColor={busy}
            label={busy ? '✦ Summarizing…' : data.focus.length > 0 && !data.focus_stale ? '✦ Summarize again' : '✦ Summarize patterns'}
            hotkey="p"
            onPress={() => (busy ? undefined : summarize($, [selected]))}
          />
          <Text dimColor>{`updated ${String(loaded.getHours()).padStart(2, '0')}:${String(loaded.getMinutes()).padStart(2, '0')}`}</Text>
        </Box>
      </Box>
    )
  })
}
