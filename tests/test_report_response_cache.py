import json
import tempfile
import unittest
from pathlib import Path

from grouppulse.core import App
from grouppulse.live_generation import generate_report
from grouppulse.report_response_cache import CachedReportModel


class ResponseCacheTests(unittest.TestCase):
    def test_failed_parsing_keeps_paid_response_across_restart_and_reuses_it(self):
        class Model:
            input_budget=48*1024
            calls=0
            def complete(self,messages): self.calls+=1; return '返回了不能解析的内容'
        row=dict(id='1',author_id='one',name='甲',created='2026-10-21T01:00:00+00:00',content='聊旅行。',bot=False)
        with tempfile.TemporaryDirectory() as path:
            app=App(Path(path)/'data.db'); model=Model()
            for _ in range(2):
                cached=CachedReportModel(model,App(app.path))
                with self.assertRaisesRegex(ValueError,'保存在本机'):
                    generate_report(cached,{},dict(kind='daily'),[row],[],'full')
            self.assertEqual(model.calls,1)
            with app.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM report_model_responses').fetchone()[0],1)

    def test_input_and_actual_model_parameters_define_response_identity(self):
        class Model:
            calls=0; input_budget=48000
            parameters=dict(model='example',timeout=90,max_tokens=8000)
            def config(self): return self.parameters
            def complete(self,messages): self.calls+=1; return json.dumps({'overview':'保留结果'})
        with tempfile.TemporaryDirectory() as path:
            app=App(Path(path)/'data.db'); model=Model(); cached=CachedReportModel(model,app)
            messages=[{'role':'user','content':'同一输入'}]
            self.assertEqual(cached.complete(messages),cached.complete(messages))
            model.parameters['timeout']=300
            cached.complete(messages); self.assertEqual(model.calls,1)
            model.parameters['model']='different'
            cached.complete(messages); self.assertEqual(model.calls,2)
            cached.complete([{'role':'user','content':'新的输入'}]); self.assertEqual(model.calls,3)
