# 源码运行与构建

以下命令均在仓库根目录运行。

## 运行源码和离线样例

需要 Python 3.11 或更新版本。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m grouppulse --demo --port 8767
```

打开终端提示的本地地址。演示使用虚构的聊天和模拟模型，无需 Discord Token 或 API Key。按 Ctrl+C 停止。使用自己的记录时，改用 `.venv/bin/python -m grouppulse`，按[快速上手](../README.md#生成第一份报告)配置。

## 跑测试

```bash
.venv/bin/python -m unittest discover -s tests
node --test tests/*.test.cjs tests/test_chart.cjs
```

改动报告或问答的 JavaScript 模块后，用 Node 24.5.0 检查生成文件是否与源文件一致：

```bash
node scripts/build-report-core.mjs --check
node scripts/build-query-core.mjs --check
```

## 打包 macOS 应用

需要目标架构的 Python 和 Xcode Command Line Tools。`--prepare` 会联网准备构建依赖；之后可去掉该参数。

```bash
# Apple Silicon
python3 scripts/build-macos.py --arch arm64 --prepare

# Intel
python3 scripts/build-macos.py --arch x86_64 --prepare
```

DMG 输出到 `dist/`。安装包要求 macOS 13 或更新版本，使用临时签名，未做 Apple 公证。随包运行库的许可证可在应用包内的 `Contents/Resources/licenses` 查阅。
