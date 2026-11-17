"""调用固定版本的原 DiscordChatExporter；JSON 作为内部证据，不让用户搬文件。"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .core import ROOT
from .discord import DiscordClient, identifier, snowflake
from .file_imports import MAX_FILE_BYTES, checked_timezone, parse_export
from .network import tls_context

VERSION = '2.48'
# 官方发行资产摘要；原 CLI 自带运行时，不要求使用者安装 .NET。
ASSETS = {
    ('Darwin','arm64'):('osx-arm64', '623f9d2dce568e17a46b8fbd366a18dca49803d386216f4ba24507d2c000fee9'),
    ('Darwin','x86_64'):('osx-x64', '91b4eae3525df85d084969004f3a287edad4eeaafd664e4869b26bb8422e2e88'),
}
INSTALL_LOCK = threading.Lock()


def ensure_binary(root):
    entry = ASSETS.get((platform.system(), platform.machine()))
    if not entry: raise ValueError('本机系统尚未提供自动导出组件；可使用折叠的 JSON 文件入口。')
    target, digest = entry
    directory = Path(root) / ('discord-exporter-' + VERSION + '-' + target)
    executable = directory / 'DiscordChatExporter.Cli'
    marker = directory / '.verified'
    with INSTALL_LOCK:
        license_text = (ROOT / 'vendor' / 'discordchatexporter' / 'LICENSE').read_text()
        if executable.is_file() and marker.is_file() and marker.read_text() == digest:
            (directory / 'LICENSE-DiscordChatExporter.txt').write_text(license_text)
            return str(executable)
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='.exporter-', dir=root) as temporary:
            temporary = Path(temporary)
            archive = temporary / 'cli.zip'
            url = f'https://github.com/Tyrrrz/DiscordChatExporter/releases/download/{VERSION}/DiscordChatExporter.Cli.{target}.zip'
            try:
                with urlopen(Request(url, headers={'User-Agent':'ChatPick/1.0'}), context=tls_context(), timeout=60) as response, archive.open('wb') as output:
                    total = 0
                    while chunk := response.read(128 * 1024):
                        total += len(chunk)
                        if total > 30 * 1024 * 1024: raise ValueError('导出组件大小异常，已停止准备。')
                        output.write(chunk)
            except OSError:
                raise ValueError('无法下载首次使用的读取组件，请检查网络后重试；没有读取聊天。') from None
            if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                raise ValueError('读取组件校验失败，已停止执行。')
            unpacked = temporary / 'unpacked'; unpacked.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                if sum(i.file_size for i in bundle.infolist()) > 160 * 1024 * 1024:
                    raise ValueError('读取组件解压大小异常。')
                for item in bundle.infolist():
                    if Path(item.filename).is_absolute() or '..' in Path(item.filename).parts:
                        raise ValueError('读取组件文件路径异常。')
                bundle.extractall(unpacked)
            binary = unpacked / 'DiscordChatExporter.Cli'
            if not binary.is_file(): raise ValueError('读取组件不完整，请重试准备。')
            binary.chmod(0o700)
            (unpacked / 'LICENSE-DiscordChatExporter.txt').write_text(license_text)
            (unpacked / '.verified').write_text(digest)
            if directory.exists(): shutil.rmtree(directory)
            os.replace(unpacked, directory)
    return str(executable)


def run_export(binary, token, channel, start, end, output, *, launch=None):
    identifier(channel)
    arguments = [str(binary), 'export', '-c', channel, '-f', 'Json', '-o', str(output),
                 '--after', str(snowflake(start)-1), '--before', str(snowflake(end)), '--include-threads', 'None',
                 '--markdown', 'false', '--media', 'false', '--utc', 'true']
    environment = {key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','SSL_CERT_FILE') if key in os.environ}
    environment['DISCORD_TOKEN'] = token
    launch = launch or subprocess.Popen
    try:
        with launch(arguments, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True) as process:
            try:
                return_code = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
    except subprocess.TimeoutExpired:
        raise ValueError('读取超过五分钟，已停止；请缩小日期范围或检查网络。未调用模型。') from None
    except OSError:
        raise ValueError('无法启动读取组件，请重新准备组件或联系维护者。') from None
    if return_code or not output.is_file():
        raise ValueError('记录导出未完成，请检查账号登录状态、频道权限和网络；未调用模型或使用部分文件。')
    if output.stat().st_size > MAX_FILE_BYTES: raise ValueError('本次记录超过文件处理大小，请缩小日期范围。')


class AccountClient(DiscordClient):
    def __init__(self, token, before_read):
        super().__init__(token)
        self.authorization = token
        self.before_read = before_read

    def get(self, path, params=None):
        return super().get(path, params, before_read=self.before_read)

    def identity(self):
        user = self.get('/users/@me')
        if not isinstance(user, dict) or user.get('bot') is True:
            raise ValueError('此入口需要个人账号读取凭据；Bot 请继续使用现有 Bot 接入页。')
        return dict(user_id=identifier(user.get('id')), username=user.get('global_name') or user.get('username','本人'))

    def readable_channels(self, guild, user_id):
        try: return super().readable_channels(guild, user_id)
        except ValueError as error: raise ValueError(str(error).replace('Bot', '当前账号')) from None

    def message(self, channel, message_id):
        page = self.get(f'/channels/{identifier(channel)}/messages', dict(around=identifier(message_id), limit=1))
        if not isinstance(page, list): raise ValueError('引用原文响应异常，未采用。')
        return next((row for row in page if row.get('id') == message_id and row.get('channel_id') == channel), None)


class Exporter:
    def __init__(self, app):
        self.app = app
        self.jobs = {}
        self.lock = threading.Lock()
        self.account_lock = threading.RLock()
        self.process = None

    def state(self):
        value = self.app.meta('exporter-account', {})
        enabled = value.get('enabled') is True
        return dict(value, enabled=enabled, status=value.get('status', 'saved') if enabled else 'disabled',
                    has_token=bool(value.get('has_token')), storage=self.app.vault.storage)

    def save(self, data):
        if (not isinstance(data, dict) or not data.keys() <= {'token', 'clear_token', 'enabled'}
                or any(type(data.get(key, False)) is not bool for key in ('clear_token', 'enabled'))):
            raise ValueError('请在界面配置读取账号。')
        token = data.get('token','')
        if not isinstance(token, str) or len(token) > 4096 or any(c.isspace() for c in token): raise ValueError('读取凭据格式无效，请只填写凭据本身。')
        with self.account_lock:
            account = self.app.meta('exporter-account', {})
            previous = account.get('enabled') is True
            enabled = data.get('enabled', previous)
            if token and not enabled:
                raise ValueError('请阅读账号风险提示，再主动启用个人账号读取。')
            credential_changed = data.get('clear_token') or bool(token)
            if credential_changed:
                if data.get('clear_token'): self.app.vault.delete('exporter-user')
                else: self.app.vault.set('exporter-user', token)
                account = dict(has_token=bool(token and not data.get('clear_token')), status='saved')
            if credential_changed or enabled != previous:
                self.app.save_meta('exporter-account', dict(account, enabled=enabled, revision=uuid.uuid4().hex))
                if self.process is not None and self.process.poll() is None:
                    self.process.kill()
        return self.state()

    def require_enabled(self):
        if not self.state()['enabled']:
            raise ValueError('个人账号读取已关闭，请在风险提示下主动启用。')

    def client(self, state, token):
        self.unchanged(state, token)
        if not token: raise ValueError('请先在读取设置中保存个人账号凭据。')
        return AccountClient(token, lambda: self.unchanged(state, token))

    def unchanged(self, state, token):
        with self.account_lock:
            self.require_enabled()
            if (self.state().get('revision') != state.get('revision')
                    or self.app.vault.get('exporter-user') != token):
                raise ValueError('读取账号已变化，本次结果已丢弃，请重新连接并选择范围。')

    def connect(self):
        with self.account_lock:
            self.require_enabled()
            state, token = self.state(), self.app.vault.get('exporter-user')
        client = self.client(state, token); identity = client.identity(); servers = client.guilds()
        with self.account_lock:
            self.unchanged(state, token)
            self.app.save_meta('exporter-account', dict(enabled=True, has_token=True, status='connected',
                revision=uuid.uuid4().hex, **identity, servers=servers, channels={}))
        return self.state()

    def channels(self, server):
        identifier(server)
        with self.account_lock:
            self.require_enabled()
            state, token = self.state(), self.app.vault.get('exporter-user')
        if state.get('status') != 'connected' or server not in state.get('servers', {}): raise ValueError('请先连接账号，再选择服务器。')
        client = self.client(state, token); identity = client.identity()
        if identity['user_id'] != state['user_id']: raise ValueError('读取账号已变化，请重新连接。')
        channels = client.readable_channels(server, identity['user_id'])
        with self.account_lock:
            self.unchanged(state, token)
            known = dict(self.state().get('channels', {})); known[server] = channels
            self.app.save_meta('exporter-account', {k:v for k,v in dict(state, channels=known).items() if k != 'storage'})
        return dict(server=server, channels=channels)

    def prepare(self, data):
        if (not isinstance(data,dict) or not {'server','channel'} <= data.keys()
                or not data.keys() <= {'server','channel','date','timezone','start_time','end_time'}): raise ValueError('请选择服务器、频道和时间范围。')
        zone = data.get('timezone','Asia/Shanghai'); tz = checked_timezone(zone)
        from .time_ranges import minute_window, describe_window
        try:
            precise = bool(data.get('start_time') or data.get('end_time'))
            if precise:
                start, end = minute_window(data.get('start_time',''), data.get('end_time',''), tz)
            else:
                day = date.fromisoformat(data.get('date',''))
                start = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
                end = datetime.combine(day+timedelta(days=1), time.min, tz).astimezone(timezone.utc)
        except (TypeError,ValueError,OverflowError): raise ValueError('请选择有效日期或起止时间；结束须晚于开始，避免夏令时重复或不存在的时间。') from None
        window = describe_window(start,end,tz,zone,precise)
        with self.account_lock:
            self.require_enabled()
            state, token = self.state(), self.app.vault.get('exporter-user')
        if data['channel'] not in state.get('channels',{}).get(data['server'],{}): raise ValueError('请先发现当前账号可读的频道，不允许跨范围读取。')
        if not self.lock.acquire(blocking=False): raise ValueError('已有读取任务，请等待完成。')
        try:
            client = self.client(state, token); identity = client.identity()
            if identity['user_id'] != state['user_id']: raise ValueError('读取账号已变化，请重新连接。')
            client.check_channel(data['server'], data['channel'], identity['user_id'])
            binary = ensure_binary(self.app.path.parent)
            with tempfile.TemporaryDirectory(prefix='.export-', dir=self.app.file_imports.directory) as folder:
                output = Path(folder) / 'records.json'
                def launch(*args, **kwargs):
                    with self.account_lock:
                        self.unchanged(state, token)
                        self.process = subprocess.Popen(*args, **kwargs)
                        return self.process

                run_export(binary, token, data['channel'], start.isoformat(), end.isoformat(), output, launch=launch)
                self.unchanged(state, token)
                text = output.read_text(encoding='utf-8-sig')
                metadata, rows = parse_export(text, '频道记录.json')
                if metadata['server'] != data['server'] or metadata['channel'] != data['channel']:
                    raise ValueError('导出结果范围与选择不符，已拒绝使用。')
                if any(not start <= datetime.fromisoformat(row['created']) < end for row in rows):
                    raise ValueError('导出记录超出所选时间范围，已拒绝使用。')
                # 原导出器提供主记录；窗口外引用沿用现有只读客户端补读，绝不跨频道。
                value = json.loads(text)
                known = {row['id'] for row in rows}
                pending = [(row, 0) for row in rows]
                while pending:
                    row, depth = pending.pop()
                    mid = row.get('reference_id')
                    if (not mid or mid in known or depth >= 6 or row.get('reference_channel') != data['channel']
                            or row.get('reference_server') != data['server']): continue
                    known.add(mid)
                    try: original = client.message(data['channel'], mid)
                    except ValueError: continue
                    if not original: continue
                    if original.get('guild_id', data['server']) != data['server']: continue
                    ref = original.get('message_reference') or {}
                    exported = dict(id=original['id'], type='Reply' if original.get('type') == 19 else 'Default' if original.get('type',0) == 0 else 'System',
                        timestamp=original['timestamp'], timestampEdited=original.get('edited_timestamp'), content=original.get('content',''),
                        author=dict(id=original['author']['id'], name=original['author'].get('global_name') or original['author']['username'], isBot=bool(original['author'].get('bot'))),
                        reference=dict(messageId=ref.get('message_id'), channelId=ref.get('channel_id'), guildId=ref.get('guild_id')) if ref else None)
                    value['messages'].append(exported)
                    _, normalized = parse_export(dict_to_export(metadata, exported), '引用.json')
                    pending.extend((extra,depth+1) for extra in normalized)
                value['messageCount'] = len(value['messages'])
                with self.account_lock:
                    self.unchanged(state, token)
                    item = self.app.file_imports.add(dict(filename=f'{window["start_date"]}-频道记录.json', content=json.dumps(value,ensure_ascii=False)),
                        origin=dict(kind='account-export', account_id=identity['user_id'], date=window['start_date'], timezone=zone,
                                    start_time=window['start_time'],end_time=window['end_time'],window=window))
                    self.app.account_data.ingest(item)
            return self.app.file_imports.inspect(item['id'], zone)
        finally:
            with self.account_lock:
                self.process = None
            self.lock.release()

    def queue(self, data):
        self.require_enabled()
        job = dict(id=uuid.uuid4().hex, status='running', note='正在读取所选频道；首次使用会自动准备读取组件。')
        self.jobs[job['id']] = job
        def work():
            try: job.update(status='complete', item=self.prepare(data), note='记录已就绪，可以生成报告。')
            except ValueError as error: job.update(status='failed', note=str(error))
            except Exception: job.update(status='failed', note='读取未完成，请检查配置或联系维护者；没有调用模型。')
        threading.Thread(target=work, daemon=True).start()
        return dict(job)

    def job(self, job_id):
        if not isinstance(job_id,str) or job_id not in self.jobs: raise ValueError('读取任务不存在，请重新读取。')
        return dict(self.jobs[job_id])


def dict_to_export(metadata, message):
    return json.dumps(dict(guild=dict(id=metadata['server'],name=metadata['server_name']),
        channel=dict(id=metadata['channel'],name=metadata['channel_name'],type='GuildTextChat'), messages=[message]), ensure_ascii=False)
