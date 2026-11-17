"""真实HTTP边界：不跟随重定向，不回显响应正文或凭据。"""

import json
import socket
import ssl
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .generation import GenerationError


class ServiceError(GenerationError):
    def __init__(self, message, code='connection', status=0, retry_after=0):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def tls_context():
    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if not paths.cafile and not paths.capath:
        try:
            import certifi
        except ImportError:
            raise ServiceError('本机缺少 HTTPS 根证书，请联系维护者安装应用的证书依赖后重试。', 'certificate') from None
        context.load_verify_locations(cafile=certifi.where())
    return context


def chat_stream(response):
    """逐行接收SSE，让推理片段/服务端心跳维持连接；归并回原Chat Completions结构。"""
    content, reasoning, tools, lines = [], [], {}, []
    total, finish = 0, None
    while True:
        raw = response.readline(8 * 1024 * 1024 + 1)
        total += len(raw)
        if total > 8 * 1024 * 1024:
            raise ServiceError('服务返回内容过大，请缩小范围后重试。', 'response_size')
        line = raw.decode('utf-8').rstrip('\r\n').lstrip('\ufeff')
        if line.startswith('data:'):
            lines.append(line[5:].lstrip(' '))
        if not line or not raw:
            payload = '\n'.join(lines)
            lines = []
            if payload == '[DONE]': break
            if payload:
                value = json.loads(payload)
                if not isinstance(value,dict) or value.get('error'):
                    raise ServiceError('模型流式响应异常；结果可能未知，未自动重发。', 'connection')
                choices = value.get('choices',[])
                if not isinstance(choices,list):
                    raise ServiceError('模型流式响应格式不兼容。', 'protocol')
                for choice in choices:
                    if not isinstance(choice,dict):
                        raise ServiceError('模型流式响应格式不兼容。', 'protocol')
                    if choice.get('index',0) != 0: continue
                    delta = choice.get('delta') or {}
                    if not isinstance(delta,dict):
                        raise ServiceError('模型流式响应格式不兼容。', 'protocol')
                    for field, target in (('content',content),('reasoning_content',reasoning)):
                        part = delta.get(field)
                        if part is not None:
                            if not isinstance(part,str):
                                raise ServiceError('模型流式响应格式不兼容。', 'protocol')
                            target.append(part)
                    fragments = delta.get('tool_calls') or []
                    if not isinstance(fragments,list):
                        raise ServiceError('模型流式工具响应格式不兼容。', 'protocol')
                    for fragment in fragments:
                        if (not isinstance(fragment,dict) or type(fragment.get('index')) is not int
                                or not 0 <= fragment['index'] < 5):
                            raise ServiceError('模型流式工具响应格式不兼容。', 'protocol')
                        tool = tools.setdefault(fragment['index'],dict(id='',type='function',function=dict(name='',arguments='')))
                        function = fragment.get('function') or {}
                        if not isinstance(function,dict) or fragment.get('type','function')!='function':
                            raise ServiceError('模型流式工具响应格式不兼容。', 'protocol')
                        for field, source, target in (('id',fragment,tool),('name',function,tool['function']),('arguments',function,tool['function'])):
                            part = source.get(field)
                            if part is not None:
                                if not isinstance(part,str):
                                    raise ServiceError('模型流式工具响应格式不兼容。', 'protocol')
                                target[field] += part
                    if choice.get('finish_reason') is not None:
                        finish = choice['finish_reason']
        if not raw: break
    if not finish:
        raise ServiceError('模型连接在完成前中断，结果可能未知；未采用部分结果，未自动重发。', 'connection')
    message = dict(role='assistant',content=''.join(content))
    if reasoning: message['reasoning_content'] = ''.join(reasoning)
    if tools: message['tool_calls'] = [tools[index] for index in sorted(tools)]
    return dict(choices=[dict(message=message,finish_reason=finish)])


def http_json(url, headers, data=None, timeout=60):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    streaming = isinstance(data,dict) and data.get('stream') is True
    request = Request(url, data=body, headers={**headers, 'Accept':'text/event-stream' if streaming else 'application/json',
                      'Content-Type':'application/json', 'User-Agent':'GroupPulse/1.0 (local read-only client)'},
                      method='POST' if body is not None else 'GET')
    try:
        handlers = [NoRedirect()]
        if request.type == 'https':
            handlers.append(HTTPSHandler(context=tls_context()))
        with build_opener(*handlers).open(request, timeout=timeout) as response:
            if streaming and response.headers.get('Content-Type','').split(';')[0].strip().lower()=='text/event-stream':
                return chat_stream(response)
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ServiceError('服务返回内容过大，请缩小范围后重试。', 'response_size')
        return json.loads(raw)
    except HTTPError as error:
        status = error.code
        retry = 0
        if status == 429:
            try:
                retry = min(5, max(0, float(error.headers.get('Retry-After', '1'))))
            except (ValueError, TypeError):
                retry = 1
        error.close()
        texts = {400:'服务不接受当前参数，请检查模型、协议及高级设置。',
                 401:'鉴权失败，请检查密钥或 Bot Token。',
                 403:'服务拒绝访问，请检查 Bot 频道权限或模型使用权限。',
                 404:'地址、模型或频道不存在，请检查配置。',
                 429:'服务限流或额度不足，请稍后重试并检查额度。'}
        message = texts.get(status, '服务暂时不可用，请稍后重试。' if status >= 500 else '服务响应异常，请检查地址与协议。')
        if 300 <= status < 400:
            message = '服务地址发生重定向，已停止以保护凭据；请填写最终 API 地址。'
        raise ServiceError(message, 'http', status, retry) from None
    except (URLError, socket.timeout, TimeoutError, OSError, HTTPException) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise ServiceError('安全连接失败：服务证书未通过验证。请检查电脑时间和服务证书，或联系维护者修复根证书配置。', 'certificate') from None
        if isinstance(reason, ssl.SSLError):
            raise ServiceError('安全连接失败：TLS 握手未完成，请检查服务地址、证书和网络设置。', 'tls') from None
        raise ServiceError('无法连接服务或请求超时，请检查地址、网络和超时设置；未自动重试未知结果。', 'connection') from None
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise ServiceError('服务未返回有效 JSON，请检查 API 基础地址和协议。', 'protocol') from None


class ChatModel:
    engine = 'openai-compatible'

    def __init__(self, config, secret):
        self.config, self.secret = config, secret
        self.calls = 0

    @property
    def input_budget(self):
        return self.config()['input_kib'] * 1024

    def complete(self, messages, tools=None, timeout=None, *, json_mode=None):
        config = self.config()
        if not config['base_url'] or not config['model']:
            raise ServiceError('请先在模型设置中保存服务地址和模型名称。', 'configuration')
        counted = dict(messages=messages,tools=tools) if tools is not None else messages
        if len(json.dumps(counted, ensure_ascii=False).encode()) > config['input_kib'] * 1024:
            raise ServiceError('本次输入超过设置的长度预算，请缩小周期或调高输入预算。', 'input_size')
        key = self.secret()
        headers = {'Authorization':'Bearer ' + key} if key else {}
        body = dict(model=config['model'], messages=messages, stream=True)
        if tools:
            body.update(tools=tools,tool_choice='auto')
        parameter = config['output_parameter']
        if parameter == 'auto':
            from urllib.parse import urlsplit
            parameter = 'max_completion_tokens' if urlsplit(config['base_url']).hostname == 'api.openai.com' else 'max_tokens'
        body[parameter] = config['max_tokens']
        if config['temperature'] is not None:
            body['temperature'] = config['temperature']
        if (config['json_mode'] if json_mode is None else json_mode) and tools is None:
            body['response_format'] = {'type':'json_object'}
        base = config['base_url'].rstrip('/')
        endpoint = base if base.endswith('/chat/completions') else base + '/chat/completions'
        for attempt in range(config['retries'] + 1):
            try:
                self.calls += 1
                value = http_json(endpoint, headers, body, min(config['timeout'],timeout/1000) if timeout else config['timeout'])
                break
            except ServiceError as error:
                # 只重试服务明确拒绝的限流请求，不重试连接中断等未知计费结果。
                if error.status != 429 or attempt == config['retries']:
                    raise
                time.sleep(error.retry_after or 1)
        try:
            choice = value['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ServiceError('模型输出已截断，未采用不完整结果。请提高最大输出长度，或减小输入预算以分更多批次。', 'truncated')
            if choice.get('finish_reason') not in (('stop','tool_calls',None) if tools is not None else ('stop',None)):
                raise ServiceError('模型未正常完成回答，请检查服务限制后重试。', 'completion')
            message = choice['message']
            if tools is not None:
                calls = message.get('tool_calls') or []
                if not isinstance(calls,list) or len(calls)>5:
                    raise ValueError()
                parsed = []
                for call in calls:
                    if (call.get('type')!='function' or not isinstance(call.get('id'),str) or
                        not isinstance(call['function']['name'],str) or not isinstance(call['function']['arguments'],str)):
                        raise ValueError()
                    parsed.append(dict(id=call['id'],name=call['function']['name'],arguments=call['function']['arguments']))
                content = message.get('content') or ''
                if not isinstance(content,str) or (not parsed and not content.strip()):
                    raise ValueError()
                # Kimi requires its reasoning continuation beside native tool calls.
                assistant = {k:v for k,v in message.items() if k in ('role','content','tool_calls','reasoning_content')}
                assistant['role']='assistant'
                return dict(success=True,data=content,toolCalls=parsed,assistantMessage=assistant)
            content = message['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            return content
        except (KeyError, IndexError, TypeError, ValueError) as error:
            if isinstance(error, ServiceError):
                raise
            raise ServiceError('模型响应格式不兼容或内容为空，请检查模型与协议。', 'protocol') from None

    def complete_text(self, messages):
        return self.complete(messages,json_mode=False)

    def complete_tools(self, messages, tools, timeout=None):
        return self.complete(messages,tools=tools,timeout=timeout)
