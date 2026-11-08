import { z } from 'zod';

export const STRATEGIES = ['stabilize_context', 'split_context', 'batch_layout', 'css_layout',
  'yield_work', 'worker', 'virtualize', 'defer_render', 'colocate_state', 'memoize', 'contain_layout'];

const answerSchema = z.object({
  culprit: z.string().min(1),
  source_file: z.string().min(1),
  suggested_strategy: z.enum(STRATEGIES),
  explanation: z.string().min(1),
}).strict();

export function declares(source, name) {
  if (!/^[A-Za-z_$][\w$]*$/.test(name ?? '')) return false;
  const escaped = name.replace(/[$]/g, '\\$');
  return new RegExp(`\\b(?:function|const|let|class)\\s+${escaped}\\b`).test(source);
}

export function sourceSupportsBlame(report, source) {
  const blame = report.explanation.blame;
  if (declares(source, blame.name)) return true;
  return blame.kind === 'script' && blame.name === report.target?.handler &&
    declares(source, blame.detail) && /^[A-Za-z][\w]*$/.test(blame.name) &&
    new RegExp(`\\b${blame.name}\\s*=`).test(source);
}

export function evaluate(report, strategies, text, results) {
  let answer;
  try { answer = answerSchema.parse(JSON.parse(text.trim().replace(/^```(?:json)?\s*|\s*```$/g, ''))); } catch {}
  const culprit = report.explanation.blame.name;
  const sources = results.filter((r) => r.toolName === 'get_component_source' &&
    !r.output?.error && typeof r.output?.file === 'string' && typeof r.output?.source === 'string' &&
    sourceSupportsBlame(report, r.output.source));
  const checks = {
    read_source: sources.length > 0,
    named_culprit: !!answer && !!culprit && answer.culprit === culprit,
    documented_strategy: !!answer && strategies.includes(answer.suggested_strategy),
    cited_source: !!answer && sources.some((r) => r.output.file === answer.source_file),
  };
  return { answer, checks, outcome: Object.values(checks).every(Boolean) ? 'passed' : 'evaluation_failed' };
}

export function errorOutcome(error) {
  return ['AbortError', 'TimeoutError'].includes(error?.name) ? 'timeout' : 'error';
}

export async function explain({ report, strategies, generate, signal, onCheck }) {
  const results = [];
  try {
    const response = await generate({
      abortSignal: signal,
      onStepFinish: (step) => results.push(...(step.toolResults ?? [])),
    });
    const evaluation = evaluate(report, strategies, response.text ?? '', results);
    for (const [name, pass] of Object.entries(evaluation.checks)) onCheck(name, pass);
    return { ...evaluation, completed: true };
  } catch (error) {
    return { outcome: errorOutcome(error), completed: false };
  }
}
