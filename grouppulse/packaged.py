"""macOS 独立包入口；程序资源只读，用户数据保存在包外。"""

import argparse
import json
import os
from pathlib import Path
import sys


def default_data_dir():
    return Path.home()/'Library'/'Application Support'/'ChatPick'


def main():
    arguments=sys.argv[1:]
    if arguments and arguments[0]=='--self-check':
        import platform
        import quickjs
        from .core import ROOT
        required=('web/index.html','web/discord-format.js','fixtures/demo.json',
                  'vendor/starmemo/report-core.js','vendor/starmemo/query/query-core.js',
                  'vendor/chat-digest/classifier.py','vendor/discordchatexporter/LICENSE')
        missing=[name for name in required if not (ROOT/name).is_file()]
        context=quickjs.Context()
        from .reference_report import system_prompt
        from .reference_questions import QueryRuntime
        report_core_ok=bool(system_prompt())
        query_core_ok=len(QueryRuntime().references('self-check',['1']))==1
        print(json.dumps(dict(packaged=bool(getattr(sys,'frozen',False)),architecture=platform.machine(),resources_ok=not missing,
                              missing=missing,quickjs=context.eval('21*2'),report_core_ok=report_core_ok,query_core_ok=query_core_ok,
                              data_dir=str(default_data_dir()),resource_root=str(ROOT)),ensure_ascii=False))
        raise SystemExit(1 if missing else 0)
    os.environ.setdefault('CHATPICK_DATA_DIR',str(default_data_dir()))
    if arguments and arguments[0] in ('--serve','--background'):
        sys.argv=[sys.argv[0],*arguments[1:]]
        if arguments[0]=='--serve':
            from .__main__ import main as run
        else:
            from .background import main as run
        return run()
    parser=argparse.ArgumentParser(description='拾聊 ChatPick')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',default=os.environ['CHATPICK_DATA_DIR'])
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args(arguments)
    if not 1024<=args.port<=65535: parser.error('端口应在1024至65535之间。')
    os.environ['CHATPICK_DATA_DIR']=str(Path(args.data_dir).expanduser().resolve())
    from .background import start
    try:
        start(args.port,not args.no_open)
    except (ValueError,OSError) as error:
        # Finder 启动时没有终端，将实际错误保留给使用者和维护者。
        from .background import runtime_path
        runtime_path().mkdir(mode=0o700,parents=True,exist_ok=True)
        notice=runtime_path()/'启动说明.txt'
        notice.write_text('拾聊未能启动。\n'+str(error)+'\n未停止或替换其他服务。\n',encoding='utf-8')
        notice.chmod(0o600)
        if not args.no_open:
            import webbrowser
            webbrowser.open(notice.as_uri())
        raise SystemExit(1) from None


if __name__=='__main__': main()
