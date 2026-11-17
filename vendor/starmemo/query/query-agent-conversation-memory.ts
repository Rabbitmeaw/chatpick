import type { AskChannelConversationContext } from '../../shared/query-agent'
import type { QueryAgentHistoryTurn } from './query-agent-service'

/** Current-process conversation context. No idle expiry and no history-file hydration. */
export const CONVERSATION_MEMORY_MAX_TURNS = 6
export const CONVERSATION_MEMORY_QUESTION_MAX_CHARS = 256
export const CONVERSATION_MEMORY_ANSWER_MAX_CHARS = 512
export const CONVERSATION_MEMORY_MAX_PENDING_TURNS = 10
export const CONVERSATION_MEMORY_MAX_CONVERSATIONS = 50
export const CONVERSATION_SUMMARY_MAX_CHARS = 2_400
export const CONVERSATION_SUMMARY_QUOTE_MAX_CHARS = 240

export interface ConversationSummaryItem {
  sourceId: string
  role: 'user' | 'assistant'
  /** An exact excerpt, verified against the source before it can enter memory. */
  quote: string
}

export interface ConversationSummary {
  goals: ConversationSummaryItem[]
  confirmedConditions: ConversationSummaryItem[]
  conclusions: ConversationSummaryItem[]
}

interface StoredTurn extends QueryAgentHistoryTurn {
  id: string
}

export interface ConversationCompactionInput {
  previousSummary?: ConversationSummary
  turnsToCompact: StoredTurn[]
  recentTurns: QueryAgentHistoryTurn[]
}

export type ConversationCompactionResult =
  | { success: true; summary: ConversationSummary }
  | { success: false }

interface ConversationEntry {
  generation: number
  revision: number
  nextTurnId: number
  turns: StoredTurn[]
  pending: StoredTurn[]
  summary?: ConversationSummary
  summarizedTurnCount: number
  omittedTurnCount: number
  summaryStatus: AskChannelConversationContext['summaryStatus']
  compaction?: Promise<void>
}

function truncate(value: string, maxChars: number): string {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
  return normalized.length > maxChars ? `${normalized.slice(0, maxChars)}…` : normalized
}

function cloneSummary(summary: ConversationSummary): ConversationSummary {
  return {
    goals: summary.goals.map((item) => ({ ...item })),
    confirmedConditions: summary.confirmedConditions.map((item) => ({ ...item })),
    conclusions: summary.conclusions.map((item) => ({ ...item }))
  }
}

/**
 * Keeps ten recent question/answer pairs and a bounded extractive summary of older pairs.
 * Tool results/evidence are never retained. A failed compaction preserves recent pairs and
 * up to ten pending older pairs; further overflow is explicitly counted, never hidden.
 * Entry identity/generation invalidates in-flight writes after clear, reset or LRU eviction.
 */
export class QueryAgentConversationMemory {
  private readonly entries = new Map<string, ConversationEntry>()
  private nextGeneration = 1

  begin(key: string): number {
    let entry = this.entries.get(key)
    if (!entry) {
      entry = {
        generation: this.nextGeneration++,
        revision: 0,
        nextTurnId: 1,
        turns: [],
        pending: [],
        summarizedTurnCount: 0,
        omittedTurnCount: 0,
        summaryStatus: 'none'
      }
    }
    // Insertion order is the LRU order; an active question makes this conversation recent.
    this.entries.delete(key)
    this.entries.set(key, entry)
    while (this.entries.size > CONVERSATION_MEMORY_MAX_CONVERSATIONS) {
      const oldest = this.entries.keys().next().value
      if (oldest !== undefined) this.entries.delete(oldest)
    }
    return entry.generation
  }

  isCurrent(key: string, generation: number): boolean {
    return this.entries.get(key)?.generation === generation
  }

  history(key: string): QueryAgentHistoryTurn[] {
    return (this.entries.get(key)?.turns || []).map(({ question, answer }) => ({
      question,
      answer
    }))
  }

  summary(key: string): ConversationSummary | undefined {
    const summary = this.entries.get(key)?.summary
    return summary ? cloneSummary(summary) : undefined
  }

  status(key: string): AskChannelConversationContext {
    const entry = this.entries.get(key)
    const summary = entry?.summary
    return {
      recentTurnCount: entry?.turns.length || 0,
      maxRecentTurns: CONVERSATION_MEMORY_MAX_TURNS,
      hasSummary: Boolean(
        summary &&
        [summary.goals, summary.confirmedConditions, summary.conclusions].some(
          (items) => items.length
        )
      ),
      summarizedTurnCount: entry?.summarizedTurnCount || 0,
      pendingTurnCount: entry?.pending.length || 0,
      omittedTurnCount: entry?.omittedTurnCount || 0,
      summaryStatus: entry?.summaryStatus || 'none'
    }
  }

  record(key: string, question: string, answer: string, generation?: number): boolean {
    if (generation !== undefined && !this.isCurrent(key, generation)) return false
    const normalizedQuestion = truncate(question, CONVERSATION_MEMORY_QUESTION_MAX_CHARS)
    const normalizedAnswer = truncate(answer, CONVERSATION_MEMORY_ANSWER_MAX_CHARS)
    if (!normalizedQuestion || !normalizedAnswer) return false
    if (generation === undefined) this.begin(key)
    const entry = this.entries.get(key)!
    entry.turns.push({
      id: `t${entry.nextTurnId++}`,
      question: normalizedQuestion,
      answer: normalizedAnswer
    })
    entry.revision += 1
    while (entry.turns.length > CONVERSATION_MEMORY_MAX_TURNS)
      entry.pending.push(entry.turns.shift()!)
    while (entry.pending.length > CONVERSATION_MEMORY_MAX_PENDING_TURNS) {
      entry.pending.shift()
      entry.omittedTurnCount += 1
    }
    if (entry.pending.length && !entry.compaction) entry.summaryStatus = 'pending'
    return true
  }

  /** Background work; callers need not await it before returning an already completed answer. */
  compact(
    key: string,
    generation: number,
    compact: (input: ConversationCompactionInput) => Promise<ConversationCompactionResult>
  ): Promise<void> {
    const entry = this.entries.get(key)
    if (!entry || entry.generation !== generation || !entry.pending.length) return Promise.resolve()
    if (entry.compaction) return entry.compaction
    const job = Promise.resolve()
      .then(async () => {
        // A changing conversation can invalidate a relevance decision. Retry once with fresh
        // context, then leave pending work for the next question instead of looping indefinitely.
        for (let attempt = 0; attempt < 2; attempt += 1) {
          if (this.entries.get(key) !== entry || !entry.pending.length) return
          entry.summaryStatus = 'compacting'
          const revision = entry.revision
          const pending = entry.pending.map((turn) => ({ ...turn }))
          let result: ConversationCompactionResult
          try {
            result = await compact({
              ...(entry.summary ? { previousSummary: cloneSummary(entry.summary) } : {}),
              turnsToCompact: pending,
              recentTurns: this.history(key)
            })
          } catch {
            result = { success: false }
          }
          if (this.entries.get(key) !== entry) return
          if (!result.success) {
            entry.summaryStatus = 'failed'
            return
          }
          if (revision !== entry.revision) {
            entry.summaryStatus = 'pending'
            continue
          }
          entry.summary = cloneSummary(result.summary)
          entry.pending = []
          entry.summarizedTurnCount += pending.length
          entry.summaryStatus = 'ready'
          entry.revision += 1
          return
        }
      })
      .finally(() => {
        if (this.entries.get(key) === entry) entry.compaction = undefined
      })
    entry.compaction = job
    return job
  }

  /** Omit key to invalidate every active/pending conversation. */
  forget(key?: string): void {
    if (key === undefined) this.entries.clear()
    else this.entries.delete(key)
  }
}
