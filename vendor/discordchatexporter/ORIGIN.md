# DiscordChatExporter

上游：[DiscordChatExporter 2.48](https://github.com/Tyrrrz/DiscordChatExporter/releases/tag/2.48) · [MIT](LICENSE)

拾聊首次使用个人账号读取时下载对应架构的上游 CLI，并校验发行文件。CLI 自带运行环境，无需另装 .NET。

| macOS 下载包 | SHA-256 |
| --- | --- |
| Apple Silicon | `623f9d2dce568e17a46b8fbd366a18dca49803d386216f4ba24507d2c000fee9` |
| Intel | `91b4eae3525df85d084969004f3a287edad4eeaafd664e4869b26bb8422e2e88` |

## 适配范围

适配器只调用 `export`：按一个文字频道和指定时间范围导出 JSON，不下载附件，不读取线程。日期边界转换成对应的 Snowflake，导入后按 UTC 时间再次筛选。登录 Token 通过进程环境变量传递。

上游版本对应提交 `905489b01b5523719082275c34c232fc47f827c6`。更新 CLI 时，同步更新固定版本、资产校验值和许可证。
