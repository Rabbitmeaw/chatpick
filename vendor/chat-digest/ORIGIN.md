# chat-digest 话题分类

上游：[chat-digest 固定版本](https://github.com/tawashi-lab/chat-digest/tree/fb83b3b07810d13b501384a2208fc3f76cccc30b) · [MIT](LICENSE)

用于按条为 Discord 消息分类。复用下方列出的四个函数和 `casual.yaml` 的两个分类提示词。函数接收拾聊传入的单频道消息和引用，使用拾聊的模型连接与数据视图，不依赖 pandas 或服务商专用缓存。

下列 SHA-256 用于核对上游文件与复用的函数。

```json
{
  "commit": "fb83b3b07810d13b501384a2208fc3f76cccc30b",
  "functions": {
    "fetch_chat_topic": "d2ee62e97f6485f4bd9da537df1db31ffc83288a55333b87195eea53ca06c230",
    "split_topic_judge_user_prompt": "65f51ec4dbcc5987a5526c75e0abcdc9ecad2af2436359ed432624de36626b74",
    "build_topic_judge_prompt_payload": "1de5244eff67ddab4452474bcf16f652d55a6021518daa6a9b804e475bfc2d41",
    "call_topic_judge_llm": "55bd7895b7d5aca7c90416137bce18c07c78fa7c1aa049e165b8f90b7dddf3f6"
  },
  "files": {
    "src/chat_digest/analysis/topics.py": "98d7fc7ed9f6b27e2fdb828e49b7ab8504f3e4da278312b8361957d9f7c07ac0",
    "src/chat_digest/llm/prompt_cache.py": "fed8f3a2242c28add1637409884a0ccb5e11b94daa1e61d1913710f8e259c7fb",
    "src/chat_digest/prompts/casual.yaml": "87f950c3f93353ee04b79adf34b6438096224b26b457fa678760dac3cf52dbff",
    "LICENSE": "2744c3a11660ad62f3d3bc8cb0bf1bf179adc493c95d69b2eaae88091d6d8d53"
  }
}
```
