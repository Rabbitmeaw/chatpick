# 拾聊 ChatPick

把 Discord 频道里的讨论整理成日报、周报和成员摘要，也能在选定的频道和时间范围内提问。报告和回答可以回看原文。

## 安装

从 [GitHub Releases](https://github.com/Rabbitmeaw/chatpick/releases) 下载 `ChatPick-macos-arm64.dmg`，把「拾聊 ChatPick」拖入「应用程序」。安装包适用于 Apple Silicon Mac，需要 macOS 13 或更新版本，无需另装 Python 或 Node。

如果系统首次拦截打开，按安装盘里的《使用说明》操作。Intel Mac 可按[构建说明](packaging/BUILD.md)自行构建。

## 生成第一份报告

1. 打开「模型设置」，填入你的模型服务商提供的 **Base URL**（API 基础地址）、**Model**（模型名称）和 **API Key**。服务需兼容 OpenAI API。
2. 在「报告与总结」选择聊天来源：
   - **已有 JSON**：选「个人账号或 JSON」，展开「备用：导入已有 JSON 或查看本地记录」。支持你有权处理的 DiscordChatExporter 单频道 JSON，最大 25 MiB。导入不需要 Discord Token。
   - **服务器 Bot**：在 [Discord 开发者页面](https://discord.com/developers/applications) 的 Bot 设置中获取 Token 并启用 Message Content Intent。请有权限的成员安装 Bot，并给 Bot 授予目标频道的「查看频道」和「阅读消息历史记录」权限。然后在拾聊的「Bot 接入」中保存 Token、连接并发现频道。
3. 选频道和时间范围，生成日报、周报或成员摘要。也可以在同一范围内问答。

聊天记录和历史保存在本机，macOS 凭据保存在系统钥匙串。生成报告或问答时，相关原文和必要上下文会发送到你配置的模型服务，可能产生费用。

### 用个人 Discord 账号读取

个人读取默认关闭。在「个人账号或 JSON → Discord 账号连接」主动启用后，才可填写自己的登录 Token。拾聊只读取账号有权查看的所选文字频道。持续读取和每日生成需要分别启用。

> 开启后，拾聊使用你的 Discord 登录 Token 自动读取所选频道的消息。Discord 禁止这种普通账号自动化。即使你只读取自己有权查看的频道，账号也可能被停用。[Discord 官方说明](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots)

## 源码与反馈

[构建说明](packaging/BUILD.md)包含离线样例、源码运行、测试与打包步骤。反馈问题时，请提供版本、系统和复现步骤，示例请用虚构聊天。

拾聊自有代码和文档采用 [MIT](LICENSE)。依赖见[第三方许可证](THIRD_PARTY_NOTICES.md)。
