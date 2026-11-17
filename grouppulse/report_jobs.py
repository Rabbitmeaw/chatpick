"""长报告的本机后台执行与短请求进度查询；实际生成仍调用已有入口。"""

import hashlib
import json
import re
import threading
import time
import uuid


class ReportJobs:
    def __init__(self, app):
        self.app, self.lock, self.owner = app, threading.RLock(), uuid.uuid4().hex

    def queue(self, data):
        fields={'server','channel','date','end_date','start_time','end_time','kind','member','topic','period','mode','focus_member','focus_topic'}
        if (not isinstance(data,dict) or data.keys()!={'id','source','request'}
                or not isinstance(data['id'],str) or not re.fullmatch('[a-f0-9]{32}',data['id'])
                or data['source'] not in ('account','bot') or not isinstance(data['request'],dict)):
            raise ValueError('报告任务格式有误，请通过页面生成。')
        source, request=data['source'], data['request']
        allowed=fields | ({'import_id','timezone'} if source=='account' else set())
        if (not {'server','channel'}<=request.keys() or not request.keys()<=allowed
                or not all(isinstance(value,str) for value in request.values()) or request.get('kind')=='activity'):
            raise ValueError('报告任务范围有误，请通过页面选择。')
        fingerprint=hashlib.sha256(json.dumps(dict(source=source,request=request),sort_keys=True).encode()).hexdigest()
        with self.lock:
            key='report-job:'+data['id']
            old=self.app.meta(key)
            if old:
                if old['fingerprint']!=fingerprint: raise ValueError('任务与原报告范围不符。')
                return self.get(data['id'],request['server'],request['channel'])
            if source=='account':
                reader=self.app.file_imports.reader(request.get('import_id',''),request.get('timezone','Asia/Shanghai'))
                if request['server']!=reader.metadata['server'] or request['channel']!=reader.metadata['channel']:
                    raise ValueError('报告范围与记录不符。')
            elif request['channel'] not in self.app.server_channels(request['server']):
                raise ValueError('请选择当前服务器中的频道。')
            state=dict(id=data['id'],source=source,server=request['server'],channel=request['channel'],
                       fingerprint=fingerprint,owner=self.owner,status='running',started=time.time(),
                       model_wait_seconds=self.app.get_config()['timeout'])
            self.app.save_meta(key,state)

        def run():
            try:
                report=self.app.file_imports.report(request) if source=='account' else self.app.report(**request)
                result=dict(status='complete',report_id=report['report_id'],import_id=request.get('import_id',''))
            except ValueError as error:
                result=dict(status='failed',error=str(error))
            except Exception:
                result=dict(status='failed',error='生成未完成，结果可能未知；未自动重发模型请求，请先查看历史。')
            with self.lock:
                self.app.save_meta(key,dict(state,**result,finished=time.time()))
        threading.Thread(target=run,daemon=True).start()
        return self.get(data['id'],request['server'],request['channel'])

    def get(self, job_id, server, channel):
        if not isinstance(job_id,str) or not re.fullmatch('[a-f0-9]{32}',job_id):
            raise ValueError('报告任务不存在。')
        with self.lock:
            key='report-job:'+job_id
            state=self.app.meta(key)
            if not state or state['server']!=server or state['channel']!=channel:
                raise ValueError('报告任务与当前服务器或频道不符。')
            if state['status']=='running' and state['owner']!=self.owner:
                state.update(status='interrupted',finished=time.time(),error='本机服务已重启，未自动重发模型请求；请先查看报告历史。')
                self.app.save_meta(key,state)
        result={key:value for key,value in state.items() if key not in ('fingerprint','owner')}
        result['elapsed_seconds']=max(0,int(state.get('finished',time.time())-state['started']))
        if state['status']=='complete':
            if state['source']=='account':
                result['result']=self.app.file_imports.saved(state['import_id'],server,channel,state['report_id'])
            else:
                with self.app.db() as db:
                    row=db.execute('SELECT body FROM report_snapshots WHERE id=?',(state['report_id'],)).fetchone()
                if not row: raise ValueError('保存的报告不存在，请查看历史。')
                report=json.loads(row['body'])
                if report['server']!=server or report['channel']!=channel: raise ValueError('保存的报告范围不符。')
                result['result']=report
        return result
