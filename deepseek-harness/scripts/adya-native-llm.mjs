/** Deterministic keyless LLM adapter for the ADYA native DSH prototype. */

import { LlmAdapter, ToolCallId } from '@deepseek-ai/dsh-llm'

let callNumber = 0

function toolCalls(messages) {
  return messages.flatMap(message => message.role === 'assistant'
    ? message.content.filter(block => block.type === 'tool-call').map(block => block.name)
    : [])
}

function latestToolText(messages) {
  const tool = messages.findLast(message => message.role === 'tool')
  if (tool === undefined) return ''
  return tool.content.filter(block => block.type === 'text').map(block => block.text).join('\n')
}

function userText(messages) {
  return messages.flatMap(message => message.role === 'user'
    ? message.content.filter(block => block.type === 'text').map(block => block.text)
    : []).join('\n')
}

function chunksForToolCalls(specs) {
  const chunks = []
  for (const [index, spec] of specs.entries()) {
    const id = ToolCallId(`adya-native-${++callNumber}`)
    const args = JSON.stringify(spec.args)
    chunks.push(
      { type: 'block-start', index, blockType: 'tool-call' },
      { type: 'tool-call-delta', index, id, name: spec.name, argumentsDelta: args },
      { type: 'block-end', index, block: { type: 'tool-call', id, name: spec.name, arguments: args } },
    )
  }
  chunks.push({ type: 'usage', usage: { inputTokens: 10, outputTokens: 5 } })
  chunks.push({ type: 'finish', reason: { kind: 'tool-calls' } })
  return chunks
}

function textChunks(text) {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'usage', usage: { inputTokens: 10, outputTokens: 3 } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

function rootResponse(messages) {
  const calls = toolCalls(messages)
  if (!calls.includes('adya_orchestrate')) {
    return chunksForToolCalls([{ name: 'adya_orchestrate', args: {} }])
  }
  return textChunks('ADYA native workflow completed; structured evidence was returned by adya_orchestrate.')
}

function childResponse(messages, role, goal) {
  const calls = toolCalls(messages)
  const previousToolText = latestToolText(messages)
  if (role === 'planner' && calls.length === 0) {
    return chunksForToolCalls([{
      name: 'adya_capability',
      args: { kind: 'skill', id: 'goal-decomposition', role, arguments: { goal, contract: {} } },
    }])
  }
  if (role === 'researcher' && calls.length === 0) {
    return chunksForToolCalls([{
      name: 'adya_capability',
      args: { kind: 'mcp', id: 'mcp-memory', capability: 'memory.search', role, arguments: { query: goal, limit: 5 } },
    }])
  }
  if (role === 'builder') {
    if (calls.length === 0) {
      return chunksForToolCalls([{
        name: 'adya_capability',
        args: { kind: 'tool', id: 'goal.echo', role, arguments: { value: goal } },
      }])
    }
    if (calls.length === 1) {
      return chunksForToolCalls([{
        name: 'adya_capability',
        args: { kind: 'skill', id: 'evidence-synthesis', role, arguments: { goal, evidence: { tool: previousToolText } } },
      }])
    }
  }
  if (role === 'reviewer' && calls.length === 0) {
    return chunksForToolCalls([{
      name: 'adya_capability',
      args: { kind: 'skill', id: 'risk-review', role, arguments: { contract: {}, evidence: {} } },
    }])
  }
  return textChunks(`${role} completed through the native DSH child runtime.`)
}

class AdyaNativeFixtureAdapter extends LlmAdapter {
  async *stream(options) {
    const toolNames = options.tools.map(tool => tool.name)
    const text = userText(options.messages)
    const root = toolNames.includes('adya_orchestrate') && !/You are the ADYA [a-z0-9_-]+ agent/i.test(text)
    if (root) {
      for (const chunk of rootResponse(options.messages)) {
        options.signal?.throwIfAborted()
        yield chunk
      }
      return
    }
    if (!toolNames.includes('adya_capability')) throw new Error('ADYA child is missing the native capability tool')
    const role = text.match(/You are the ADYA ([a-z0-9_-]+) agent/i)?.[1]?.toLowerCase()
    if (!role) throw new Error('ADYA native fixture could not identify the child role')
    const goal = text.match(/Goal: ([^\n]+)/)?.[1] ?? 'ADYA native demo'
    for (const chunk of childResponse(options.messages, role, goal)) {
      options.signal?.throwIfAborted()
      yield chunk
    }
  }
}

export const name = 'adya-native-llm'
export const inject = ['llm']

export function apply(ctx) {
  ctx.llm.registerAdapter(['deepseek-official'], new AdyaNativeFixtureAdapter())
}
