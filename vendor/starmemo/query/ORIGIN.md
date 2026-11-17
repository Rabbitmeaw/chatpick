# TraceMemo 问答模块

问答模块来自 TraceMemo。接入时的来源快照为 `05bda4a454a9b7c06d1e8dbe661dbf0c934ddd3a`，文件版本及适配见 `manifest.json`。

此目录直接复制其中 8 个运行模块，共 3581 行原源码：Query Agent、问答入口、对话记忆、消息引用、引用清理、本地工具执行、工具协议、模型失败分类。原字节与当前复制版本的 SHA-256 见 `manifest.json`。原项目未被修改。

复制时只做以下适配：

- 平台名称改为 Discord 或中性“聊天”，入口类型改为 AskChannel；用户可见品牌与设置路径对应拾聊界面。
- 保留拾聊现有最多 6 次完整问答；原记忆模块的三个参数改为 6 / 256 / 512（轮数 / 问题字符 / 回答字符）。其他裁剪、pending、摘录校验、LRU、generation/revision 防竞态函数保留原代码。
- 关闭原工具循环的自动扩大日期分支。单频道和日期的硬限制由 SQLite 适配器再次执行。

`query-core.js` 由 `scripts/build-query-core.mjs` 使用固定 Node 24.5.0 机械转换 TypeScript 语法、连接原 import/export。没有把算法翻译成 Python。Node 只用于维护时生成；应用使用现有内嵌 QuickJS 运行复制模块。

`host.js` 仅提供 QuickJS 缺少的编码/随机数接口、异步消息桥、固定 Discord 范围，以及原记忆与 SQLite 快照之间的恢复。Python 适配器提供四个原工具的本机 SQLite 实现、已配置 OpenAI Compatible 传输及严格身份/出处校验；未带入原工程全局检索、媒体服务、旧引擎回退或日志。

原历史 UI 是 React 与浏览器结果缓存，且明确不恢复引擎记忆。拾聊保留已有 SQLite 全文历史，恢复后交给复制的记忆模块处理；没有移植 React、Electron 或浏览器聊天持久化。旧历史无原记忆快照时，使用原 `record` 函数装载既有问答。
