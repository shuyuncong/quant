# 模型接口协议选择

模型配置支持 `chat_completions`（默认）和 `responses`。旧代码把全部模型固定请求到 `/chat/completions`，且强制发送 `temperature: 0.2`；协议不匹配或上游不支持该采样参数都可能造成测试失败。仅凭 `gpt-6-astra` 模型名称无法判断服务商的接口能力，仍需结合具体 HTTP 错误确认。

## 使用

在系统配置的模型配置页，编辑模型，选择服务商支持的接口协议，保存后再测试。Base URL 通常填到 `/v1`；如填写完整 `/chat/completions` 或 `/responses` 地址，会根据所选协议规范化，避免重复拼接端点。新增、编辑、复制均保留协议；测试、AI 分析、图片识别和降级链使用同一设置。

Responses 使用 `input`、`input_text`、`input_image`，读取 `output` 中的文本消息及 SSE 文本事件；要求流收到 `response.completed`，失败或截断不能当成成功报告。请求设置 `store: false`、`stream: true`；系统提示统一放到 `instructions`，不发送 developer 角色。两种协议都不强制采样参数，使用上游默认值。连通测试超时为 60 秒。

Responses 可选推理强度：服务默认、low、medium、high、xhigh、max。服务默认不发送 reasoning 参数；其余发送 `reasoning.effort`，具体档位是否可用由上游模型决定。反馈中的 `xhigh` 可在编辑模型时选择。`contextWindow`、`maxTokens` 是客户端能力元数据，未照搬为 API 参数；命令行的 sandbox/approval 配置与模型 HTTP 协议无关。

## 存储与发布

协议保存在已有 `quant.settings` 的 `models.protocol.<模型ID>` 键中，推理强度保存在 `models.reasoning.<模型ID>`。模型与设置的新增、更新、删除通过同一事务处理。旧模型缺少设置时默认 Chat Completions、服务默认推理强度。

本次不修改 schema：仍要求原有 v4，已完成 v4 升级的环境无需新增 `db:setup`。发布时更新代码并重新构建、启动 Web。此前尚未完成 v4 升级的环境仍需按原重构发布流程升级。

## 验证边界

自动测试使用隔离模拟，覆盖协议与推理强度持久化调用、接口参数校验、旧配置默认值、测试请求、图片转换、Responses 流式结果、未完成响应及跨协议降级，不调用生产数据库或真实付费模型接口。

用户随后明确要求排查 `newapi.illsky.com`，进行了真实最小请求。初期使用本地量化模型配置中保存的 Key，模型列表返回 200 且包含 gpt-6-astra，但生成请求返回 `invalid_responses_request`（400）或 `do_request_failed`（500）。当前进程中的 `NEW_API_KEY` 与量化配置中的 Key 不同，前者返回 401；这不构成保存的 Key 无效的证据。

进一步用本地模拟 HTTP 服务捕获 Codex CLI 请求结构（未让 CLI 调用真实模型），对比后成功用原模型 Key 获得 HTTP 200、OK 和 response.completed。无需模拟 Codex 请求头。已验证的兼容请求包含明确的 message 类型、`text.verbosity=low`、`include=["reasoning.encrypted_content"]` 和每次调用生成的 `prompt_cache_key`，保留系统 instructions 和原推理强度。

实现方式：普通 Responses 请求保持原行为；仅当上游返回 HTTP 400 且明确包含 invalid codex request / invalid_responses_request 时，补齐上述兼容字段重试一次。遇到 do_request_failed 时按瞬时错误重试一次，保留同一份兼容请求和 cache key。两类重试均有次数限制，不会无限请求，也不会修改模型保存的 Key 或协议设置。

最终直接运行应用真实 `testProfile` 函数，读取本地现有 gpt-6-astra 配置（Responses、xhigh），返回 `{"ok":true,"detail":"OK"}`，耗时 15983 毫秒。真实生成调用已验证成功；生产服务尚未发布本轮改动。

本次环境访问 OpenAI 官方文档页面返回 HTTP 403，未能据此确认指定模型或第三方转发服务的支持范围。
