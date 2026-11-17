"""本工作区的 macOS 登录后台服务；不接触其他应用或凭据。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

from .core import ROOT


WORKSPACE = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16]


def runtime_path():
    return Path(os.environ.get('CHATPICK_DATA_DIR',str(ROOT/'runtime'))).expanduser().resolve()


def workspace_id():
    directory=runtime_path()
    return WORKSPACE if directory==(ROOT/'runtime').resolve() else hashlib.sha256(('data:'+str(directory)).encode()).hexdigest()[:16]


def service_command(port):
    command=[sys.executable,'--serve'] if getattr(sys,'frozen',False) else [sys.executable,'-m','grouppulse']
    return command+['--port',str(port),'--data-dir',str(runtime_path())]


class ServiceLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def acquire(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open('a+')
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close(); self.handle = None
            return False
        os.chmod(self.path, 0o600)
        return True

    def close(self):
        if self.handle:
            self.handle.close(); self.handle = None


def label(port=8765):
    return f'com.chatpick.local.{workspace_id()}.{port}'


def agent_path(port=8765):
    return Path.home() / 'Library' / 'LaunchAgents' / (label(port) + '.plist')


def specification(port=8765):
    return dict(Label=label(port), ProgramArguments=service_command(port),
                WorkingDirectory=str(ROOT), RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                EnvironmentVariables=dict(CHATPICK_BACKGROUND='1', PYTHONUNBUFFERED='1'),
                StandardOutPath=str(runtime_path() / 'background.out.log'),
                StandardErrorPath=str(runtime_path() / 'background.error.log'), Umask=0o077)


def info(app=None, port=8765):
    installed = False
    if sys.platform == 'darwin' and agent_path(port).is_file():
        try:
            installed = plistlib.loads(agent_path(port).read_bytes()).get('WorkingDirectory') == str(ROOT)
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    return dict(app='GroupPulse', workspace=workspace_id(), pid=os.getpid(),data_dir=str(runtime_path()),
                supported=sys.platform == 'darwin', installed=installed,
                managed=os.environ.get('CHATPICK_BACKGROUND') == '1',
                generating=bool(app and app.generation_lock.locked()))


def probe(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/background', timeout=2) as response:
            value = json.load(response)
        if value.get('app') == 'GroupPulse' and value.get('workspace') == workspace_id():
            return value
    except Exception:
        pass
    return None


def control(*args, check=True):
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True, timeout=15)
    if check and result.returncode:
        raise ValueError('后台服务未能启动，请重新双击启动拾聊；仍失败时请联系维护者检查登录服务权限。')
    return result


def stop_foreground(state):
    pid = state.get('pid')
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise ValueError('无法确认旧服务身份，未停止任何进程。')
    command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=3).stdout
    cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True, timeout=3).stdout
    owned_command=(sys.executable in command and '--serve' in command) if getattr(sys,'frozen',False) else '-m grouppulse' in command
    if not owned_command or f'n{ROOT}\n' not in cwd:
        raise ValueError('旧服务路径不匹配，未停止其他程序。')
    if state.get('generating'):
        raise ValueError('当前正在生成报告，请完成后再开启登录后台运行。')
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        state=subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],capture_output=True,text=True,timeout=3).stdout.strip()
        if not state or state.startswith('Z'):
            return
        time.sleep(.2)
    raise ValueError('旧服务仍在退出，请稍后重试；未强制终止进程。')


def verify_agent(port):
    path = agent_path(port)
    if path.is_symlink():
        raise ValueError('后台服务文件不是本应用的普通文件，未覆盖。')
    if path.exists():
        previous = plistlib.loads(path.read_bytes())
        if previous.get('Label') != label(port) or previous.get('WorkingDirectory') != str(ROOT):
            raise ValueError('同名登录任务不属于本工作区，未覆盖。')
    return path


def start(port=8765, open_browser=False):
    if sys.platform != 'darwin':
        raise ValueError('一键登录后台运行目前支持 macOS；其他设备请由维护者配置常驻服务。')
    os.umask(0o077)
    url = f'http://127.0.0.1:{port}/'
    current = probe(port)
    if current and current['managed']:
        if open_browser:
            webbrowser.open(url)
        return
    # A foreign listener must be left alone, including a different workspace.
    if not current:
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('端口被其他程序使用，未停止或替换它，请联系维护者。')
    path = verify_agent(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path().mkdir(mode=0o700,parents=True,exist_ok=True)
    staged = path.with_suffix(f'.{os.getpid()}.tmp')
    try:
        with staged.open('xb') as handle:
            plistlib.dump(specification(port), handle)
        os.chmod(staged, 0o600)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    if current:
        stop_foreground(current)
    domain = f'gui/{os.getuid()}'
    target = domain + '/' + label(port)
    control('enable', target)
    if control('print', target, check=False).returncode == 0:
        control('kickstart', target)
    else:
        control('bootstrap', domain, str(path))
    for _ in range(80):
        state = probe(port)
        if state and state['managed']:
            if open_browser:
                webbrowser.open(url)
            return
        time.sleep(.25)
    raise ValueError('后台启动尚未完成，请稍后重新打开拾聊；日志位于本应用 runtime 目录。')


def stop(port=8765):
    if sys.platform != 'darwin':
        raise ValueError('此停止入口目前支持 macOS。')
    path = verify_agent(port)
    target = f'gui/{os.getuid()}/' + label(port)
    loaded = control('print', target, check=False).returncode == 0
    if loaded:
        control('bootout', target)
    path.unlink(missing_ok=True)
    current = probe(port)
    if current and not current['managed']:
        stop_foreground(current)


def request_start(port):
    if sys.platform != 'darwin':
        raise ValueError('一键登录后台运行目前支持 macOS。')
    command=[sys.executable,'--background'] if getattr(sys,'frozen',False) else [sys.executable,'-m','grouppulse.background']
    subprocess.Popen(command+['start','--port',str(port),'--data-dir',str(runtime_path())],
                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return dict(status='starting', note='正在开启后台运行，页面稍后会恢复；无需保留终端窗口。')


def main():
    parser = argparse.ArgumentParser(description='拾聊后台运行管理')
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data-dir', help='本机数据目录；资源仍从程序包读取')
    args = parser.parse_args()
    if args.data_dir: os.environ['CHATPICK_DATA_DIR']=str(Path(args.data_dir).expanduser().resolve())
    if not 1024 <= args.port <= 65535:
        parser.error('端口应在 1024 至 65535 之间。')
    try:
        if args.action == 'start':
            start(args.port, args.open)
            print('拾聊正在后台运行，可关闭终端窗口；下次登录后自动启动。')
        elif args.action == 'stop':
            stop(args.port)
            print('拾聊后台已停止；配置、凭据和历史记录保留。')
        else:
            print(json.dumps(probe(args.port) or dict(status='stopped'), ensure_ascii=False))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, ValueError) else '后台操作未完成，请联系维护者检查权限和运行环境。')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
