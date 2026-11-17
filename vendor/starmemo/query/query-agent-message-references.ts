import { randomBytes } from 'node:crypto'
import { decodeMessageRef, encodeMessageRef } from '../../shared/local-query-api'

const REFERENCE_TOKEN = /[A-Za-z0-9_-]+/g
const CANONICAL_PREFIX = 'eyJjIjoi' // base64url of the exact JSON prefix {"c":".
const RUN_REFERENCE = /\bqmr_[a-f0-9]{24}_[1-9]\d*\b/g
const JSON_STRING_PART = String.raw`(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*`
const TRUNCATED_REFERENCE = new RegExp(
  String.raw`^\{"c":"(${JSON_STRING_PART})","m":"${JSON_STRING_PART}(?:\\(?:u[0-9a-fA-F]{0,3})?)?"?$`
)

function isCanonicalReference(value: string): boolean {
  const identity = decodeMessageRef(value)
  return (
    identity !== null && encodeMessageRef(identity.conversationId, identity.messageId) === value
  )
}

/** Only accept a truncated token after both canonical identity keys are established. */
function isTruncatedCanonicalReference(value: string): boolean {
  const prefix = Buffer.from(value, 'base64url').toString('utf8')
  const match = TRUNCATED_REFERENCE.exec(prefix)
  if (!match) return false
  try {
    const conversation: unknown = JSON.parse(`"${match[1]}"`)
    return (
      typeof conversation === 'string' &&
      conversation.trim().length > 0 &&
      JSON.stringify(conversation) === `"${match[1]}"`
    )
  } catch {
    return false
  }
}

/** Remove host message identities, leaving unrelated base64, URLs and prose intact. */
export function hideCanonicalMessageReferences(text: string): string {
  return text.replace(REFERENCE_TOKEN, (token) => {
    if (!token.startsWith(CANONICAL_PREFIX)) return token
    return isCanonicalReference(token) || isTruncatedCanonicalReference(token) ? '' : token
  })
}

/** Prior turns cannot carry current citations or references into another model run. */
export function sanitizeQueryHistoryText(text: string): string {
  return hideCanonicalMessageReferences(text)
    .replace(RUN_REFERENCE, '')
    .replace(/\[E\d+\]/g, '')
    .trim()
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** One instance belongs to one run; raw references remain confined to the host. */
export class QueryAgentMessageReferences {
  private readonly prefix = `qmr_${randomBytes(12).toString('hex')}_`
  private readonly aliases = new Map<string, string>()
  private readonly references = new Map<string, string>()

  forModel(value: unknown): unknown {
    if (Array.isArray(value)) return value.map((item) => this.forModel(item))
    if (typeof value === 'string') return hideCanonicalMessageReferences(value)
    if (!value || typeof value !== 'object') return value
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => {
        if (key !== 'messageRef' || typeof item !== 'string' || !item)
          return [key, this.forModel(item)]
        let alias = this.aliases.get(item)
        if (!alias) {
          alias = `${this.prefix}${this.aliases.size + 1}`
          this.aliases.set(item, alias)
          this.references.set(alias, item)
        }
        return [key, alias]
      })
    )
  }

  resolve(value: unknown): string | undefined {
    return typeof value === 'string' ? this.references.get(value) : undefined
  }

  cleanAnswer(answer: string, citationFor: (realRef: string) => string | undefined): string {
    const known = [...this.aliases.keys(), ...this.references.keys()]
      .sort((left, right) => right.length - left.length)
      .map(escapeRegExp)
    let result = answer
    if (known.length) {
      const tokens = known.join('|')
      const citation = (token: string): string => {
        const real = this.references.get(token) ?? token
        const id = citationFor(real)
        return id && /^E[1-9]\d*$/.test(id) ? `[${id}]` : ''
      }
      // Consume metadata labels with their value, rather than leave host field names in prose.
      const assignment = new RegExp(
        `(?:["'\x60]messageRef["'\x60]|\\bmessageRef\\b)\\s*[:：=]\\s*(["'\x60]?)(${tokens})\\1(?![A-Za-z0-9_-])`,
        'g'
      )
      result = result.replace(assignment, (_match, _quote: string, token: string) =>
        citation(token)
      )
      result = result.replace(new RegExp(`\x60(${tokens})\x60`, 'g'), (_match, token: string) =>
        citation(token)
      )
      result = result.replace(
        new RegExp(`(^|[^A-Za-z0-9_-])(${tokens})(?=$|[^A-Za-z0-9_-])`, 'g'),
        (_match, prefix: string, token: string) => `${prefix}${citation(token)}`
      )
    }
    result = result.replace(
      /(?:["'`]citationId["'`]|\bcitationId\b)\s*[:：=]\s*(["'`]?)(E[1-9]\d*)\1(?![A-Za-z0-9_-])/g,
      '[$2]'
    )
    return hideCanonicalMessageReferences(result).trim()
  }
}
