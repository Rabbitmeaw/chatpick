"""仅供本机离线预览的 HTTP 服务。"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core import ROOT
from .generation import GenerationError
from . import background

STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/charts.js': ('charts.js', 'text/javascript; charset=utf-8'),
          '/live-ui.js': ('live-ui.js', 'text/javascript; charset=utf-8'),
          '/daily-ui.js': ('daily-ui.js', 'text/javascript; charset=utf-8'),
          '/import-ui.js': ('import-ui.js', 'text/javascript; charset=utf-8'),
          '/discord-format.js': ('discord-format.js', 'text/javascript; charset=utf-8'),
          '/assets/app-icon.png': ('assets/app-icon.png', 'image/png'),
          '/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/layouts.css': ('layouts.css', 'text/css; charset=utf-8')}


def create_server(app, port=8765):
    def source_app(values):
        source=values.get('source','bot')
        if source not in ('account','bot'): raise ValueError('请选择个人账号或服务器Bot数据源。')
        if source=='bot':
            if values.get('account_id'): raise ValueError('Bot请求不能携带个人身份。')
            return app
        if not getattr(app,'live',False): raise ValueError('离线样例不读取个人记录。')
        return app.account_data.reader(values.get('server',''),values.get('channel',''),values.get('account_id',''),values.get('timezone','Asia/Shanghai'))

    def source_payload(values, timezone=False):
        return {key:value for key,value in values.items() if key not in ({'source','account_id'} if timezone else {'source','account_id','timezone'})}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # 不记录请求内容、模型地址或凭据。

        def trusted(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed:
                return False
            origin = self.headers.get('Origin')
            return not origin or origin in {f'http://{host}' for host in allowed}

        def reply(self, value, status=200, mime='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.trusted():
                return self.reply({'error': '仅允许从本机页面访问。'}, 403)
            parsed = urlsplit(self.path)
            if parsed.path in STATIC:
                name, mime = STATIC[parsed.path]
                return self.reply((ROOT / 'web' / name).read_bytes(), mime=mime)
            params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path == '/api/bootstrap':
                    if getattr(app,'live',False):
                        value={**source_app(params).bootstrap(params.get('server',''),params.get('channel',''),remember=params.get('remember')!='0'),'app':'GroupPulse','version':'live-1'}
                    else:
                        server = params.get('server', 'community')
                        channel = params.get('channel') or next(iter(app.server_channels(server)))
                        value = {**app.bootstrap(server, channel), 'app': 'GroupPulse', 'version': 'offline-1'}
                elif parsed.path == '/api/report':
                    if getattr(app,'live',False) and params.get('kind')!='activity':
                        return self.reply({'error':'真实模型生成请点击页面按钮，通过POST提交。'},405)
                    value = source_app(params).report(**{k: v for k, v in params.items() if k in {'date', 'channel', 'kind', 'member', 'topic', 'server', 'mode', 'period', 'focus_member', 'focus_topic', 'end_date'}})
                elif parsed.path == '/api/account-data' and getattr(app,'live',False):
                    value=app.account_data.state(params.get('server',''),params.get('channel',''),params.get('account_id',''),params.get('timezone','Asia/Shanghai'))
                elif parsed.path == '/api/sync' and getattr(app,'live',False):
                    value=app.sync_job(params.get('id',''))
                elif parsed.path == '/api/report-job' and getattr(app,'live',False):
                    value=app.report_jobs.get(params.get('id',''),params.get('server',''),params.get('channel',''))
                elif parsed.path == '/api/question-job' and getattr(app,'live',False):
                    value=app.question_jobs.get(params.get('id',''),{key:value for key,value in params.items() if key!='id'})
                elif parsed.path == '/api/auto-sync' and getattr(app,'live',False):
                    value=app.auto_sync.status()
                elif parsed.path == '/api/daily-task' and getattr(app,'live',False):
                    value=source_app(params).daily_tasks.state(params.get('server',''),params.get('channel',''),params.get('before',''))
                elif parsed.path == '/api/daily-report' and getattr(app,'live',False):
                    value=source_app(params).daily_tasks.report(params.get('server',''),params.get('channel',''),params.get('id',''))
                elif parsed.path == '/api/background' and getattr(app,'live',False):
                    value=background.info(app,self.server.server_port)
                elif parsed.path == '/api/imports' and getattr(app,'live',False):
                    value=app.file_imports.items()
                elif parsed.path == '/api/import-history' and getattr(app,'live',False):
                    value=app.file_imports.history(params.get('server',''),params.get('channel',''))
                elif parsed.path == '/api/exporter' and getattr(app,'live',False):
                    value=app.exporter.state()
                elif parsed.path == '/api/export-records' and getattr(app,'live',False):
                    value=app.exporter.job(params.get('id',''))
                elif parsed.path == '/api/import' and getattr(app,'live',False):
                    value=app.file_imports.inspect(params.get('import_id',''),params.get('timezone','Asia/Shanghai'))
                elif parsed.path == '/api/import-report' and getattr(app,'live',False):
                    value=app.file_imports.saved(params.get('import_id',''),params.get('server',''),params.get('channel',''),params.get('id',''))
                elif parsed.path == '/api/config':
                    value = app.get_config()
                elif parsed.path == '/api/conversations':
                    value = source_app(params).conversations(params.get('server', ''), params.get('channel', ''))
                elif parsed.path == '/api/conversation':
                    value = source_app(params).conversation(params.get('id', ''), params.get('server', ''), params.get('channel', ''), params.get('before', ''))
                elif parsed.path == '/api/bots':
                    value = app.bots(params.get('server', ''))
                elif parsed.path == '/api/alerts':
                    selected=source_app(params)
                    value = {'alerts': selected.alerts(params.get('channel', ''), params.get('server', 'community'))}
                    if getattr(app,'live',False):value.update(offline=False,rules=selected.alert_rules(params.get('channel',''),params.get('server','')))
                else:
                    return self.reply({'error': '页面不存在，请回到首页。'}, 404)
                self.reply(value)
            except GenerationError as e:
                self.reply({'error': str(e)}, 502)
            except ValueError as e:
                self.reply({'error': str(e)}, 400)

        def do_POST(self):
            if not self.trusted() or self.headers.get('X-GroupPulse') not in ('offline','local'):
                return self.reply({'error': '请通过本机页面操作。'}, 403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply({'error': '请使用页面表单提交。'}, 415)
            try:
                size = int(self.headers.get('Content-Length', 0))
                path = urlsplit(self.path).path
                limit = 50 * 1024 * 1024 if path == '/api/imports' and getattr(app,'live',False) else 32768
                if not 0 < size <= limit:
                    return self.reply({'error': '表单内容过大或为空。'}, 413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('表单内容格式错误。')
                path = urlsplit(self.path).path
                if path == '/api/demo':
                    value = app.import_demo()
                elif path == '/api/config':
                    value = app.save_config(data)
                elif path == '/api/config/check':
                    value = app.check_config(data)
                elif path == '/api/report':
                    fields = {'date','channel','kind','member','topic','server','mode','period','focus_member','focus_topic','end_date'}
                    if getattr(app,'live',False): fields |= {'start_time','end_time','instructions'}
                    if not data.keys() <= fields:
                        raise ValueError('报告字段有误，请通过页面选择。')
                    value=app.report(**data)
                elif path == '/api/imports' and getattr(app,'live',False):
                    value=app.file_imports.add(data)
                elif path == '/api/report-jobs' and getattr(app,'live',False):
                    value=app.report_jobs.queue(data)
                elif path == '/api/import-report' and getattr(app,'live',False):
                    value=app.file_imports.report(data)
                elif path == '/api/exporter' and getattr(app,'live',False):
                    value=app.exporter.save(data)
                elif path == '/api/exporter/connect' and getattr(app,'live',False):
                    if data: raise ValueError('请通过读取设置连接账号。')
                    value=app.exporter.connect()
                elif path == '/api/exporter/channels' and getattr(app,'live',False):
                    if data.keys() != {'server'}: raise ValueError('请选择服务器。')
                    value=app.exporter.channels(data['server'])
                elif path == '/api/export-records' and getattr(app,'live',False):
                    value=app.exporter.queue(data)
                elif path == '/api/topic-trends' and getattr(app,'live',False):
                    selected=source_app(data);data=source_payload(data)
                    if not {'server','channel','date'}<=data.keys() or not data.keys()<={'server','channel','date','end_date','refresh'}:
                        raise ValueError('请通过当前频道的统计页面分析话题。')
                    value=selected.topic_trends(**data)
                elif path == '/api/bots/connect' and getattr(app,'live',False):
                    if data.keys()!={'bot_id'}:
                        raise ValueError('请选择已保存的Bot。')
                    value=app.connect_bot(data['bot_id'])
                elif path == '/api/discord/channels' and getattr(app,'live',False):
                    if data.keys()!={'bot_id','server'}:
                        raise ValueError('请选择Bot和服务器。')
                    value=app.discover_channels(data['bot_id'],data['server'])
                elif path == '/api/sync' and getattr(app,'live',False):
                    value=app.start_sync(data)
                elif path == '/api/auto-sync' and getattr(app,'live',False):
                    if not data.keys() <= {'server','channel','enabled'} or not {'server','channel'} <= data.keys():
                        raise ValueError('请选择当前服务器和频道。')
                    if 'enabled' in data and not isinstance(data['enabled'],bool):
                        raise ValueError('自动同步设置无效。')
                    value=app.auto_sync.select(**data)
                elif path == '/api/daily-task' and getattr(app,'live',False):
                    value=source_app(data).daily_tasks.save(source_payload(data,timezone=True))
                elif path == '/api/daily-task/run' and getattr(app,'live',False):
                    value=source_app(data).daily_tasks.queue(source_payload(data))
                elif path == '/api/account-sync' and getattr(app,'live',False):
                    value=app.account_data.configure_sync({key:value for key,value in data.items() if key!='source'})
                elif path == '/api/background/start' and getattr(app,'live',False):
                    if data:
                        raise ValueError('请通过页面开启后台运行。')
                    value=background.request_start(self.server.server_port)
                elif path == '/api/alerts':
                    value = source_app(data).check_alerts(**{k: v for k, v in data.items() if k in {'keyword', 'channel', 'date', 'server'}})
                elif path == '/api/alert-rules' and getattr(app,'live',False):
                    value=source_app(data).save_alert_rule(source_payload(data))
                elif path == '/api/question-jobs' and getattr(app,'live',False):
                    value=app.question_jobs.queue(data)
                elif path == '/api/questions':
                    selected=source_app(data);data=source_payload(data)
                    if not data.keys() <= {'question', 'server', 'channel', 'start_date', 'end_date', 'conversation_id'}:
                        raise ValueError('问答表单字段有误，请通过当前频道页面重新提问。')
                    value = selected.ask(**data)
                elif path == '/api/bots':
                    value = app.save_bot(data)
                elif path == '/api/deliveries/preview':
                    if not data.keys() <= {'report_id', 'bot_id', 'destination', 'server', 'channel', 'kind'}:
                        raise ValueError('请通过报告页面选择发送预览。')
                    value = app.preview_delivery(**data)
                elif path == '/api/deliveries/simulate':
                    if not data.keys() <= {'preview_id', 'server', 'channel'}:
                        raise ValueError('请通过预览执行本地模拟。')
                    value = app.simulate_delivery(**data)
                else:
                    return self.reply({'error': '操作不存在。'}, 404)
                self.reply(value)
            except GenerationError as error:
                self.reply({'error': str(error)}, 502)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                self.reply({'error': '表单内容有误，请检查字段后重试。'}, 400)
            except ValueError as error:
                # 业务校验错误均为固定中文文案，不回显字段原值。
                self.reply({'error': str(error)}, 400)

    server_class=ThreadingHTTPServer if getattr(app,'live',False) else HTTPServer
    return server_class(('127.0.0.1', port), Handler)
