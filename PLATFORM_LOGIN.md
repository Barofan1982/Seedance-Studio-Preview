# 分析平台登录与分发

## 使用者

在工作台右上角打开“选择分析平台 / 登录”，选择平台并完成浏览器授权。
工具里没有应用注册、应用密钥或开发者配置表单。各平台账号独立保存，切换时保留素材、创作提示词和视频生成参数。

| 平台 | 当前接入方式 | 当前状态 |
| --- | --- | --- |
| ChatGPT | 现有账号授权 | 保留现有功能；使用当前账号计划额度 |
| DeepSeek | Harness 官方账号协议，浏览器授权、S256 PKCE、本机回调 | 登录和图片/文本分析客户端已实现；实际登录需使用者在浏览器完成 |
| WorkBuddy | 开发者授权服务，用户浏览器授权 | 客户端已实现；发行版尚未配置授权服务，显示暂未开通 |
| 豆包工作 | 开发者授权服务，用户浏览器授权 | 客户端已实现；发行版尚未配置授权服务，显示暂未开通 |

DeepSeek 模型是官方 Harness 的预置目录，不代表已逐个验证账号权限；图片分析选择 `deepseek-flash`，纯文本还可选择 `deepseek-v4-pro`。
DeepSeek 使用平台账号余额或赠送额度，WorkBuddy 和豆包工作使用相应平台额度；不共享 ChatGPT Pro 额度。
账号分析不会转为 OpenAI API 密钥调用。原有可选语音转写仍需要单独的 OpenAI 接口密钥，仅用户启用时调用。

## 开发者准备（在工具外完成）

WorkBuddy 的官方第三方应用要求开发者注册、审核应用并运行授权服务。应用密钥与刷新令牌仅保留在服务端。
软件分发后，使用者只需登录授权；不需要每个人重新注册开发者应用。

豆包工作公开门户当前展示的部分智能体示例指向飞书接口。这里没有将飞书接口直接当作豆包工作接口启用。
需要开发者确认实际租户、接口、用户授权和图片分析能力后，提供兼容服务。

发行配置只有本目录 `platform_defaults.json` 中的公开 `service_url`。留空时禁用登录，不打开平台主页假装连接成功。
例如将 `workbuddy.service_url` 设置为开发者已经部署并验证的 HTTPS 服务地址。
此文件不能含应用密钥、刷新令牌或其他使用者的账号。

## 授权服务契约

以下是 **Seedance Studio 自有服务契约**，不是 WorkBuddy 或豆包工作的原生接口。
服务必须实现各平台的实际 OAuth 和业务适配，完成注册与真实账号验证后才可用于分发。

所有路径位于 `service_url/v1/platforms/{provider}/`；`provider` 为 `workbuddy` 或 `doubao_work`。
除公开元数据和登录申请外，均验证 `Authorization: Bearer <session_token>`。

| 路径 | 方法 | 请求与响应 |
| --- | --- | --- |
| `metadata` | GET | 返回 `{provider, configured, models:[{slug, display_name, vision}]}`；未开通时 configured=false |
| `auth/start` | POST | 收到 `{code_challenge, code_challenge_method:"S256"}`；返回 `{attempt_id, authorize_url, expires_in}` |
| `auth/poll` | POST | 收到 `{attempt_id, code_verifier}`；返回 `{status:"pending"}`，或 `{status:"authorized", session_token, label}` |
| `auth/cancel` | POST | 收到 `{attempt_id, code_verifier}`；取消并禁止后续领取凭证 |
| `auth/revoke` | POST | 撤销当前桌面会话，返回 JSON 对象 |
| `account` | GET | 核对当前用户授权有效性；无效时返回 HTTP 401 |
| `analyze` | POST | 收到 `{request_id, request:{model, instructions, input, text:{format:{schema}}, …}}`；返回 `{draft, usage}` |

`authorize_url` 必须先指向授权服务自身同一 HTTPS 来源；服务端再重定向到官方 OAuth 页面。
服务端必须生成和验证独立随机 OAuth state、绑定回调、校验 S256 verifier、限制登录有效期、单次领取桌面会话。
桌面只保存不透明 session_token，不能返回平台 client_secret、refresh_token 或其他用户凭证。
建议短期桌面凭证、撤销和失效处理、按用户隔离授权、登录申请限流。创建 AI 分析请求不自动重试。

`request.input[].content` 中的 `input_image.image_url` 包含实际抽帧/图片的 data URL，`input_text.text` 包含要求与素材说明。
服务需要把图片交给支持视觉的模型；不能删除图片后谎称完成视频分析。
WorkBuddy 公开本地助理消息接口目前接受文本；若适配服务尚未完成图片传递与视觉处理验证，metadata 中必须报告 vision=false。
当 vision=false 时，桌面禁止对视频和图片执行分析，保留纯文本优化能力。

`draft` 字段固定包含 analysis（字符串）、shots（数组，项含 time/visual/camera/motion 字符串）、seedance_prompt（非空字符串）、uncertainties（字符串数组）、keyframe_handles（字符串数组）。
返回结构不完整、连接中断、额度不足或账号失效都保留用户当前草稿。

## 本机数据与验证

登录凭据由当前 Windows 用户的系统加密保存到工作室数据目录：
`deepseek_account.bin`、`workbuddy_account.bin`、`doubao_work_account.bin`。
配置、历史记录和日志折叠媒体编码并隐藏凭证。模型路由和平台来源记入提示词分析记录，DeepSeek 返回的 token 用量随记录保存。

验证覆盖 PKCE、错误/重复/非 ASCII state、取消后迟到的兑换响应、账号代次失效、登录网址来源、图片传递、结构解析、平台切换和授权服务契约。
这些自动检查使用模拟账号，不消耗模型或视频费用。真实账号授权需要使用者完成浏览器登录。

## 官方资料

- [DeepSeek Harness 账号授权模块](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/credentials/deepseek-account-platform/README.zh.md)
- [DeepSeek Harness 账号推理模块](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek-account/README.md)
- [DeepSeek Harness 官方模型目录](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek/src/models.ts)
- [WorkBuddy 第三方应用与 OAuth](https://open.workbuddy.cn/docs/third-party-app)
- [WorkBuddy Open API](https://open.workbuddy.cn/docs/openapi)
- [豆包开放平台](https://open.doubao.com/)
