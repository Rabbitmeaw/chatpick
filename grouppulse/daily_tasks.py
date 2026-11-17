"""持久每日任务；生成直接调用 LiveApp.report，不修改总结核心。"""

import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta

from .core import TZ
from .network import ServiceError


class DailyTasks:
    def __init__(self, app, clock=None):
        self.app = app
        self.clock = clock or (lambda: datetime.now(TZ))
        self.worker = threading.Lock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.thread = None
        with app.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS daily_tasks (
                    id TEXT PRIMARY KEY, server TEXT, channel TEXT, body TEXT,
                    UNIQUE(server,channel));
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id TEXT PRIMARY KEY, task_id TEXT, day TEXT, status TEXT, body TEXT,
                    UNIQUE(task_id,day));
            ''')

    def scope(self, server, channel):
        if not isinstance(channel, str) or channel not in self.app.server_channels(server):
            raise ValueError('请选择当前服务器中的可读频道。')

    def find_task(self, server, channel):
        with self.app.db() as db:
            row = db.execute('SELECT body FROM daily_tasks WHERE server=? AND channel=?', (server, channel)).fetchone()
        return json.loads(row['body']) if row else None

    def write_task(self, task):
        with self.app.db() as db:
            db.execute('INSERT OR REPLACE INTO daily_tasks VALUES (?,?,?,?)',
                       (task['id'], task['server'], task['channel'], json.dumps(task, ensure_ascii=False)))

    def write_run(self, run):
        run['updated'] = self.clock().isoformat()
        with self.app.db() as db:
            db.execute('INSERT OR REPLACE INTO daily_runs VALUES (?,?,?,?,?)',
                       (run['id'], run['task_id'], run['date'], run['status'], json.dumps(run, ensure_ascii=False)))

    def save(self, data):
        if not isinstance(data, dict) or not {'server', 'channel', 'members'} <= data.keys() or not data.keys() <= {'server', 'channel', 'members', 'run_time', 'enabled', 'timezone', 'instructions'}:
            raise ValueError('请通过每日总结表单保存设置。')
        server, channel = data['server'], data['channel']
        self.scope(server, channel)
        members, run_time, enabled = data['members'], data.get('run_time', '00:10'), data.get('enabled', not bool(getattr(self.app,'account_scope',None)))
        if not isinstance(members, list) or any(not isinstance(person, str) for person in members) or len(set(members)) != len(members):
            raise ValueError('请选择不重复的关注成员。')
        if not isinstance(enabled, bool) or not isinstance(run_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', run_time):
            raise ValueError('每日时间应为有效的小时和分钟。')
        old = self.find_task(server, channel)
        instructions=data.get('instructions',(old or {}).get('instructions',''))
        if not isinstance(instructions,str) or len(instructions)>2000:
            raise ValueError('固定总结要求最多2000字，请只填写文字。')
        from .file_imports import checked_timezone
        zone_name=data.get('timezone',(old or {}).get('timezone','Asia/Shanghai'))
        zone=checked_timezone(zone_name)
        if not getattr(self.app,'account_scope',None) and zone_name!='Asia/Shanghai':
            raise ValueError('当前Bot每日任务按北京时间统计；个人任务可选择统计时区。')
        names = self.app.bootstrap(server, channel, remember=False)['members']
        known = {**((old or {}).get('member_names', {})), **names}
        if any(person not in known for person in members):
            raise ValueError('请选择当前频道已同步的成员。')
        if enabled:
            self.app.sync_bot(server, channel)
            config = self.app.get_config()
            if not config.get('has_key') or not config.get('base_url') or not config.get('model'):
                raise ValueError('请先到模型设置保存服务地址、模型名称和密钥。')
        now = self.clock().astimezone(zone)
        first = now.date() - timedelta(days=1) if now.strftime('%H:%M') < run_time else now.date()
        task = dict(id=(old or {}).get('id') or hashlib.sha256(f'{server}\0{channel}'.encode()).hexdigest()[:32],
                    server=server, channel=channel, members=members, member_names={p: known[p] for p in members},
                    run_time=run_time, timezone=zone_name, instructions=instructions, enabled=enabled,
                    next_day=(old or {}).get('next_day', first.isoformat()), updated=now.isoformat())
        if old and not old['enabled'] and enabled:
            task['next_day'] = first.isoformat()  # Paused dates are deliberately skipped.
        with self.app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT body FROM daily_tasks WHERE id=?', (task['id'],)).fetchone()
            if latest:
                current = json.loads(latest['body'])
                if current['enabled'] or not enabled:
                    task['next_day'] = current['next_day']
            db.execute('INSERT OR REPLACE INTO daily_tasks VALUES (?,?,?,?)',
                       (task['id'], server, channel, json.dumps(task, ensure_ascii=False)))
        self.wake.set()
        return task

    @staticmethod
    def due(task):
        from .file_imports import checked_timezone
        following = date.fromisoformat(task['next_day']) + timedelta(days=1)
        return datetime.fromisoformat(f'{following}T{task["run_time"]}:00').replace(tzinfo=checked_timezone(task.get('timezone','Asia/Shanghai')))

    def state(self, server, channel, before=''):
        self.scope(server, channel)
        task = self.find_task(server, channel)
        runs = []
        if before:
            try:
                if date.fromisoformat(before).isoformat() != before:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('历史日期无效。') from None
        if task:
            with self.app.db() as db:
                runs = [json.loads(row['body']) for row in db.execute(
                    'SELECT body FROM daily_runs WHERE task_id=? AND day<? ORDER BY day DESC LIMIT 21',
                    (task['id'], before or '9999-12-31'))]
        return dict(task=task, runs=runs[:20], next_before=runs[19]['date'] if len(runs) > 20 else '',
                    next_due=self.due(task).isoformat() if task and task['enabled'] else '', timezone=(task or {}).get('timezone',getattr(self.app,'report_timezone_name','Asia/Shanghai')))

    def ensure_run(self, task, day, manual=False):
        identifier = hashlib.sha256(f'{task["id"]}\0{day}'.encode()).hexdigest()[:32]
        steps = [dict(key='daily', label='频道日报', kind='daily', member='')]
        steps += [dict(key='member:' + person, label=task['member_names'][person], kind='member', member=person) for person in task['members']]
        for step in steps:
            step.update(status='pending', report_id=hashlib.sha256(f'{identifier}\0{step["key"]}'.encode()).hexdigest()[:32])
        run = dict(id=identifier, task_id=task['id'], server=task['server'], channel=task['channel'],
                   date=day, timezone=task.get('timezone','Asia/Shanghai'),instructions=task.get('instructions',''),status='pending', manual=manual, steps=steps, note='', updated=self.clock().isoformat())
        with self.app.db() as db:
            db.execute('INSERT OR IGNORE INTO daily_runs VALUES (?,?,?,?,?)',
                       (identifier, task['id'], day, 'pending', json.dumps(run, ensure_ascii=False)))
            run = json.loads(db.execute('SELECT body FROM daily_runs WHERE id=?', (identifier,)).fetchone()['body'])
        return run

    def queue(self, data):
        if not isinstance(data, dict) or not {'server', 'channel'} <= data.keys() or not data.keys() <= {'server', 'channel', 'date', 'retry'}:
            raise ValueError('请通过每日总结页面生成。')
        self.scope(data['server'], data['channel'])
        task = self.find_task(data['server'], data['channel'])
        if not task:
            raise ValueError('请先保存每日总结设置。')
        from .file_imports import checked_timezone
        today = self.clock().astimezone(checked_timezone(task.get('timezone','Asia/Shanghai'))).date()
        day = data.get('date', (today - timedelta(days=1)).isoformat())
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or date.fromisoformat(day) >= today:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('请选择已经结束的完整日期。') from None
        if not isinstance(data.get('retry', False), bool):
            raise ValueError('继续生成选项无效。')
        run = self.ensure_run(task, day, manual=True)
        with self.app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            run = json.loads(db.execute('SELECT body FROM daily_runs WHERE id=?', (run['id'],)).fetchone()['body'])
            if run['status'] == 'needs_attention':
                if not data.get('retry'):
                    raise ValueError('上次生成未完成；检查原因后点击“继续未完成项”，可能重新调用模型。')
                for step in run['steps']:
                    if step['status'] != 'complete':
                        step['status'] = 'pending'
                run.update(status='pending', note='', manual=True)
            elif run['status'] == 'pending':
                run['manual'] = True
                run.pop('retry_at', None)
            db.execute('UPDATE daily_runs SET status=?,body=? WHERE id=?',
                       (run['status'],json.dumps(run,ensure_ascii=False),run['id']))
        self.wake.set()
        return run

    def snapshot(self, run, step):
        with self.app.db() as db:
            row = db.execute('SELECT body FROM report_snapshots WHERE id=?', (step['report_id'],)).fetchone()
        if not row:
            return None
        report = json.loads(row['body'])
        if (report['server'], report['channel'], report['date'], report['kind']) != (run['server'], run['channel'], run['date'], step['kind']) or (step['member'] and report.get('subject', {}).get('id') != step['member']):
            raise ValueError('报告快照与每日任务范围不一致，请联系维护者检查。')
        return report

    def report(self, server, channel, report_id):
        self.scope(server, channel)
        task = self.find_task(server, channel)
        if task:
            with self.app.db() as db:
                rows = db.execute('SELECT body FROM daily_runs WHERE task_id=?', (task['id'],)).fetchall()
            for row in rows:
                run = json.loads(row['body'])
                for step in run['steps']:
                    if step['report_id'] == report_id and step['status'] == 'complete':
                        value = self.snapshot(run, step)
                        if value:
                            return value
        raise ValueError('报告不属于当前频道，或尚未生成完成。')

    def advance(self, run):
        if not run['manual']:
            following = (date.fromisoformat(run['date']) + timedelta(days=1)).isoformat()
            with self.app.db() as db:
                db.execute("UPDATE daily_tasks SET body=json_set(body,'$.next_day',?) "
                           "WHERE id=? AND json_extract(body,'$.next_day')<=?", (following, run['task_id'], run['date']))

    def recover(self):
        # Called only after this workspace's service instance lock is acquired.
        with self.app.db() as db:
            runs = [json.loads(row['body']) for row in db.execute('SELECT body FROM daily_runs WHERE status=?', ('running',))]
        for run in runs:
            for step in run['steps']:
                if self.snapshot(run, step):
                    step['status'] = 'complete'
                elif step['status'] == 'running':
                    step['status'] = 'uncertain'
            complete = all(step['status'] == 'complete' for step in run['steps'])
            uncertain = any(step['status'] == 'uncertain' for step in run['steps'])
            run.update(status='complete' if complete else 'needs_attention' if uncertain else 'pending',
                       note='上次服务中断，部分模型结果未知；已完成报告保留，请检查后继续未完成项。' if uncertain else '')
            self.write_run(run)
            if complete:
                self.advance(run)

    def tick(self, now=None):
        if self.stopped.is_set() or not self.worker.acquire(blocking=False):
            return False
        try:
            if hasattr(self.app,'account_data'):
                self.app.account_data.tick(now)
            if self.app.generation_lock.locked():
                return False
            moment = (now or self.clock()).astimezone(TZ)
            with self.app.db() as db:
                tasks = [json.loads(row['body']) for row in db.execute('SELECT body FROM daily_tasks ORDER BY id')]
                pending = [json.loads(row['body']) for row in db.execute('SELECT body FROM daily_runs WHERE status=? ORDER BY day,id', ('pending',))]
            enabled = {t['id'] for t in tasks if t['enabled']}
            ready = lambda r: not r.get('retry_at') or datetime.fromisoformat(r['retry_at']) <= moment
            run = next((r for r in pending if (r['manual'] or r['task_id'] in enabled) and ready(r)), None)
            if not run:
                for task in sorted(tasks, key=lambda item: item['next_day']):
                    if task['enabled'] and self.due(task) <= moment:
                        run = self.ensure_run(task, task['next_day'])
                        if run['status'] == 'complete':
                            # A manual preview may already have produced this day.
                            run['manual'] = False
                            self.write_run(run)
                            self.advance(run)
                            return True
                        if run['status'] == 'pending' and ready(run):
                            break
                        run = None
            if not run:
                return False
            self.execute(run)
            return True
        finally:
            self.worker.release()

    def execute(self, run):
        with self.app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            run = json.loads(db.execute('SELECT body FROM daily_runs WHERE id=?', (run['id'],)).fetchone()['body'])
            if run['status'] != 'pending':
                return
            run['status'] = 'running'
            run.pop('retry_at', None)
            db.execute('UPDATE daily_runs SET status=?,body=? WHERE id=?',
                       ('running',json.dumps(run,ensure_ascii=False),run['id']))
        before_calls = self.app.model.calls
        step = None
        try:
            context=self.app.for_timezone(run.get('timezone','Asia/Shanghai')) if hasattr(self.app,'for_timezone') else self.app
            context.ensure_synced(run['server'], run['channel'], run['date'], run['date'])
            start, end = context.window(run['date'])
            if not context.covered(run['channel'], start, end):
                raise ValueError('当天记录或子线程尚未补齐，请检查 Bot 权限后继续。')
            for step in run['steps']:
                existing = self.snapshot(run, step)
                if existing:
                    step.update(status='complete', coverage=existing['coverage']['status'])
                    continue
                task = self.find_task(run['server'], run['channel'])
                if self.stopped.is_set() or (not run['manual'] and not task['enabled']):
                    run['status'] = 'pending'
                    self.write_run(run)
                    return
                step['status'] = 'running'
                self.write_run(run)
                before_calls = self.app.model.calls
                report = context.report(server=run['server'], channel=run['channel'], date=run['date'],
                                         kind=step['kind'], member=step['member'], instructions=run.get('instructions',''),_snapshot_id=step['report_id'], _synced=True)
                step.update(status='complete', coverage=report['coverage']['status'])
                self.write_run(run)
            run.update(status='complete', note='当天没有发言的成员也保存空摘要，不调用模型。', finished=self.clock().isoformat())
            self.write_run(run)
            self.advance(run)
        except Exception as error:
            if step is None and isinstance(error, ServiceError) and (error.code in ('connection','tls') or error.status == 429 or error.status >= 500):
                run.update(status='pending', retry_at=(self.clock()+timedelta(minutes=5)).isoformat(),
                           note='读取记录暂未完成，稍后自动继续；尚未为本次重试调用模型。')
                self.write_run(run)
                return
            if step and step['status'] == 'running':
                step['status'] = 'failed'
            run.update(status='needs_attention', note=str(error) if isinstance(error, ValueError) else '生成未完成，请检查连接后继续；已完成的报告保留。')
            run['model_calls'] = self.app.model.calls - before_calls
            self.write_run(run)

    def start(self, interval=15):
        if self.thread:
            return
        self.recover()
        def work():
            while not self.stopped.is_set():
                self.wake.clear()
                try:
                    again = self.tick()
                except Exception:
                    again = False
                self.wake.wait(1 if again else interval)
        self.thread = threading.Thread(target=work, daemon=True, name='chatpick-daily-reports')
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
