/**
 * Run one ADYA request through the native DSH SDK in a separate process.
 * The caller receives structured DSH session events and the ADYA tool result;
 * no ADYA HTTP bridge or second agent executor is involved.
 */

import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(SCRIPT_DIR, '..', '..')
const DSH_ROOT = resolve(SCRIPT_DIR, '..')

async function stdinText() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

function contentText(message) {
  return (message?.content ?? [])
    .filter(block => block.type === 'text')
    .map(block => block.text)
    .join('')
}

function parseLeadingJsonObject(text) {
  const start = text.indexOf('{')
  if (start < 0) return undefined
  let depth = 0
  let quoted = false
  let escaped = false
  for (let index = start; index < text.length; index += 1) {
    const character = text[index]
    if (quoted) {
      if (escaped) escaped = false
      else if (character === '\\') escaped = true
      else if (character === '"') quoted = false
      continue
    }
    if (character === '"') {
      quoted = true
      continue
    }
    if (character === '{') depth += 1
    else if (character === '}') {
      depth -= 1
      if (depth === 0) {
        try {
          return JSON.parse(text.slice(start, index + 1))
        } catch {
          return undefined
        }
      }
    }
  }
  return undefined
}

function extractAdyaResult(events) {
  const calls = new Map()
  for (const event of events) {
    if (event.type === 'tool/call' && event.data.name === 'adya_orchestrate') {
      calls.set(event.data.callId, true)
    }
    if (event.type !== 'tool/result') continue
    const message = event.data.message
    if (!calls.has(message?.toolCallId)) continue
    const text = contentText(message)
    const prefix = 'ADYA_NATIVE_RESULT_JSON:'
    if (!text.startsWith('{')) {
      const index = text.indexOf(prefix)
      if (index >= 0) {
        return parseLeadingJsonObject(text.slice(index + prefix.length).trim())
      }
    }
    return parseLeadingJsonObject(text)
  }
  return undefined
}

function parseFinalEnvelope(finalResponse) {
  const prefix = 'ADYA_NATIVE_RESULT_JSON:'
  const index = finalResponse.indexOf(prefix)
  if (index < 0) return undefined
  return parseLeadingJsonObject(finalResponse.slice(index + prefix.length).trim())
}

async function main() {
  const input = JSON.parse(await stdinText())
  const pluginPath = resolve(input.plugin_path ?? resolve(PROJECT_ROOT, 'adya-share', 'adya_native_plugin.mjs'))
  const fixturePath = resolve(input.fixture_path ?? resolve(DSH_ROOT, 'scripts', 'adya-native-llm.mjs'))
  const work = await mkdtemp(resolve(tmpdir(), 'adya-native-dsh-'))
  const patchPath = resolve(work, 'adya-native.patch.json')
  const patch = [
    { id: 'llm-deepseek', disabled: true },
    { id: 'tool-bash', disabled: true },
    { id: 'tool-pwsh', disabled: true },
    { id: 'tool-fs', disabled: true },
    { id: 'tool-fs-search', disabled: true },
    { id: 'tool-subagent', disabled: true },
    { id: 'tool-subagent-fork', disabled: true },
    { id: 'tool-workflow', disabled: true },
    { id: 'tool-skill', disabled: true },
    {
      insert: [
        { id: 'adya-native-llm', name: pathToFileURL(fixturePath).href },
        {
          id: 'adya-native-plugin',
          name: pathToFileURL(pluginPath).href,
          config: {
            goal: String(input.goal ?? ''),
            contract: input.contract ?? {},
            roles: input.roles ?? [],
            memory_hits: input.memory_hits ?? [],
            run_id: String(input.run_id ?? 'run'),
            user_id: String(input.user_id ?? 'anonymous'),
          },
        },
      ],
    },
  ]
  await writeFile(patchPath, JSON.stringify(patch), 'utf8')

  const harness = new DeepSeekHarness({
    cwd: input.cwd ?? PROJECT_ROOT,
    processCwd: DSH_ROOT,
    provider: 'deepseek-official',
    model: input.model ?? 'deepseek-v4-flash',
    profile: 'sdk',
    patches: [patchPath],
    dshHome: input.dsh_home,
    initializeTimeoutMs: Number(input.initialize_timeout_ms ?? 60000),
    requestTimeoutMs: Number(input.timeout_ms ?? 120000),
    shutdownTimeoutMs: 3000,
  })
  try {
    const prompt = [
      'You are the root DSH agent for an ADYA native run.',
      'Call adya_orchestrate exactly once before answering.',
      'After it returns, output its JSON result unchanged with the prefix ADYA_NATIVE_RESULT_JSON:.',
      `Goal: ${String(input.goal ?? '')}`,
      `Approved roles: ${JSON.stringify(input.roles ?? [])}`,
      `Contract: ${JSON.stringify(input.contract ?? {})}`,
    ].join('\n')
    const result = await harness.run(prompt, { sessionId: String(input.session_id ?? `adya-${Date.now()}`) })
    const adyaResult = extractAdyaResult(result.events) ?? parseFinalEnvelope(result.finalResponse)
    if (adyaResult === undefined) {
      const eventTypes = result.events.map(event => event.type).slice(-20).join(', ')
      const toolResults = result.events
        .filter(event => event.type === 'tool/result')
        .map(event => {
          const text = contentText(event.data.message)
          return { length: text.length, head: text.slice(0, 120), tail: text.slice(-120) }
        })
      throw new Error(`native DSH run completed without an ADYA result envelope; final_length=${result.finalResponse.length}; final_tail=${result.finalResponse.slice(-200)}; tool_results=${JSON.stringify(toolResults)}; events=${eventTypes}`)
    }
    process.stdout.write(JSON.stringify({
      ok: true,
      session_id: result.sessionId,
      final_response: result.finalResponse,
      events: result.events,
      adya_result: adyaResult,
    }))
  } finally {
    await harness.close()
    await rm(work, { recursive: true, force: true })
  }
}

main().catch(error => {
  process.stderr.write(`${error?.stack ?? error}\n`)
  process.stdout.write(JSON.stringify({ ok: false, error: String(error?.message ?? error) }))
  process.exitCode = 1
})
