"""报告模型响应的本机缓存；整理失败后的同输入重试不再次计费。"""

import hashlib
import json
from datetime import datetime, timezone


class CachedReportModel:
    def __init__(self, model, app):
        self.model, self.app, self.hits = model, app, 0
        with app.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS report_model_responses (id TEXT PRIMARY KEY, created TEXT, body TEXT)')

    @property
    def input_budget(self): return self.model.input_budget

    @property
    def calls(self): return self.model.calls

    def complete(self, messages):
        config=self.model.config() if callable(getattr(self.model,'config',None)) else {}
        parameters={k:config.get(k) for k in ('base_url','model','protocol','temperature','max_tokens','json_mode','output_parameter')}
        key=hashlib.sha256(json.dumps([parameters,messages],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with self.app.db() as db:
            row=db.execute('SELECT body FROM report_model_responses WHERE id=?',(key,)).fetchone()
        if row:
            self.hits+=1
            return json.loads(row['body'])
        raw=self.model.complete(messages)
        # 在任何解析、身份或出处整理之前落盘；凭据不写入。
        with self.app.db() as db:
            db.execute('INSERT OR IGNORE INTO report_model_responses VALUES (?,?,?)',
                       (key,datetime.now(timezone.utc).isoformat(),json.dumps(raw,ensure_ascii=False)))
        return raw
