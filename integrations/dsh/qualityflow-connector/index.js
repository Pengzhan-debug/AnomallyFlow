import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'qualityflow-connector'
export const inject = ['tools']

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'qualityflow_health',
    description: 'Check whether the local QualityFlow API is available.',
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(_args, exec) {
      const response = await fetch('http://127.0.0.1:8000/healthz', {
        signal: AbortSignal.any([exec.signal, AbortSignal.timeout(3000)]),
        redirect: 'error',
      })
      if (!response.ok) throw new Error(`QualityFlow API returned HTTP ${response.status}.`)
      const health = await response.json()
      if (health.status !== 'ok') throw new Error('QualityFlow API reported an unexpected health status.')
      return 'QualityFlow API is healthy.'
    },
  }))
}
