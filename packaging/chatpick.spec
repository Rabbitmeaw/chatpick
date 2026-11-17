from pathlib import Path
import os

root=Path(SPECPATH).parent
build=Path(os.environ.get('CHATPICK_BUILD_DIR',str(root/'runtime/build-macos')))
target_arch=os.environ.get('CHATPICK_TARGET_ARCH','arm64')
resources=[(str(root/'web'),'web'),(str(root/'fixtures/demo.json'),'fixtures'),(str(root/'vendor'),'vendor'),
           (str(build/'licenses'),'licenses')]
a=Analysis([str(root/'packaging/macos-entry.py')],pathex=[str(root)],binaries=[],datas=resources,
           hiddenimports=['quickjs','_quickjs'],hookspath=[],hooksconfig={},runtime_hooks=[],
           excludes=['tkinter'],noarchive=False,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='ChatPickService',debug=False,
        bootloader_ignore_signals=False,strip=False,upx=False,console=False,argv_emulation=False,
        target_arch=target_arch,codesign_identity=None,entitlements_file=None)
collect=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='ChatPick')
app=BUNDLE(collect,name='拾聊 ChatPick.app',icon=str(build/'ChatPick.icns'),
           bundle_identifier='app.chatpick.desktop',info_plist={'LSUIElement':True,
           'CFBundleDisplayName':'拾聊 ChatPick','NSHighResolutionCapable':True})
