import type {
  AIProviderFailureCategory,
  AIProviderFailureDiagnostic
} from '../../shared/ai-provider'

interface FailureInput {
  notConfigured?: boolean
  status?: number
  errorCode?: string
  errorType?: string
  error?: string
  timedOut?: boolean
  htmlInsteadOfJson?: boolean
  elapsedMs?: number
}

// Upstream codes are arbitrary strings and may echo request data. Retain only known values.
const SAFE_CODES = new Set([
  'invalid_api_key',
  'incorrect_api_key',
  'authentication_error',
  'insufficient_quota',
  'quota_exceeded',
  'billing_hard_limit_reached',
  'rate_limit_exceeded',
  'rate_limit_error',
  'model_not_found',
  'unsupported_model',
  'invalid_request_error',
  'unsupported_parameter',
  'unsupported_tool',
  'unsupported_function_call',
  'context_length_exceeded',
  'token_limit_exceeded',
  'invalid_response',
  'empty_response',
  'request_timeout',
  'overloaded',
  'server_error'
])
const SAFE_TYPES = new Set([
  'authentication_error',
  'permission_error',
  'rate_limit_error',
  'invalid_request_error',
  'not_found_error',
  'overloaded_error',
  'server_error',
  'api_error',
  'insufficient_quota',
  'timeout_error'
])

/** Classify locally; raw error text is used for matching only and is never returned. */
export function describeProviderFailure(input: FailureInput): AIProviderFailureDiagnostic {
  const code = input.errorCode?.toLowerCase() || ''
  const type = input.errorType?.toLowerCase() || ''
  const hint = `${code} ${type} ${input.error || ''}`.toLowerCase()
  const status =
    Number.isInteger(input.status) && input.status! >= 100 && input.status! <= 599
      ? input.status
      : undefined
  let category: AIProviderFailureCategory = 'unknown'
  if (input.notConfigured) category = 'not_configured'
  else if (
    input.timedOut ||
    status === 408 ||
    status === 504 ||
    /request_timeout|timeout_error/.test(hint)
  )
    category = 'timeout'
  else if (status === 401 || /invalid_api_key|incorrect_api_key|authentication_error/.test(hint))
    category = 'authentication'
  else if (/insufficient_quota|quota_exceeded|billing_hard_limit_reached/.test(hint))
    category = 'quota_exceeded'
  else if (status === 403 || type === 'permission_error') category = 'access_denied'
  else if (status === 429 || /rate_limit_exceeded|rate_limit_error/.test(hint))
    category = 'rate_limited'
  else if (status === 404 || /model_not_found|unsupported_model|not_found_error/.test(hint))
    category = 'endpoint_or_model'
  else if ((status !== undefined && status >= 500) || /server_error|overloaded/.test(hint))
    category = 'upstream_unavailable'
  else if (/context_length_exceeded|token_limit_exceeded/.test(hint)) category = 'context_too_large'
  else if (
    /unsupported_tool|unsupported_function_call/.test(hint) ||
    /(?:tool|function.call).*(?:not.support|unsupported|not.allowed)/.test(hint) ||
    /(?:not.support|unsupported).*(?:tool|function.call)/.test(hint)
  )
    category = 'tool_unsupported'
  else if (input.htmlInsteadOfJson || code === 'invalid_response' || code === 'empty_response')
    category = 'invalid_response'
  else if (status === 400 || status === 422 || type === 'invalid_request_error')
    category = 'invalid_request'
  else if (/fetch failed|network|econn|enotfound|eai_again/.test(hint)) category = 'network'

  return {
    category,
    ...(status !== undefined ? { status } : {}),
    ...(SAFE_CODES.has(code) ? { errorCode: code } : {}),
    ...(SAFE_TYPES.has(type) ? { errorType: type } : {}),
    ...(input.timedOut ? { timedOut: true } : {}),
    ...(input.htmlInsteadOfJson ? { htmlInsteadOfJson: true } : {}),
    ...(Number.isFinite(input.elapsedMs) && input.elapsedMs! >= 0
      ? { elapsedMs: Math.round(input.elapsedMs!) }
      : {})
  }
}

export function providerFailureMessage(failure: AIProviderFailureDiagnostic): string {
  switch (failure.category) {
    case 'not_configured':
      return '尚未配置可用的 AI 模型。请在 拾聊 ChatPick 的「模型设置」添加模型并设为默认。'
    case 'authentication':
      return 'AI 模型认证失败。请在 拾聊 ChatPick 的「模型设置」检查 API Key 和认证方式。'
    case 'access_denied':
      return 'AI 模型接口拒绝了请求。请检查账号权限、接口适用范围和认证方式，或更换服务商。'
    case 'quota_exceeded':
      return 'AI 模型额度不足。请检查服务商账号的余额或配额，或在「模型设置」更换默认模型。'
    case 'rate_limited':
      return 'AI 模型请求过于频繁或已达到用量限制。请稍后重试，或在「模型设置」更换默认模型。'
    case 'endpoint_or_model':
      return '未找到 AI 模型或接口。请在 拾聊 ChatPick 的「模型设置」检查模型 ID、接口地址和协议。'
    case 'tool_unsupported':
      return '当前模型接口不支持查询聊天记录所需的工具调用。请在「模型设置」检查接口协议，或更换支持工具调用的模型。'
    case 'invalid_request':
      return 'AI 模型接口不接受当前请求。请在 拾聊 ChatPick 的「模型设置」检查接口协议与模型参数；普通连接成功不代表支持查询工具。'
    case 'context_too_large':
      return '本次内容超过模型上下文限制。请缩小聊天范围或开始新对话，或更换支持更长上下文的模型。'
    case 'timeout':
      return 'AI 模型响应超时。请稍后重试，或在「模型设置」增加请求超时时间、关闭思考模式或更换模型。'
    case 'invalid_response':
      return 'AI 模型没有返回可用的完整回答。请在「模型设置」检查接口协议和输出长度限制，再重试。'
    case 'network':
      return '无法连接 AI 模型服务。请检查网络、代理和接口地址后重试。'
    case 'upstream_unavailable':
      return 'AI 模型服务暂时出现故障。请稍后重试，或在「模型设置」更换默认模型。'
    default:
      return 'AI 模型请求失败。请在 拾聊 ChatPick 的「模型设置」检查默认模型、接口地址与协议后重试。'
  }
}
