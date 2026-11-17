#!/usr/bin/env python3
"""只用临时空数据验证独立包；不连接Discord、模型或现有8765服务。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,default=ROOT/'dist/ChatPick-macos-arm64/拾聊 ChatPick.app')
    parser.add_argument('--port',type=int,default=17865)
    args=parser.parse_args()
    if args.port==8765: parser.error('不能使用现有真实服务端口。')
    executable=args.app.resolve()/'Contents/MacOS/ChatPick'
    environment=dict(os.environ,PATH='/no-system-python')
    environment.pop('CHATPICK_DATA_DIR',None);environment.pop('CHATPICK_BACKGROUND',None)
    check=json.loads(subprocess.check_output([executable,'--self-check'],env=environment,text=True))
    assert check['packaged'] and check['resources_ok'] and check['report_core_ok'] and check['query_core_ok']
    resource_root=Path(check['resource_root'])
    url=f'http://127.0.0.1:{args.port}'
    def get(path):
        with urlopen(url+path,timeout=3) as response: return json.load(response)
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',args.port))==0: raise RuntimeError('测试端口已占用，未启动或停止其他服务。')
    proof=dict(self_check=check,system_python_on_path=False,model_calls=0,discord_messages=0)
    with tempfile.TemporaryDirectory(prefix='chatpick-package-') as temporary:
        directory=Path(temporary).resolve();data=directory/'data'
        command=[str(executable),'--serve','--port',str(args.port),'--data-dir',str(data)]
        process=None
        with (directory/'service.log').open('w') as output:
            def start():
                child=subprocess.Popen(command,cwd=resource_root,env=environment,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                for _ in range(100):
                    if child.poll() is not None: raise RuntimeError('独立包启动失败，请检查临时服务日志。')
                    try:
                        value=get('/api/background')
                        if value['pid']==child.pid: return child
                    except Exception: pass
                    time.sleep(.15)
                raise RuntimeError('独立包未在限时内就绪。')
            def stop(child):
                result=subprocess.run([str(executable),'--background','stop','--port',str(args.port),'--data-dir',str(data)],
                                      cwd=resource_root,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
                if result.returncode: raise RuntimeError((result.stdout+result.stderr).decode('utf-8',errors='replace'))
                child.wait(timeout=10)
            try:
                process=start();first=process.pid
                bootstrap=get('/api/bootstrap')
                assert bootstrap['version']=='live-1' and not bootstrap['servers']
                account=get('/api/exporter')
                assert account['enabled'] is False and account['has_token'] is False
                status=get('/api/background')
                assert status['data_dir']==str(data) and status['generating'] is False
                duplicate=subprocess.run(command,cwd=resource_root,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
                assert duplicate.returncode==0 and get('/api/background')['pid']==first
                foreign=subprocess.run(command[:-1]+[str(directory/'another-data')],cwd=resource_root,env=environment,
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
                assert foreign.returncode!=0 and get('/api/background')['pid']==first
                with sqlite3.connect(data/'live.sqlite3') as db:
                    assert db.execute('SELECT count(*) FROM bots').fetchone()[0]==0
                    assert db.execute('SELECT count(*) FROM daily_tasks').fetchone()[0]==0
                    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='report_deliveries'").fetchone() is None
                    db.execute('INSERT INTO live_meta VALUES (?,?)',('packaging-smoke-marker',json.dumps('retained')))
                with urlopen(url+'/discord-format.js',timeout=3) as response: assert b'discordReportParts' in response.read()
                stop(process);process=None
                process=start()
                assert process.pid!=first
                with sqlite3.connect(data/'live.sqlite3') as db:
                    assert json.loads(db.execute('SELECT body FROM live_meta WHERE key=?',('packaging-smoke-marker',)).fetchone()[0])=='retained'
                assert get('/api/exporter')['enabled'] is False
                proof.update(startup=True,duplicate_reuses_process=True,foreign_data_dir_rejected=True,
                             stop_and_restart=True,persistent_data_retained=True,personal_reading_default_disabled=True,
                             first_pid=first,restarted_pid=process.pid)
                stop(process);process=None
            finally:
                if process and process.poll() is None:
                    # 仅本测试直接创建且仍由Popen持有的子进程，不扫描或停止其他服务。
                    process.terminate();process.wait(timeout=10)
    archive=args.app.resolve().parent.with_suffix('.zip')
    proof['archive_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
    build='build-macos-x86_64' if check.get('architecture')=='x86_64' else 'build-macos'
    destination=ROOT/'runtime'/build/'smoke-proof.json'
    destination.write_text(json.dumps(proof,ensure_ascii=False,indent=2));destination.chmod(0o600)
    print(json.dumps(proof,ensure_ascii=False))


if __name__=='__main__': main()
