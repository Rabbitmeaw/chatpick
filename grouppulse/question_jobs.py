"""沿用报告任务的持久状态/短轮询模式；回答、记忆和全文仍由原 ask 保存。"""

import hashlib
import json
import re
import threading
import time
import uuid


class QuestionJobs:
    def __init__(self, app):
        self.app, self.lock, self.owner = app, threading.RLock(), uuid.uuid4().hex

    def reader(self, values):
        source=values.get('source','bot')
        if source=='account':
            return self.app.account_data.reader(values.get('server',''),values.get('channel',''),
                values.get('account_id',''),values.get('timezone','Asia/Shanghai'))
        if source!='bot' or 'account_id' in values or 'timezone' in values:
            raise ValueError('问答读取身份有误，请重新选择当前频道。')
        if values.get('channel') not in self.app.server_channels(values.get('server','')):
            raise ValueError('请选择当前服务器中的频道。')
        return self.app

    def scope(self, values):
        return {key:values.get(key,default) for key,default in (
            ('source','bot'),('account_id',''),('server',''),('channel',''),('timezone','Asia/Shanghai'))}

    def queue(self, data):
        fields={'question','server','channel','start_date','end_date','conversation_id','source','account_id','timezone'}
        if (not isinstance(data,dict) or data.keys()!={'id','request'}
                or not isinstance(data['id'],str) or not re.fullmatch('[a-f0-9]{32}',data['id'])
                or not isinstance(data['request'],dict)):
            raise ValueError('问答任务格式有误，请通过页面提问。')
        request=dict(data['request'])
        if (not {'question','server','channel','start_date','end_date'}<=request.keys()
                or not request.keys()<=fields or not all(isinstance(value,str) for value in request.values())
                or not 1<=len(request['question'].strip())<=2000):
            raise ValueError('问答字段有误，请通过当前频道页面提问。')
        reader=self.reader(request)
        reader.window(request['start_date'],end_date=request['end_date'])
        if not request['start_date'] or not request['end_date']:
            raise ValueError('请同时选择开始和结束日期。')
        fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        key='question-job:'+data['id']
        with self.lock:
            old=reader.meta(key)
            if old:
                if old['fingerprint']!=fingerprint: raise ValueError('任务与原问题或范围不符。')
                return self.get(data['id'],request)
            state=dict(id=data['id'],scope=self.scope(request),request=request,fingerprint=fingerprint,
                owner=self.owner,status='running',started=time.time(),model_wait_seconds=self.app.get_config()['timeout'])
            reader.save_meta(key,state)

        def run():
            try:
                response=reader.ask(**{key:value for key,value in request.items() if key not in {'source','account_id','timezone'}})
                with reader.db() as db:
                    turn=db.execute("SELECT seq FROM question_turns WHERE conversation_id=? AND json_extract(body,'$.response.asked_at')=? ORDER BY seq DESC LIMIT 1",
                        (response['conversation_id'],response['asked_at'])).fetchone()
                if not turn: raise ValueError('回答已返回但历史尚未确认，请先查看以前的问答。')
                result=dict(status='complete',conversation_id=response['conversation_id'],turn_seq=turn['seq'])
            except ValueError as error:
                result=dict(status='failed',error=str(error))
            except Exception:
                result=dict(status='failed',error='问答未完成，结果可能未知；未自动重发模型请求，请先查看以前的问答。')
            with self.lock:
                current=reader.meta(key)
                if current and current['owner']==self.owner and current['status']=='running':
                    reader.save_meta(key,dict(state,**result,finished=time.time()))
        threading.Thread(target=run,daemon=True).start()
        return self.get(data['id'],request)

    def get(self, job_id, values):
        if not isinstance(job_id,str) or not re.fullmatch('[a-f0-9]{32}',job_id):
            raise ValueError('问答任务不存在。')
        reader=self.reader(values)
        with self.lock:
            key='question-job:'+job_id
            state=reader.meta(key)
            if not state or state['scope']!=self.scope(values):
                raise ValueError('问答任务与当前账号、服务器或频道不符。')
            if state['status']=='running' and state['owner']!=self.owner:
                state.update(status='interrupted',finished=time.time(),error='本机服务已重启，未自动重发模型请求；请先查看以前的问答，确认原问题是否已保存。')
                reader.save_meta(key,state)
        result={key:value for key,value in state.items() if key not in ('fingerprint','owner')}
        result['elapsed_seconds']=max(0,int(state.get('finished',time.time())-state['started']))
        if state['status']=='complete':
            history=reader.conversation(state['conversation_id'],state['scope']['server'],state['scope']['channel'],str(state['turn_seq']+1))
            turn=next((turn for turn in history['turns'] if turn['seq']==state['turn_seq']),None)
            if not turn: raise ValueError('保存的问答不存在，请查看以前的问答。')
            result['result']=turn['response']
        return result
