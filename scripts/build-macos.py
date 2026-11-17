#!/usr/bin/env python3
"""构建内置运行环境的macOS分发包；不读取运行数据。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/'runtime/build-macos'
RELEASE=ROOT/'dist/ChatPick-macos-arm64'
ARCH=platform.machine()
MINIMUM_MACOS='13.0'
VERSION=(ROOT/'VERSION').read_text().strip()


def python_command(python,*arguments):
    return ['/usr/bin/arch','-'+ARCH,str(python),*map(str,arguments)]


def run(command,**options):
    subprocess.run([str(part) for part in command],check=True,cwd=ROOT,**options)


def audit(folder):
    entries=[]
    for path in sorted(folder.rglob('*')):
        name=path.relative_to(folder).as_posix()
        if any(part in ('runtime','credentials','secrets','.venv','.git','logs') for part in path.relative_to(folder).parts):
            raise ValueError('发布包含非发布目录：'+name)
        if path.suffix.lower() in ('.db','.sqlite','.sqlite3','.log','.key') or path.name.startswith('.env'):
            raise ValueError('发布包含运行数据或凭据文件：'+name)
        if path.suffix.lower()=='.pem' and path.name!='cacert.pem':
            raise ValueError('发布包含非证书库PEM：'+name)
        if path.is_symlink():
            if not path.resolve().is_relative_to(folder.resolve()): raise ValueError('发布包含外部符号链接：'+name)
            entries.append(dict(path=name,symlink=os.readlink(path)))
        elif path.is_file():
            entries.append(dict(path=name,bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return entries


def audit_native(folder,architecture,minimum_system):
    result=[]
    def version(value): return tuple(map(int,(value+'.0.0').split('.')[:3]))
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.is_symlink(): continue
        with path.open('rb') as handle: magic=handle.read(4)
        if magic not in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca'): continue
        architectures=subprocess.check_output(['/usr/bin/lipo','-archs',str(path)],text=True).strip().split()
        if architecture not in architectures: raise ValueError('发布二进制缺少目标架构：'+str(path))
        commands=subprocess.check_output(['/usr/bin/otool','-l',str(path)],text=True)
        minimum='0.0';command=''
        for line in commands.splitlines():
            parts=line.split()
            if len(parts)!=2: continue
            if parts[0]=='cmd': command=parts[1]
            elif (command=='LC_BUILD_VERSION' and parts[0]=='minos') or (command=='LC_VERSION_MIN_MACOSX' and parts[0]=='version'):
                if version(parts[1])>version(minimum): minimum=parts[1]
        if version(minimum)>version(minimum_system): raise ValueError('发布二进制要求更高系统版本：'+str(path)+' → '+minimum)
        result.append(dict(path=path.relative_to(folder).as_posix(),architectures=architectures,minimum_macos=minimum))
    return result


def intel_quickjs_sources():
    # PyPI的固定版本sdist漏带头文件；从对应v0.17.0源码补齐，不换引擎。
    sources=BUILD/'sources';sources.mkdir(exist_ok=True)
    pinned=[('quickjs_ng-0.17.0.1.tar.gz',
        'https://files.pythonhosted.org/packages/9d/f0/34285112767f07aa2df4f4f8741b00fc6bae80124162245f2901fba1f2e7/quickjs_ng-0.17.0.1.tar.gz',
        'a1f7352b7e508346c8254f5ae790799898ebd1b2df2538bf9b7070a757faf903'),
        ('quickjs-v0.17.0.tar.gz',
        'https://codeload.github.com/quickjs-ng/quickjs/tar.gz/6d46d07d04041b40f4f49eaa7fdebe44c314c699',
        'a62cf1ff7d6d2f82b90a2d247a57e9eb56b81c03feb1f372a53923426e358cb0')]
    for name,url,digest in pinned:
        path=sources/name
        if not path.is_file(): run(['/usr/bin/curl','--fail','--location','--max-time','90',url,'--output',path])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest: raise ValueError('构建源文件哈希不匹配：'+name)
    source=sources/'quickjs_ng-0.17.0.1'
    if source.is_symlink(): raise ValueError('构建源码目录为符号链接，未覆盖。')
    if source.exists(): shutil.rmtree(source)
    for name,_,_ in pinned:
        with tarfile.open(sources/name) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or not (sources/member.name).resolve().is_relative_to(sources.resolve()):
                    raise ValueError('构建源文件包含不允许的路径。')
            archive.extractall(sources,filter='data')
    upstream=sources/'quickjs-6d46d07d04041b40f4f49eaa7fdebe44c314c699'
    for original in (source/'upstream-quickjs').iterdir():
        if original.suffix in ('.c','.h') and original.read_bytes()!=(upstream/original.name).read_bytes():
            raise ValueError('QuickJS发布源码与固定版本不一致：'+original.name)
    for header in upstream.glob('*.h'): shutil.copyfile(header,source/'upstream-quickjs'/header.name)
    proof=dict(version='0.17.0.1',upstream_commit='6d46d07d04041b40f4f49eaa7fdebe44c314c699',
               archives=[dict(name=name,url=url,sha256=digest) for name,url,digest in pinned],
               existing_c_and_headers_identical=True,change='仅补齐同版本sdist遗漏的头文件')
    (sources/'quickjs-build-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    return source


def prepare_intel_quickjs(python,environment):
    source=intel_quickjs_sources()
    run(python_command(python,'-m','pip','install','--disable-pip-version-check','--no-build-isolation',source),env=environment)


def desktop(app):
    sdk=subprocess.check_output(['/usr/bin/xcrun','--show-sdk-path'],text=True).strip()
    run(['/usr/bin/swiftc','-swift-version','5','-parse-as-library','-O','-target',ARCH+'-apple-macosx'+MINIMUM_MACOS,
         '-sdk',sdk,'-module-cache-path',BUILD/'swift-modules','-framework','AppKit','-framework','WebKit',
         ROOT/'packaging/Desktop.swift','-o',app/'Contents/MacOS/ChatPick'])
    path=app/'Contents/Info.plist'
    info=plistlib.loads(path.read_bytes())
    info.update(CFBundleExecutable='ChatPick',CFBundleShortVersionString=VERSION.split('-')[0],CFBundleVersion='1',
                LSUIElement=False,LSMinimumSystemVersion=MINIMUM_MACOS,
                NSAppTransportSecurity=dict(NSAllowsLocalNetworking=True))
    path.write_bytes(plistlib.dumps(info))
    run(['/usr/bin/codesign','--force','--deep','--sign','-',app])
    run(['/usr/bin/codesign','--verify','--deep','--strict',app])


def icon():
    directory=BUILD/'ChatPick.iconset';directory.mkdir(parents=True,exist_ok=True)
    for size in (16,32,128,256,512):
        for scale in (1,2):
            output=directory/f'icon_{size}x{size}{"@2x" if scale==2 else ""}.png'
            run(['/usr/bin/sips','-z',size*scale,size*scale,ROOT/'web/assets/app-icon.png','--out',output],stdout=subprocess.DEVNULL)
    run(['/usr/bin/iconutil','-c','icns',directory,'-o',BUILD/'ChatPick.icns'])


def licenses(python):
    target=BUILD/'licenses';target.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'LICENSE', target/'ChatPick-MIT.txt')
    (target/'ChatPick-Third-Party-Notices.md').unlink(missing_ok=True)
    script="""from importlib.metadata import distribution
import json,sys
from pathlib import Path
result=[]
for package in ('PyInstaller','quickjs-ng','certifi'):
 d=distribution(package)
 for entry in d.files:
  if '.dist-info/' in str(entry) and ('/licenses/' in str(entry) or Path(entry).name.upper().startswith(('LICENSE','COPYING'))):
   result.append([package,str(d.locate_file(entry))])
python=Path(sys.base_prefix)/('lib/python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'LICENSE.txt'
if not python.is_file(): raise RuntimeError('未找到CPython许可证')
result.append(['Python',str(python)])
print(json.dumps(result))
"""
    result=subprocess.check_output(python_command(python,'-c',script),text=True,cwd=ROOT)
    for package,source in json.loads(result):
        shutil.copyfile(source,target/(package+'-'+Path(source).name))
    if ARCH=='x86_64':
        shutil.copyfile(BUILD/'sources/quickjs-6d46d07d04041b40f4f49eaa7fdebe44c314c699/LICENSE',target/'QuickJS-upstream-LICENSE')


def main():
    global BUILD,RELEASE,ARCH
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true',help='首次准备固定官方构建依赖，需要联网')
    parser.add_argument('--arch',choices=('arm64','x86_64'),default=platform.machine(),help='目标架构；依赖和中间产物按架构隔离')
    args=parser.parse_args()
    if platform.system()!='Darwin':
        raise SystemExit('macOS分发包必须在macOS构建。')
    ARCH=args.arch
    BUILD=ROOT/('runtime/build-macos' if ARCH=='arm64' else 'runtime/build-macos-x86_64')
    RELEASE=ROOT/('dist/ChatPick-macos-'+ARCH)
    BUILD.mkdir(parents=True,exist_ok=True)
    python=BUILD/'venv/bin/python'
    if args.prepare:
        if not python.is_file(): run(python_command(sys.executable,'-m','venv',BUILD/'venv'))
        prepare_environment=dict(os.environ,PIP_CACHE_DIR=str(BUILD/'pip-cache'),
            MACOSX_DEPLOYMENT_TARGET=MINIMUM_MACOS,ARCHFLAGS='-arch '+ARCH)
        if ARCH=='x86_64':
            # 直接调用原生clang交叉编译，避开Rosetta的系统xcrun启动器。
            prepare_environment.update(CC=subprocess.check_output(['/usr/bin/xcrun','--find','clang'],text=True).strip(),
                SDKROOT=subprocess.check_output(['/usr/bin/xcrun','--show-sdk-path'],text=True).strip(),
                _PYTHON_HOST_PLATFORM='macosx-'+MINIMUM_MACOS+'-x86_64')
            run(python_command(python,'-m','pip','install','-r',ROOT/'packaging/requirements-build.txt','wheel==0.45.1','certifi==2026.7.22'),env=prepare_environment)
            prepare_intel_quickjs(python,prepare_environment)
        else:
            run(python_command(python,'-m','pip','install','-r',ROOT/'packaging/requirements-build.txt','-r',ROOT/'requirements.txt'),env=prepare_environment)
    if not python.is_file(): raise SystemExit('先执行 python3 scripts/build-macos.py --arch '+ARCH+' --prepare 准备构建环境。')
    runtime=json.loads(subprocess.check_output(python_command(python,'-c',
        'import json,platform,sys,quickjs; from importlib.metadata import version; print(json.dumps(dict(architecture=platform.machine(),python=sys.version.split()[0],quickjs=version("quickjs-ng"),engine_ok=quickjs.Context().eval("21*2")==42)))'),text=True))
    if runtime['architecture']!=ARCH or not runtime['engine_ok'] or runtime['quickjs']!='0.17.0.1':
        raise ValueError('构建Python或JavaScript运行环境不匹配，未制作分发包。')
    if ARCH=='x86_64' and not args.prepare: intel_quickjs_sources()
    icon();licenses(python)
    toolchain=Path(subprocess.check_output(['/usr/bin/xcrun','--find','clang'],text=True).strip()).parent
    environment=dict(os.environ,PYINSTALLER_CONFIG_DIR=str(BUILD/'cache'),CHATPICK_BUILD_DIR=str(BUILD),CHATPICK_TARGET_ARCH=ARCH,
                     PATH=str(toolchain)+os.pathsep+os.environ.get('PATH','/usr/bin:/bin'))
    run(python_command(python,'-m','PyInstaller','--noconfirm','--distpath',BUILD/'frozen','--workpath',BUILD/'work',
         ROOT/'packaging/chatpick.spec'),env=environment)
    if RELEASE.is_symlink(): raise ValueError('发布目录为符号链接，未覆盖。')
    if RELEASE.exists(): shutil.rmtree(RELEASE)
    RELEASE.mkdir(parents=True)
    shutil.copytree(BUILD/'frozen/拾聊 ChatPick.app',RELEASE/'拾聊 ChatPick.app',symlinks=True)
    app=RELEASE/'拾聊 ChatPick.app'
    desktop(app)
    shutil.copyfile(ROOT/'packaging/README-desktop.txt',RELEASE/'使用说明.txt')
    native=audit_native(app,ARCH,MINIMUM_MACOS)
    entries=audit(RELEASE)
    manifest=dict(version=VERSION,architecture=ARCH,python=runtime['python'],quickjs=runtime['quickjs'],pyinstaller='6.22.3',
        minimum_macos=MINIMUM_MACOS,desktop='Swift + WKWebView',native_binaries=native,
        developer_id_signed=False,notarized=False,contains_runtime_data=False,files=entries)
    if ARCH=='x86_64': manifest['quickjs_sources']=json.loads((BUILD/'sources/quickjs-build-proof.json').read_text())
    (RELEASE/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    archive=RELEASE.with_suffix('.zip')
    if archive.exists(): archive.unlink()
    run(['/usr/bin/ditto','-c','-k','--sequesterRsrc','--keepParent',RELEASE,archive])
    staging=BUILD/'dmg-stage'
    if staging.is_symlink(): raise ValueError('磁盘映像准备目录为符号链接，未覆盖。')
    if staging.exists(): shutil.rmtree(staging)
    shutil.copytree(RELEASE,staging,symlinks=True)
    (staging/'Applications').symlink_to('/Applications')
    dmg=RELEASE.with_suffix('.dmg')
    run(['/usr/bin/hdiutil','create','-volname','拾聊 ChatPick','-srcfolder',staging,'-format','UDZO','-ov',dmg])
    run(['/usr/bin/hdiutil','verify',dmg])
    print(json.dumps(dict(app=str(app),dmg=str(dmg),dmg_bytes=dmg.stat().st_size,
                         dmg_sha256=hashlib.sha256(dmg.read_bytes()).hexdigest(),archive=str(archive),bytes=archive.stat().st_size,
                         sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),audited_files=len(entries)),ensure_ascii=False))


if __name__=='__main__': main()
