"""chat-digest 原单条分类函数的轻量宿主，不安装 pandas 或其他模型客户端。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .generation import GenerationError

PATH=Path(__file__).resolve().parents[1]/'vendor/chat-digest/classifier.py'
SOURCE=PATH.read_text()


def classify(model, topics, row, contexts):
    invalid=[]
    def call_llm(*,system_prompt,user_prompt,**_kwargs):
        messages=[dict(role='system',content=system_prompt),dict(role='user',content=user_prompt)]
        if len(json.dumps(messages,ensure_ascii=False).encode())>model.input_budget:
            raise GenerationError('分类原文及引用超出输入预算，本条尚未调用模型；请在模型高级设置调整预算。')
        return getattr(model,'complete_text',model.complete)(messages)
    host=dict(call_llm=call_llm,call_llm_messages=call_llm,print=lambda *args:invalid.append(True))
    exec(compile(SOURCE,str(PATH),'exec'),host)
    topic_list_str=''
    for i,topic in enumerate(topics):
        topic_list_str+=f'topic{i+1}: '+str(topic['title'])+'\n'
        topic_list_str+=f'topic{i+1}の説明: '+str(topic['summary'])+'\n'
    topic_list_str+=f'topic{len(topics)+1}:"その他"'
    # 原函数只读取 iloc；单条目标带稳定身份和已核验引用，不拼接其他线程的邻近消息。
    target=json.dumps(dict(message=row,referenceBackground=contexts),ensure_ascii=False)
    frame=SimpleNamespace(iloc=[dict(integrated_content=target)])
    index=host['fetch_chat_topic'](0,frame,topic_list_str,len(topics),host['PROMPTS'],
        'configured-model',0,use_prompt_cache=False)
    return dict(index=index,valid=not invalid and 1<=index<=len(topics)+1)


def cache_key(server,channel,topics,row,contexts):
    return hashlib.sha256(json.dumps([server,channel,topics,row,contexts],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
