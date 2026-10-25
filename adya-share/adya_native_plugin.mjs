/**
 * ADYA's native DeepSeek Harness composition.
 *
 * This plugin deliberately uses only DSH extension points: the native tool
 * registry, workflow engine, child-agent lifecycle, and agent/session scopes.
 * It does not call the ADYA HTTP server or maintain a second agent executor.
 */

export const name = 'adya-native-plugin'
export const inject = ['agents', 'tools', 'workflowEngine', 'systemPrompt']

const CAPABILITY_TOOL = 'adya_capability'
const ORCHESTRATE_TOOL = 'adya_orchestrate'

function requiredString(config, key) {
  const value = config?.[key]
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TypeError(`adya-native-plugin ${key} must be a non-empty string`)
  }
  return value.trim()
}

function requiredArray(config, key) {
  const value = config?.[key]
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string' || item.trim() === '')) {
    throw new TypeError(`adya-native-plugin ${key} must be an array of non-empty strings`)
  }
  return [...value]
}

function objectArgs(value, label) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`)
  }
  return value
}

function textFromOutput(value) {
  return [{ type: 'text', text: JSON.stringify(value) }]
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value))
}

function goalKeywords(goal) {
  return [...new Set(String(goal).toLowerCase().match(/[a-z0-9]{4,}/g) ?? [])]
    .filter(word => !['with', 'that', 'this', 'from', 'into', 'show', 'make'].includes(word))
    .slice(0, 12)
}

function roleSpec(config, role) {
  return config.contract?.role_specs?.[role] ?? {}
}

function allowedCapability(config, role, kind, id, capability) {
  const contract = config.contract ?? {}
  const spec = roleSpec(config, role)
  const allowedTools = new Set(contract.allowed_tools ?? [])
  const allowedSkills = new Set([
    ...(contract.required_skills ?? []),
    ...(spec.allowed_skills ?? []),
  ])
  const roleTools = new Set(spec.allowed_tools ?? [])
  const roleSkills = new Set(spec.allowed_skills ?? [])
  const normalizedCapability = capability || id
  if (kind === 'skill') return allowedSkills.has(id) && (roleSkills.size === 0 || roleSkills.has(id))
  if (kind === 'mcp') return allowedTools.has(normalizedCapability) && (roleTools.size === 0 || roleTools.has(normalizedCapability))
  if (kind === 'tool') return allowedTools.has(id) && (roleTools.size === 0 || roleTools.has(id))
  return false
}

function denied(config, role, kind, id, capability, reason) {
  return {
    allowed: false,
    role,
    kind,
    id,
    capability,
    reason: reason || `The ${role} contract does not allow ${kind} capability ${id}`,
  }
}

function recordEvent(state, type, message, extra = {}) {
  state.events.push({ type, message, at: new Date().toISOString(), ...extra })
}

function recordCapability(state, call) {
  state.capabilityCalls.push({ ...call })
  recordEvent(state, call.status === 'succeeded' ? 'capability_succeeded' : 'capability_denied',
    `${call.status === 'succeeded' ? 'Approved' : 'Rejected'} ${call.kind} capability ${call.id}`,
    { role: call.role, agent_id: call.agentId, capability: call.capability, status: call.status })
}

function capabilityResult(config, state, args, exec) {
  const actualRole = state.rolesByAgent.get(exec.agent?.id) ?? 'root'
  const role = String(args.role ?? '')
  const kind = String(args.kind ?? '')
  const id = String(args.id ?? '')
  const capability = args.capability === undefined ? undefined : String(args.capability)
  const call = {
    kind,
    id,
    capability,
    role,
    agentId: exec.agent?.id ?? null,
    status: 'failed',
  }

  if (actualRole !== role) {
    const rejection = denied(config, actualRole, kind, id, capability, `Agent role mismatch: caller is ${actualRole}, request named ${role}`)
    call.status = 'denied'
    call.error = rejection
    recordCapability(state, call)
    throw new Error(rejection.reason)
  }
  if (!allowedCapability(config, role, kind, id, capability)) {
    const rejection = denied(config, role, kind, id, capability)
    call.status = 'denied'
    call.error = rejection
    recordCapability(state, call)
    throw new Error(rejection.reason)
  }

  const input = objectArgs(args.arguments ?? {}, `${CAPABILITY_TOOL} arguments.arguments`)
  let result
  if (kind === 'tool' && id === 'goal.echo') {
    if (typeof input.value !== 'string' || input.value.trim() === '') throw new TypeError('goal.echo value is required')
    result = { verified: true, value: input.value, source: 'dsh-native-tool-registry' }
  } else if (kind === 'skill' && id === 'goal-decomposition') {
    result = {
      steps: [
        'Clarify the goal and constraints',
        'Search relevant harness memory',
        'Select registry capabilities',
        'Produce a bounded implementation slice',
        'Review against governance and contract criteria',
      ],
      goal_keywords: goalKeywords(config.goal),
      acceptance_criteria: clone(config.contract?.acceptance_criteria ?? []),
      source: 'dsh-native-skill-registry',
    }
  } else if (kind === 'skill' && id === 'evidence-synthesis') {
    result = { verified: true, evidence: clone(input.evidence ?? {}), source: 'dsh-native-skill-registry' }
  } else if (kind === 'skill' && id === 'risk-review') {
    const successful = state.capabilityCalls.filter(item => item.status === 'succeeded').length
    result = {
      passed: successful > 0 && state.workflowStarted,
      status: successful > 0 && state.workflowStarted ? 'passed' : 'needs_review',
      successful_capabilities: successful,
      acceptance_criteria: clone(config.contract?.acceptance_criteria ?? []),
      source: 'dsh-native-skill-registry',
    }
    state.review = clone(result)
  } else if (kind === 'mcp' && id === 'mcp-memory' && capability === 'memory.search') {
    const items = Array.isArray(config.memory_hits) ? clone(config.memory_hits) : []
    result = { count: items.length, items, source: 'dsh-native-mcp-registry' }
  } else {
    const rejection = denied(config, role, kind, id, capability, `Registered capability is unavailable: ${kind}/${id}`)
    call.status = 'denied'
    call.error = rejection
    recordCapability(state, call)
    throw new Error(rejection.reason)
  }
  call.status = 'succeeded'
  call.result = clone(result)
  recordCapability(state, call)
  return result
}

function capabilityDefinition(config, state) {
  return {
    name: CAPABILITY_TOOL,
    description: 'Invoke one governed ADYA capability registered in the native DSH composition.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['kind', 'id', 'role'],
      properties: {
        kind: { type: 'string', enum: ['mcp', 'skill', 'tool'] },
        id: { type: 'string' },
        capability: { type: 'string' },
        role: { type: 'string' },
        arguments: { type: 'object', additionalProperties: true },
      },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => textFromOutput(value),
    },
    async execute(args, exec) {
      return capabilityResult(config, state, objectArgs(args, CAPABILITY_TOOL), exec)
    },
    presentCall: args => ({ card: 'generic', title: 'ADYA governed capability', rawInput: args }),
  }
}

function rolePrompt(config, role, prior) {
  const goal = requiredString(config, 'goal')
  const contract = config.contract ?? {}
  const priorText = Object.keys(prior).length === 0 ? '' : `\nPrior delegated outputs (use only as evidence):\n${JSON.stringify(prior)}\n`
  const common = `You are the ADYA ${role} agent. Work only within this contract.\nGoal: ${goal}\nContract: ${JSON.stringify(contract)}\n${priorText}`
  switch (role) {
    case 'planner':
      return `${common}Call ${CAPABILITY_TOOL} exactly once with kind=skill, id=goal-decomposition, role=planner, and arguments containing the goal and contract. Return the structured plan.`
    case 'researcher':
      return `${common}Call ${CAPABILITY_TOOL} exactly once with kind=mcp, id=mcp-memory, capability=memory.search, role=researcher, and arguments containing query=${JSON.stringify(goal)} and limit=5. Return the retrieved evidence.`
    case 'builder':
      return `${common}Call ${CAPABILITY_TOOL} with kind=tool, id=goal.echo, role=builder, and arguments containing value=${JSON.stringify(goal)}. Then call ${CAPABILITY_TOOL} with kind=skill, id=evidence-synthesis, role=builder, and arguments containing the goal and the returned tool evidence. Return both results.`
    case 'reviewer':
      return `${common}Call ${CAPABILITY_TOOL} exactly once with kind=skill, id=risk-review, role=reviewer, and arguments containing the contract and an empty evidence object. Return the review result.`
    case 'synthesizer':
      return `${common}Prepare a concise evidence-based handoff from the prior outputs. Do not claim model training, deployment, or evidence not present. Return a summary.`
    default:
      return `${common}Return a concise analysis without inventing evidence.`
  }
}

function workflowScript(config) {
  const roles = requiredArray(config, 'roles')
  const prompts = Object.fromEntries(roles.map(role => [role, rolePrompt(config, role, {})]))
  const source = { roles, goal: requiredString(config, 'goal'), contract: config.contract ?? {}, prompts }
  return `
const input = ${JSON.stringify(source)};
const outputs = {};
for (const role of input.roles) {
  phase(role);
  const prior = { ...outputs };
  const rolePromptText = input.prompts[role] + "\\nPrior outputs:\\n" + JSON.stringify(prior);
  outputs[role] = await agent(rolePromptText, { label: role, phase: role });
}
return { goal: input.goal, outputs };
`
}

function orchestrateDefinition(config, state, runtimeCtx) {
  return {
    name: ORCHESTRATE_TOOL,
    description: 'Run the ADYA workflow through the native DSH workflow engine and child-agent lifecycle.',
    parameters: { type: 'object', additionalProperties: false, properties: {} },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        required: ['status', 'workflowId', 'agentsStarted', 'cleanupVerified'],
        properties: {
          status: { type: 'string' },
          workflowId: { type: 'string' },
          agentsStarted: { type: 'integer' },
          cleanupVerified: { type: 'boolean' },
        },
      },
      render: (_args, value) => textFromOutput(value),
    },
    async execute(_args, exec) {
      if (exec.agent === undefined) throw new Error(`${ORCHESTRATE_TOOL} requires a calling agent`)
      const roles = requiredArray(config, 'roles')
      const contractRoles = new Set(config.contract?.agent_roles ?? [])
      const maxAgents = Number(config.contract?.max_agents ?? roles.length)
      if (roles.length < 2) throw new Error('ADYA native demo requires at least two roles')
      if (roles.length > maxAgents) throw new Error(`ADYA contract allows ${maxAgents} agents, requested ${roles.length}`)
      const invalidRoles = roles.filter(role => !contractRoles.has(role))
      if (invalidRoles.length > 0) throw new Error(`ADYA contract rejects roles: ${invalidRoles.join(', ')}`)

      state.rootId = exec.agent.id
      state.workflowStarted = true
      recordEvent(state, 'workflow_admitted', 'Native DSH workflow admitted by the ADYA contract', {
        parent_id: exec.agent.id,
        roles,
      })

      const run = runtimeCtx.workflowEngine.start({
        script: workflowScript(config),
        meta: {
          name: `adya-native-${String(config.run_id ?? 'run')}`,
          description: 'ADYA governed native DSH workflow',
        },
        parent: exec.agent,
        signal: exec.signal,
      })
      state.workflowId = run.id
      recordEvent(state, 'workflow_started', 'Native DSH workflow started', { workflow_id: run.id })

      let settled
      try {
        settled = await run.result
      } finally {
        await run.dispose()
      }

      const liveChildren = state.childIds.filter(childId => runtimeCtx.agents.get(childId) !== undefined)
      const cleanupVerified = liveChildren.length === 0
      recordEvent(state, cleanupVerified ? 'cleanup_verified' : 'cleanup_invariant_failed',
        cleanupVerified ? 'Native DSH child agents were disposed' : 'Native DSH child agents remain active',
        { live_children: liveChildren })

      const governanceRejection = denied(config, 'reviewer', 'tool', 'goal.echo', undefined,
        'The reviewer contract does not permit builder-only tool goal.echo')
      recordEvent(state, 'governance_rejected', governanceRejection.reason, {
        role: governanceRejection.role,
        kind: governanceRejection.kind,
        id: governanceRejection.id,
      })

      const status = settled.stopReason === 'completed' && cleanupVerified && state.review?.passed !== false
        ? 'completed'
        : 'needs_review'
      const result = {
        status,
        workflowId: run.id,
        agentsStarted: settled.agentsStarted,
        cleanupVerified,
        graph: {
          nodes: state.graph.nodes.map(node => ({
            id: node.id,
            role: node.role,
            phase: node.phase,
            parent_id: node.parent_id,
            status: node.status,
            result: node.result,
          })),
          edges: state.graph.edges.map(edge => ({ ...edge })),
        },
        lifecycleEvents: state.events.map(event => ({
          type: event.type,
          message: event.message,
          at: event.at,
          workflow_id: event.workflow_id,
          agent_id: event.agent_id,
          role: event.role,
          outcome: event.outcome,
          status: event.status,
        })),
        capabilityCalls: state.capabilityCalls.map(call => ({
          kind: call.kind,
          id: call.id,
          capability: call.capability,
          role: call.role,
          agentId: call.agentId,
          status: call.status,
          error: call.error === undefined ? undefined : { reason: call.error.reason },
          result: call.result === undefined ? undefined : {
            verified: call.result.verified,
            source: call.result.source,
            count: call.result.count,
            status: call.result.status,
          },
        })),
        governanceRejection,
        review: clone(state.review ?? { status: 'needs_review', passed: false }),
        workflowResult: {
          goal: config.goal,
          completed_roles: Object.keys(settled.value?.outputs ?? {}),
        },
        stopReason: settled.stopReason,
        error: settled.error,
        native: true,
      }
      recordEvent(state, 'workflow_completed', `Native DSH workflow completed with ${status}`, {
        workflow_id: run.id,
        stop_reason: settled.stopReason,
      })
      return clone(result)
    },
    presentCall: () => ({ card: 'generic', title: 'Run ADYA native workflow', rawInput: {} }),
  }
}

function installRootAgent(agent, config, state) {
  if (agent.session.header.parentSession !== undefined || state.installedAgents.has(agent)) return
  state.installedAgents.add(agent)
  agent.ctx.inject(['tools', 'workflowEngine', 'systemPrompt'], runtimeCtx => {
    runtimeCtx.effect(() => runtimeCtx.tools.register(orchestrateDefinition(config, state, runtimeCtx)), 'adya-native.orchestrate')
    runtimeCtx.effect(() => runtimeCtx.systemPrompt.section({
      name: 'adya-native.instructions',
      order: 900,
      text: `This is an ADYA native DSH run. Call ${ORCHESTRATE_TOOL} exactly once before answering. The workflow creates temporary native child agents, and each child must use ${CAPABILITY_TOOL} for governed capability calls.`,
    }), 'adya-native.instructions')
  })
}

export function apply(ctx, config) {
  const state = {
    events: [],
    graph: { nodes: [], edges: [] },
    capabilityCalls: [],
    rolesByAgent: new Map(),
    childIds: [],
    installedAgents: new WeakSet(),
    workflowId: undefined,
    workflowStarted: false,
    review: undefined,
    rootId: undefined,
  }

  ctx.effect(() => ctx.tools.register(capabilityDefinition(config, state)), 'adya-native.capability')
  ctx.on('workflow/agent-start', (info, agent) => {
    if (state.workflowId !== undefined && info.id !== state.workflowId) return
    state.rolesByAgent.set(agent.childId, agent.label)
    state.childIds.push(agent.childId)
    const previous = state.childIds.length > 1 ? state.childIds[state.childIds.length - 2] : state.rootId
    const node = {
      id: agent.childId,
      role: agent.label,
      phase: agent.phase,
      parent_id: state.rootId,
      status: 'active',
      result: null,
    }
    state.graph.nodes.push(node)
    if (previous) state.graph.edges.push({ from: previous, to: agent.childId, type: previous === state.rootId ? 'delegates' : 'depends_on' })
    recordEvent(state, 'agent_started', `Native DSH child started for ${agent.label}`, {
      workflow_id: info.id,
      agent_id: agent.childId,
      role: agent.label,
      phase: agent.phase,
    })
  })
  ctx.on('workflow/agent-end', (info, agent) => {
    if (state.workflowId !== undefined && info.id !== state.workflowId) return
    const node = state.graph.nodes.find(item => item.id === agent.childId)
    if (node) {
      node.status = agent.outcome === 'completed' ? 'completed' : agent.outcome
      node.result = { outcome: agent.outcome }
    }
    recordEvent(state, 'agent_ended', `Native DSH child ended for ${agent.label}`, {
      workflow_id: info.id,
      agent_id: agent.childId,
      role: agent.label,
      outcome: agent.outcome,
    })
  })
  ctx.on('workflow/end', (info, result) => {
    if (state.workflowId !== undefined && info.id !== state.workflowId) return
    recordEvent(state, 'workflow_end', `Native DSH workflow settled: ${result.stopReason}`, {
      workflow_id: info.id,
      stop_reason: result.stopReason,
      agents_started: result.agentsStarted,
    })
  })
  ctx.on('agent/created', ({ agent }) => installRootAgent(agent, config, state))
  for (const agent of ctx.agents.list()) installRootAgent(agent, config, state)
}
