"""执行直接复制的 StarMemo JS，不在 Python 中重写整理算法。"""

import json
from pathlib import Path

from .generation import GenerationError

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
_SOURCES = tuple((VENDOR / name).read_text() for name in
                 ('jsonrepair/jsonrepair.min.js', 'starmemo/report-core.js'))


def call(name, *arguments, facts=None):
    """每次创建独立 JS 上下文，避免 HTTP 线程共享引擎或真实数据。"""
    if name not in ('buildGroupReportInput', 'parseGroupDailyReport', 'extractJson',
                    'postProcessReport', 'dedupeItems', 'createSignature'):
        raise ValueError('不支持的报告处理函数。')
    try:
        import quickjs
    except ImportError:
        raise GenerationError('缺少报告运行依赖，请使用“启动拾聊”入口或联系维护者安装 requirements.txt。') from None
    try:
        context = quickjs.Context()
        context.set_memory_limit(64 * 1024 * 1024)
        context.set_time_limit(5)
        context.set_max_stack_size(1024 * 1024)
        for source in _SOURCES:
            context.eval(source)
        context.set('_reportArguments', json.dumps(arguments, ensure_ascii=False))
        context.set('_reportFacts', json.dumps(facts, ensure_ascii=False))
        context.set('_reportFunction', name)
        context.eval('''
            globalThis.buildGroupReportFacts = async () => JSON.parse(_reportFacts);
            globalThis._reportResult = undefined;
            globalThis._reportFailed = false;
            Promise.resolve().then(() => StarMemoReportCore[_reportFunction](...JSON.parse(_reportArguments)))
                .then(value => { _reportResult = JSON.stringify(value); })
                .catch(() => { _reportFailed = true; });
        ''')
        while context.execute_pending_job():
            pass
        if context.eval('_reportFailed') or context.eval('_reportResult === undefined'):
            raise ValueError()
        return json.loads(context.eval('_reportResult'))
    except (ValueError, TypeError, quickjs.JSException):
        raise GenerationError('原报告整理函数未能解析本次结果，请检查模型输出；未改用模拟结果。') from None


def system_prompt():
    # 提取原常量供模型使用，同样来自实际执行的源码文件。
    import quickjs
    context = quickjs.Context()
    for source in _SOURCES:
        context.eval(source)
    return context.eval('StarMemoReportCore.GROUP_REPORT_SYSTEM_PROMPT')
