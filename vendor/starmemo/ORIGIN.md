# TraceMemo 日报模块

日报模块来自 TraceMemo。拾聊适配 Discord 的输入和证据校验。

接入时的来源快照为 `05bda4a454a9b7c06d1e8dbe661dbf0c934ddd3a`，对应文件 `src/renderer/src/utils/group-report.ts`。

- 原完整文件 SHA-256：`93a671ec2fabbf3d59d52b463072e8b93efd9c8be32098b10e9fd91b1624bc16`。
- 类型文件 SHA-256：`853d8b862f05522ed19e522eb588fbeaf3192e677702476c39b4f9a60e76c00e`。

`report-core.ts` 直接复制原文件开头至 `parseGroupDailyReport` 结束，保留原变量名、函数体、顺序和编辑规则；省略后续仅用于原工程兼容的导出。平台名称及特有身份字段名称替换为 Discord 中性名称，未改写排序、去重、裁剪、归一化或提示词编辑规则。

`report-core.js` 由 `scripts/build-report-core.mjs` 使用 Node 24.5.0 的 `stripTypeScriptTypes` 机械移除类型、删除原模块 import/export 并包装一个命名空间。算法没有翻译为 Python。Node 仅用于维护时再生成，应用由 Python 内嵌 QuickJS 实际执行本文件。

`buildGroupReportFacts` 由 Discord 适配层提供符合原接口的输入事实。范围、稳定身份、出处和原始统计由适配层校验。
