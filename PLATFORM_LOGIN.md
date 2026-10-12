# 分析平台连接

在工作台右上角打开“连接分析平台”。可选择 ChatGPT 或 DeepSeek，切换时保留素材、创作提示词和视频生成参数。

| 平台 | 连接方式 | 用量来源 |
| --- | --- | --- |
| ChatGPT | 浏览器账号授权 | 当前账号计划额度 |
| DeepSeek | 填写 API Key，保存并校验模型列表 | DeepSeek API 余额，按接口用量单独计费 |

## DeepSeek API

1. 在 DeepSeek 开放平台创建自己的 API Key。
2. 在本工具选择 DeepSeek，填入接口密钥，点击“保存并校验”。
3. 校验只读取 `https://api.deepseek.com/models`，不调用收费推理。
4. 图片和视频抽帧分析选择 `deepseek-flash`；`deepseek-v4-pro` 仅用于纯文本。
5. 点击分析或优化提示词时，通过 `https://api.deepseek.com/chat/completions` 发送要求、角色图和抽帧图片，返回结构化创作方案。

DeepSeek 不使用浏览器登录授权，也不会读取浏览器 Cookie 或把网页登录凭据转换为 API Key。需要有效 API Key 和可用 API 余额。
密钥由当前 Windows 账号加密保存为工作室数据目录中的 `deepseek_api_key.bin`；不进入普通设置、日志或分发包。切换、重启后可继续使用自己的已保存密钥。
更换密钥验证失败或取消校验时，保留原先可用密钥。接口返回 401 时清除被拒绝的当前密钥；其他 HTTP 错误保留密钥。收费推理失败不自动重发，原提示词保留。

## 配置与用量

ChatGPT 凭据保存为 `chatgpt_accounts.bin`。每位使用者配置自己的账号和密钥，历史、素材库和队列保存在各自 Windows 用户目录。
分析记录保存实际平台、模型、连接方式和返回的 token 用量。DeepSeek API 费用与 ChatGPT 会员、Seedance 视频生成费用分别计算。
语音转写仍默认关闭；单独开启时使用 OpenAI 接口密钥并按接口计费。

## 官方资料

- [DeepSeek API 首次调用](https://api-docs.deepseek.com/zh-cn/)
- [图片输入](https://api-docs.deepseek.com/zh-cn/guides/vision/)
- [JSON 输出](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
- [模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
