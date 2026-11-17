"""当前频道的只读后台同步；不调用模型，不向 Discord 写入。"""

import json
import threading
from datetime import datetime, timedelta

from .core import TZ


class AutoSync:
    def __init__(self, app):
        self.app=app
        self.target=app.meta('auto_sync',{})
        self.progress={}
        self.lock=threading.RLock()
        self.wake=threading.Event()
        self.stopped=threading.Event()
        self.thread=None

    def status(self):
        with self.lock:
            channel=self.target.get('channel','')
            return {'status':'waiting' if self.target.get('enabled',True) else 'paused',**self.progress,
                    'enabled':self.target.get('enabled',True),'server':self.target.get('server',''),
                    'channel':channel,'version':self.app.channel_version(channel) if channel else 0}

    def select(self, server, channel, enabled=None):
        bot=self.app.sync_bot(server,channel)
        with self.lock:
            same=(self.target.get('server'),self.target.get('channel'))==(server,channel)
            previous=self.app.sync_state(channel)
            today=datetime.now(TZ).date()
            start=self.target['start_date'] if same else (previous['start_date'] if previous else (today-timedelta(days=6)).isoformat())
            target=dict(server=server,channel=channel,bot_id=bot['id'],start_date=start,
                        enabled=self.target.get('enabled',True) if enabled is None else enabled)
            if target!=self.target:
                self.target=target
                self.progress=dict(status='waiting' if target['enabled'] else 'paused',
                    note='正在准备当前频道的自动同步。' if target['enabled'] else '后台同步已暂停；生成或提问时仍会补齐所选日期。')
                self.app.save_meta('auto_sync',target)
            if target['enabled']: self.wake.set()
        return self.status()

    def start(self, interval=15):
        if self.thread: return
        def run():
            while not self.stopped.is_set():
                self.wake.clear()
                again=self.tick()
                self.wake.wait(1 if again else interval)
        self.thread=threading.Thread(target=run,daemon=True,name='chatpick-channel-sync')
        self.thread.start()

    def stop(self):
        self.stopped.set();self.wake.set()

    def tick(self):
        with self.lock: target=dict(self.target)
        if not target.get('enabled') or not target.get('channel'): return False
        def current():
            with self.lock: return self.target==target and not self.stopped.is_set()
        def progress(state):
            if current():
                with self.lock: self.progress=dict(state)
        if not self.app.sync_lock.acquire(blocking=False): return False
        try:
            today=datetime.now(TZ).date().isoformat()
            old=self.app.sync_window_state(target['channel'],target['start_date'],today)
            if not old:
                start,end=self.app.window(target['start_date'],end_date=today)
                with self.app.db() as db:
                    row=db.execute('SELECT body FROM sync_windows WHERE channel=? AND start=? AND end<=? ORDER BY end DESC LIMIT 1',
                                   (target['channel'],start.isoformat(),end.isoformat())).fetchone()
                old=json.loads(row['body']) if row else None
            resume=bool(old and old['status']=='partial')
            cycle_end=old['end_date'] if resume else today
            after=old.get('after_id','') if resume else ''
            if old and old['status']=='complete':
                start,_end=self.app.window(target['start_date'],end_date=today)
                with self.app.db() as db:
                    row=db.execute("SELECT id FROM messages WHERE channel=? AND created>=? AND created<? "
                                   "AND coalesce(json_extract(body,'$.source_channel'),channel)=channel "
                                   "ORDER BY length(id) DESC,id DESC LIMIT 1",
                                   (target['channel'],start.isoformat(),old['effective_end'])).fetchone()
                after=row['id'] if row else ''
            result=self.app.sync_channel(target['bot_id'],target['server'],target['channel'],
                target['start_date'],cycle_end,max_pages=10,resume=resume,progress=progress,
                _after_id=after,_cancel=lambda:not current(),_incremental=bool(old and old['status']=='complete'),_previous=old)
            return (result['status']=='partial' or cycle_end<today) and current()
        except ValueError as error:
            if current():
                with self.lock: self.progress=dict(status='failed',note=str(error))
            return False
        except Exception:
            if current():
                with self.lock: self.progress=dict(status='failed',note='后台同步暂未完成，将自动重试；可到 Bot 接入页检查连接。')
            return False
        finally:
            self.app.sync_lock.release()
