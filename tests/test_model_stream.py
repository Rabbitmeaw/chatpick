import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from grouppulse import network
from grouppulse.live import LIVE_CONFIG


def event(delta=None, finish=None):
    return 'data: '+json.dumps(dict(choices=[dict(index=0,delta=delta or {},finish_reason=finish)]),ensure_ascii=False)+'\n\n'


class ModelStreamTests(unittest.TestCase):
    def complete(self, wire, *, tools=None):
        response=io.BytesIO(wire.encode())
        response.headers={'Content-Type':'text/event-stream; charset=utf-8'}
        opener=MagicMock();opener.open.return_value=response
        config=dict(LIVE_CONFIG,base_url='http://example.invalid/v1',model='test',retries=3)
        model=network.ChatModel(lambda:config,lambda:'test-only-key')
        with patch.object(network,'build_opener',return_value=opener):
            value=model.complete([dict(role='user',content='原提示词不变')],tools)
        return value,opener

    def test_streamed_unicode_json_reasoning_and_usage_keep_original_text(self):
        wire=': heartbeat\n\n'+event(dict(role='assistant',reasoning_content='思考中'))
        wire+=event(dict(content='{"summary":"闲聊'))+event(dict(content='与观点"}'))+event(finish='stop')
        wire+='data: {"choices":[],"usage":{"completion_tokens":12}}\n\ndata: [DONE]\n\n'
        value,opener=self.complete(wire)
        self.assertEqual(value,'{"summary":"闲聊与观点"}')
        self.assertTrue(json.loads(opener.open.call_args.args[0].data)['stream'])
        opener.open.assert_called_once()

    def test_tool_call_fragments_and_kimi_reasoning_reach_original_engine(self):
        wire=event(dict(role='assistant',reasoning_content='查原文。'))
        wire+=event(dict(tool_calls=[dict(index=0,id='call-1',type='function',function=dict(name='query_',arguments='{"text":'))]))
        wire+=event(dict(tool_calls=[dict(index=0,function=dict(name='messages',arguments='"话题"}'))]))
        wire+=event(finish='tool_calls')+'data: [DONE]\n\n'
        value,_=self.complete(wire,tools=[])
        self.assertEqual(value['toolCalls'],[dict(id='call-1',name='query_messages',arguments='{"text":"话题"}')])
        self.assertEqual(value['assistantMessage']['reasoning_content'],'查原文。')

    def test_incomplete_stream_is_unknown_without_retry_or_partial_report(self):
        wire=event(dict(content='尚未完成，不能采用'))
        response=io.BytesIO(wire.encode());response.headers={'Content-Type':'text/event-stream'}
        opener=MagicMock();opener.open.return_value=response
        config=dict(LIVE_CONFIG,base_url='http://example.invalid/v1',model='test',retries=5)
        with patch.object(network,'build_opener',return_value=opener):
            with self.assertRaises(network.ServiceError) as caught:
                network.ChatModel(lambda:config,lambda:'').complete([dict(role='user',content='原文')])
        self.assertEqual(caught.exception.code,'connection')
        self.assertNotIn('尚未完成',str(caught.exception))
        opener.open.assert_called_once()

    def test_stream_errors_and_truncation_do_not_expose_provider_payload(self):
        for wire in ('data: {"error":{"message":"private-upstream"}}\n\n',
                     event(dict(content='不完整'))+event(finish='length')+'data: [DONE]\n\n'):
            with self.subTest(wire=wire),self.assertRaises(network.ServiceError) as caught:
                self.complete(wire)
            self.assertNotIn('private-upstream',str(caught.exception))

    def test_idle_timeout_resets_while_real_http_heartbeats_arrive(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                try:
                    for _ in range(6):
                        self.wfile.write(b': keepalive\n\n');self.wfile.flush();time.sleep(.05)
                    self.wfile.write((event(dict(content='完成'))+event(finish='stop')+'data: [DONE]\n\n').encode())
                    self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        started=time.monotonic()
        try:
            result=network.http_json(f'http://127.0.0.1:{server.server_port}/chat/completions',{},dict(stream=True),.15)
            self.assertEqual(result['choices'][0]['message']['content'],'完成')
            self.assertGreater(time.monotonic()-started,.3)
        finally:
            server.shutdown();server.server_close();worker.join(2)
