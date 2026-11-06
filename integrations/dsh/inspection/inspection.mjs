import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

export const name = 'qualityflow-inspection'
export const inject = ['tools']
const toolName = 'qualityflow_inspection_context'

export async function apply(ctx) {
  const resolve = createRequire(process.env.QUALITYFLOW_DSH_CLI)
  const { defineTool } = await import(pathToFileURL(resolve.resolve('@deepseek-ai/dsh-tools')).href)
  const raw = process.env.QUALITYFLOW_DSH_CONTEXT
  if (!raw) throw new Error('QualityFlow inspection context is required')
  const context = JSON.parse(raw)
  if (!context.task?.task_id || !context.batch?.batch_id || !Array.isArray(context.history)) {
    throw new Error('QualityFlow inspection context is invalid')
  }

  let used = false
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow: [toolName] })
  })
  ctx.on('agent/pre-step', async ({ step }, next) => step <= 2 ? next() : { kind: 'reject' })
  ctx.tools.guard(call => call.name === toolName ? undefined : 'Only the QualityFlow inspection tool is permitted.')
  ctx.tools.register(defineTool({
    name: toolName,
    description: 'Read the assigned inspection, its production batch, and up to five earlier inspections of the same product.',
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      exec.signal.throwIfAborted()
      if (Object.keys(args).length) throw new Error('This tool does not accept arguments.')
      if (used) throw new Error('The inspection context has already been read in this run.')
      used = true
      return JSON.stringify(context)
    },
  }))
  ctx.provide('qualityflowInspectionReady', true)
}
