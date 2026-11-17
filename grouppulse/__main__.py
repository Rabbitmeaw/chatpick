import argparse
import errno
import json
import os
from pathlib import Path
import signal
import threading
import webbrowser
from urllib.request import urlopen

from .core import App, ROOT
from .live import LiveApp
from .server import create_server
from .background import ServiceLock, probe, runtime_path


def main():
    parser = argparse.ArgumentParser(description='拾聊 ChatPick 本机只读分析')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='启动后打开本机网页')
    parser.add_argument('--demo',action='store_true',help='开发回归用合成样例；与真实数据分库')
    parser.add_argument('--data-dir',help='本机数据目录；不改变程序资源位置')
    args = parser.parse_args()
    if args.data_dir: os.environ['CHATPICK_DATA_DIR']=str(Path(args.data_dir).expanduser().resolve())
    if not 1024 <= args.port <= 65535:
        parser.error('端口应在 1024 至 65535 之间。')
    os.umask(0o077)
    url = f'http://127.0.0.1:{args.port}'
    instance_lock = ServiceLock(runtime_path() / ('offline.lock' if args.demo else 'live.lock'))
    if not instance_lock.acquire():
        if probe(args.port):
            print(f'拾聊已经在运行：{url}')
            if args.open:
                webbrowser.open(url)
            return
        raise SystemExit('拾聊已在运行或正在启动，请稍后再打开；没有重复启动任务。')
    app = App(runtime_path() / 'offline.sqlite3') if args.demo else LiveApp(runtime_path() / 'live.sqlite3')
    if args.demo: app.ensure_demo()
    version='offline-1' if args.demo else 'live-1'
    try:
        server = create_server(app, args.port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise SystemExit('无法启动本机服务：当前环境不允许监听本机端口，请联系维护者检查运行权限。') from None
        try:
            if not probe(args.port): raise ValueError('端口不属于此数据目录。')
            with urlopen(url + '/api/bootstrap', timeout=2) as response:
                state = json.load(response)
            if state.get('app') == 'GroupPulse' and state.get('version') == version:
                print(f'拾聊已经在运行：{url}')
                if args.open:
                    webbrowser.open(url)
                return
        except Exception:
            pass
        raise SystemExit('启动失败：端口被其他程序占用，请联系维护者协助。没有停止或替换其他服务。')
    mode='合成样例演示，不连接真实服务。' if args.demo else '真实接入 · Discord只读；请在界面配置Bot和模型，保存不会自动连接。'
    print(f'拾聊已启动：{url}\n{mode}', flush=True)
    if not args.demo:
        app.auto_sync.start()
        app.daily_tasks.start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    if args.open:
        threading.Timer(.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n拾聊已停止。')
    finally:
        if not args.demo:
            app.auto_sync.stop()
            app.daily_tasks.stop()
        server.server_close()
        instance_lock.close()


if __name__ == '__main__':
    main()
