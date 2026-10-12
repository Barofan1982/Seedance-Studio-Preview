# 分析平台登录

## 使用者

在工作台右上角打开“选择分析平台 / 登录”，选择 ChatGPT 或 DeepSeek 并完成浏览器授权。
各平台账号独立保存，切换时保留素材、创作提示词和视频生成参数。

| 平台 | 接入方式 | 当前状态 |
| --- | --- | --- |
| ChatGPT | 账号授权 | 使用当前账号计划额度 |
| DeepSeek | Harness 官方账号协议，浏览器授权、S256 PKCE、本机回调 | 登录和图片/文本分析客户端已实现；实际授权需在浏览器完成 |

DeepSeek 模型来自官方 Harness 预置目录，不代表已逐个验证账号权限；图片分析选择 `deepseek-flash`，纯文本还可选择 `deepseek-v4-pro`。
DeepSeek 使用平台账号余额或赠送额度，不共享 ChatGPT Pro 额度。
账号分析不会转为 OpenAI API 密钥调用。原有可选语音转写仍需要单独的 OpenAI 接口密钥，仅用户启用时调用。

## 本机数据与兼容

登录凭据由当前 Windows 用户的系统加密保存到工作室数据目录：
ChatGPT 使用 `chatgpt_accounts.bin`，DeepSeek 使用 `deepseek_account.bin`。
配置、历史记录和日志折叠媒体编码并隐藏凭证。分析记录保存平台、模型和用量来源；DeepSeek 返回的 token 用量随记录保存。

旧配置若选中了当前不支持的平台，启动时自动切回 ChatGPT，清理不支持的平台模型设置，保留素材、提示词和生成参数。

## 验证

自动检查覆盖 PKCE、错误/重复/非 ASCII state、取消后迟到的兑换响应、账号代次失效、登录网址来源、图片传递、结构解析和平台切换。
返回结构不完整、连接中断、额度不足或账号失效都保留用户当前草稿。
授权兑换成功后，账号资料读取返回 403、超时或其他非鉴权错误时保留授权，界面单独提示资料未同步并显示官方预置模型；这不代表已验证推理接口的可用性。资料返回 401 或鉴权失效码 40003 时，仍清除被拒绝的凭据并要求重新登录。资料同步诊断随调用记录保存，后续刷新成功会清除提示。
这些检查使用模拟账号，不消耗模型或视频费用。真实账号授权需要使用者完成浏览器登录。

## 官方资料

- [DeepSeek Harness 账号授权模块](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/credentials/deepseek-account-platform/README.zh.md)
- [DeepSeek Harness 账号推理模块](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek-account/README.md)
- [DeepSeek Harness 官方模型目录](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek/src/models.ts)
