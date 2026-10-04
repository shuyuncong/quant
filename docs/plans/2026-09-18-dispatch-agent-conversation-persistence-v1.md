# 派工智能体完整会话持久化方案 V1

> 状态：待评审  
> 日期：2026-09-18  
> 适用范围：派工智能体工作台、自然语言派工、Chainlit 调试页面、主智能体与 Disp MCP 通信

## 1. 背景

当前系统已经保存了一部分沟通和业务过程数据，但不同类型的数据分散在不同位置：

- 普通问答主要写入 `disp_agent_feedback`；
- 创建派工、补槽、取消、改期和改派等过程写入 `disp_agent_dispatch_*` 业务表；
- 工作台完整聊天气泡主要保存在浏览器 `sessionStorage`；
- Chainlit 没有配置可作为可靠历史来源的完整 Data Layer；
- 主智能体与派工智能体的 MCP 调用写入 `disp_agent_integration_call_log`。

这些记录可以满足反馈、派工状态和接口审计等专项需求，但不能通过一个统一接口，按会话完整恢复用户消息、助手回答、进度卡片、中间状态和工具调用。

因此，需要在现有业务表之外增加一套统一的“会话账本”。

## 2. 建设目标

方案需要实现以下能力：

1. 按会话恢复完整聊天记录；
2. 保存用户消息、助手回答、进度卡片、工具调用和关键中间状态；
3. 普通问答和派工命令进入同一条会话时间线；
4. 会话记录能够关联反馈、派工草稿、命令准备结果和正式提交；
5. 支持刷新页面、重新登录和换设备后恢复历史；
6. 支持失败、取消、超时和重试场景；
7. 保证重复请求不会生成重复消息或重复派工；
8. 对会话查询、敏感数据、保留周期和审计实施统一治理。

## 3. 非目标

第一阶段不包含以下内容：

- 不废弃现有反馈表和派工业务表；
- 不依赖会话事件重建派工业务最终状态；
- 不把每一个流式输出 token 单独永久保存；
- 不承诺完整恢复历史上已经丢失的浏览器会话；
- 不在每次模型请求中加载整段历史会话。

## 4. 设计原则

### 4.1 会话记录和业务状态分离

- 统一会话账本保存“用户和智能体说了什么、处理过程发生了什么”；
- 派工业务表保存“派工业务最终处于什么状态”；
- 反馈表保存用户对具体回答的评价；
- 集成日志保存系统间调用的协议和性能审计信息。

### 4.2 所有入口统一记录

工作台、Chainlit、自然语言派工和 MCP 调用必须通过同一套会话记录组件写入，避免不同入口各自维护一套不一致的逻辑。

### 4.3 重要事件追加写入

过程事件采用追加式记录。已经发生的事件不覆盖，只追加后续状态事件，便于审计、排障和分析。

### 4.4 展示历史和模型上下文分离

数据库可以保存完整历史，但模型推理只选取最近回合、会话摘要和相关业务状态，防止上下文无限增长。

## 5. 总体架构

```text
工作台 / Chainlit / 主智能体 MCP
                 │
                 ▼
        ConversationRecorder
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
 conversation   turn    message/event
       │         │         │
       └─────────┼─────────┘
                 │
       ┌─────────┼──────────────────┐
       ▼         ▼                  ▼
   feedback   dispatch_*   integration_call_log
```

核心模型采用四层结构：

```text
conversation
  └── turn
        ├── message
        └── event
```

## 6. 核心数据模型

### 6.1 会话表 `disp_agent_conversation`

一条记录表示用户看到的一条逻辑会话。

建议字段：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 内部会话主键，建议使用 UUID |
| `thread_id` | 兼容现有线程 ID |
| `tenant_id` | 租户或组织标识 |
| `user_id` | 会话所属用户 |
| `channel` | `workbench`、`chainlit`、`mcp` 等入口 |
| `title` | 会话标题，可异步生成 |
| `status` | `active`、`closed`、`archived` |
| `scope_snapshot_json` | 创建会话时的权限作用域快照 |
| `metadata_json` | 客户端版本、入口信息等扩展属性 |
| `created_at` | 创建时间 |
| `last_activity_at` | 最近活动时间 |
| `closed_at` | 关闭时间 |
| `retention_until` | 数据保留截止时间 |

约束建议：

- `conversation_id` 为主键；
- 根据现有 `thread_id` 的生成方式，建立适当的唯一约束或普通索引；
- 为 `tenant_id + user_id + last_activity_at` 建立会话列表查询索引。

### 6.2 回合表 `disp_agent_conversation_turn`

一条记录表示一次“用户输入 → 智能体处理 → 本次处理结束”的生命周期。

建议字段：

| 字段 | 说明 |
|---|---|
| `turn_id` | 回合主键 |
| `conversation_id` | 所属会话 |
| `client_request_id` | 前端或调用方生成的请求 ID |
| `idempotency_key` | 服务端幂等键 |
| `user_message_id` | 本回合的原始用户消息 |
| `intent_type` | 普通问答、CREATE、MUTATION、CANCEL 等 |
| `status` | 回合执行状态 |
| `correlation_id` | 跨模块追踪 ID |
| `trace_id` | 链路追踪 ID，可选 |
| `command_id` | 自然语言命令关联 ID，可选 |
| `prepare_outcome_id` | 命令准备结果关联 ID，可选 |
| `submission_id` | 正式派工提交关联 ID，可选 |
| `error_code` | 失败错误码 |
| `error_summary` | 可安全展示或审计的错误摘要 |
| `started_at` | 开始时间 |
| `completed_at` | 完成时间 |

状态建议：

- `received`
- `running`
- `waiting_confirmation`
- `completed`
- `failed`
- `cancelled`
- `timeout`
- `interrupted`

### 6.3 消息表 `disp_agent_conversation_message`

消息表保存用户界面上可以恢复和展示的内容。

建议字段：

| 字段 | 说明 |
|---|---|
| `message_id` | 消息主键 |
| `conversation_id` | 所属会话 |
| `turn_id` | 所属回合，可为空 |
| `seq` | 会话内单调递增序号 |
| `role` | `user`、`assistant`、`tool`、`system` |
| `message_type` | 文本、卡片、进度、错误、最终回答等 |
| `content_text` | 普通文本内容 |
| `payload_json` | 卡片、结构化内容和扩展展示数据 |
| `source` | `workbench`、`chainlit`、`mcp`、`backend` |
| `status` | `streaming`、`completed`、`failed` |
| `content_version` | 消息结构版本 |
| `created_at` | 创建时间 |
| `completed_at` | 完成时间 |

`message_type` 建议至少支持：

- `text`
- `card`
- `progress`
- `tool_call`
- `tool_result`
- `confirmation`
- `error`
- `final`

普通文本保存在 `content_text`，派工草稿卡片、进度状态和确认操作等结构化内容保存在 `payload_json`。

### 6.4 事件表 `disp_agent_conversation_event`

事件表保存不可变的过程轨迹，主要用于审计、排障、时间线展示和行为分析。

建议字段：

| 字段 | 说明 |
|---|---|
| `event_id` | 事件主键 |
| `conversation_id` | 所属会话 |
| `turn_id` | 所属回合 |
| `event_seq` | 回合或会话内事件序号 |
| `event_type` | 事件类型 |
| `payload_json` | 事件结构化数据 |
| `correlation_id` | 跨系统追踪 ID |
| `source` | 事件产生模块 |
| `created_at` | 发生时间 |

事件类型建议包括：

- `request_received`
- `intent_classified`
- `slot_updated`
- `tool_started`
- `tool_finished`
- `command_prepared`
- `confirmation_requested`
- `confirmation_received`
- `submission_created`
- `response_started`
- `response_completed`
- `turn_failed`
- `turn_cancelled`
- `turn_timeout`

## 7. 与现有表的关系

现有表继续承担原有职责，只增加必要关联字段。

| 现有表 | 建议调整 |
|---|---|
| `disp_agent_feedback` | 关联 `conversation_id`、`turn_id`、`assistant_message_id` |
| `disp_agent_dispatch_submission_draft` | 关联创建或最近更新草稿的会话、回合 |
| `disp_agent_dispatch_submission_message` | 关联原始 `message_id` 和 `turn_id` |
| `disp_agent_dispatch_prepare_outcome` | 关联 `conversation_id` 和 `turn_id` |
| `disp_agent_dispatch_natural_language_command` | 关联命令来源回合 |
| `disp_agent_dispatch_submission` | 关联最终确认回合和原始命令回合 |
| `disp_agent_integration_call_log` | 关联 `conversation_id`、`turn_id`、`message_id`、`event_id` |

不建议将完整 transcript 复制到每一张业务表中，否则会造成数据重复、口径不一致和更新困难。

## 8. 统一记录组件

建议在后端建设统一的 `ConversationRecorder`，向各入口提供稳定接口，例如：

```text
get_or_create_conversation(...)
start_turn(...)
append_message(...)
start_assistant_message(...)
complete_assistant_message(...)
append_event(...)
complete_turn(...)
fail_turn(...)
```

组件职责：

- 统一生成和传递各类 ID；
- 处理幂等和消息顺序；
- 过滤或脱敏敏感内容；
- 统一补齐来源、时间、租户和权限作用域；
- 控制大型 payload 的保存方式；
- 统一处理异常状态和降级策略。

业务处理模块不应直接拼装多张会话表的写入逻辑。

## 9. 请求写入流程

### 9.1 普通问答

1. 根据 `conversation_id` 或旧 `thread_id` 获取或创建会话；
2. 保存用户消息；
3. 创建一个状态为 `running` 的回合；
4. 记录意图识别、检索和工具调用等关键事件；
5. 创建状态为 `streaming` 的助手消息；
6. 回答完成后保存最终文本并将消息改为 `completed`；
7. 将回合改为 `completed`；
8. 创建反馈待评价记录，或在用户评价时再写入反馈表。

### 9.2 自然语言派工命令

1. 保存用户原始消息并创建回合；
2. 记录命令识别和槽位变化事件；
3. 将回合关联到草稿、命令锁定记录和准备结果；
4. 需要用户确认时，将回合更新为 `waiting_confirmation`；
5. 保存确认卡片或确认提示消息；
6. 用户确认后，创建新的确认回合或记录确认事件；
7. 关联正式 `submission`；
8. 保存最终响应并完成回合。

派工命令即使不调用 `create_feedback`，也必须写入统一会话账本。

### 9.3 MCP 调用

MCP 请求分为两种情况：

- 如果调用来源能够提供上游 `conversation_id` 和 `turn_id`，直接沿用；
- 如果是独立的系统调用，则创建 `channel=mcp` 的会话和回合。

`correlation_id` 必须贯穿主智能体、MCP 调用日志、派工智能体回合以及相关业务记录。

### 9.4 失败、取消和超时

无论请求是否成功，都必须形成可查询的最终状态：

- 追加错误或取消事件；
- 保存必要的用户提示消息；
- 更新助手消息状态；
- 更新回合最终状态；
- 保存错误码和 `correlation_id`。

## 10. 幂等与并发控制

每个用户请求建议包含：

- `client_request_id`：调用端生成；
- `idempotency_key`：服务端用于防止重复处理；
- `correlation_id`：用于跨系统追踪。

数据库层建议建立以下唯一约束：

- `conversation_id + client_request_id`；
- `conversation_id + seq`；
- `turn_id + event_seq`；
- 必要时增加 `turn_id + idempotency_key`。

需要覆盖以下重复场景：

- 用户连续点击发送或确认；
- 浏览器因网络超时自动重试；
- MCP 客户端超时重试；
- 服务重启后的请求恢复；
- 同一派工命令被重复消费。

## 11. 流式输出策略

不建议按 token 写数据库。

推荐方式：

1. 响应开始时创建 `streaming` 状态的助手消息；
2. 流式内容在服务内存或受控缓存中累积；
3. 根据时间间隔或字节数批量刷新内容；
4. 正常完成时写入最终文本和完成时间；
5. 中断时保留已生成内容，并将消息标记为 `failed` 或 `interrupted`；
6. 工具开始、结束、等待确认和错误等关键节点单独写事件。

若未来确有逐段重放需求，可以增加粗粒度 chunk 表或对象存储文件，但不纳入第一版。

## 12. 前端改造

`sessionStorage` 从可靠历史来源降级为短期缓存，只保留以下职责：

- 未提交输入框草稿；
- 网络异常时的临时消息缓存；
- 页面初始化期间的快速展示；
- 短期断线重连辅助。

页面加载流程调整为：

1. 使用 `conversation_id` 请求后端历史；
2. 后端按游标分页返回消息；
3. 前端恢复聊天气泡、卡片和回合状态；
4. 当前执行过程通过 SSE 或 WebSocket 接收；
5. 客户端根据 `message_id` 和 `event_id` 去重；
6. 本地缓存与服务端结果不一致时，以服务端记录为准。

建议提供以下只读接口：

```text
GET /api/workbench/conversations
GET /api/workbench/conversations/{conversation_id}
GET /api/workbench/conversations/{conversation_id}/messages
GET /api/workbench/conversations/{conversation_id}/timeline
```

查询接口需要支持：

- 游标分页；
- 时间范围；
- 消息类型过滤；
- 是否包含事件；
- 权限作用域过滤；
- 对大型 payload 延迟加载。

## 13. 模型上下文策略

完整保存会话不代表每次向模型发送全部历史。

建议模型上下文由以下部分组成：

- 最近若干个有效回合；
- 当前派工草稿和关键槽位；
- 长会话摘要；
- 与当前问题相关的历史片段；
- 当前用户权限作用域。

可以在会话较长时异步生成阶段性摘要，摘要属于辅助数据，不能替代原始消息记录。

## 14. 权限、安全和数据治理

### 14.1 权限

- 所有查询必须同时校验租户、用户和业务权限作用域；
- 不允许只凭 `conversation_id` 越权读取；
- 管理员跨用户查询需要单独权限并记录审计；
- MCP 调用产生的会话需要明确归属服务身份或最终用户。

### 14.2 敏感数据

- 手机号、地址、证件号等敏感字段按规则脱敏；
- 工具原始响应和前端可见响应可以分层保存；
- 密钥、令牌、连接串和内部认证头禁止进入会话记录；
- 对工具输入输出设置字段级允许列表或拒绝列表。

### 14.3 大型内容

- 大型附件、图片和文件保存到受控对象存储；
- 消息表只保存文件引用、摘要、大小和校验值；
- 大型工具输出只保存必要摘要和可追溯引用。

### 14.4 保留策略

建议分别设置：

- 普通会话消息保留周期；
- 过程事件保留周期；
- MCP 原始 payload 保留周期；
- 反馈和正式派工审计保留周期。

具体期限需要由业务、合规和运维共同确认，不建议默认永久保存所有原始 payload。

## 15. 历史数据迁移

历史数据只能部分回填。

可以回填的数据包括：

- `disp_agent_feedback` 中的用户问题和助手答案；
- 派工业务表中的用户原始表达、准备结果和提交结果；
- `disp_agent_integration_call_log` 中的接口请求和响应；
- 迁移期间仍存在的浏览器 `sessionStorage` 内容。

无法可靠恢复的数据包括：

- 已经清理的浏览器聊天记录；
- 未持久化的中间进度卡片；
- 过去的完整工具调用顺序；
- 已丢失的流式输出片段。

历史回填记录建议带上：

```text
source = legacy_backfill
completeness = partial
```

避免把推导或拼接出来的历史数据标记为完整原始记录。

## 16. 可用性和降级策略

需要提前确定会话记录失败时是否阻断核心派工业务。

建议：

- 用户消息和最终结果属于关键记录，写入失败时应明确告警并进行有限重试；
- 非关键进度事件可以异步写入，短暂失败不阻断核心业务；
- 正式派工提交仍以业务表事务结果为准；
- 记录组件需要监控写入失败率、积压量和处理延迟；
- 不建议在第一版引入复杂消息队列，可先采用数据库事务加可靠的应用层补偿，后续按规模演进。

如果要求“业务提交成功和会话最终事件绝不出现不一致”，可以进一步采用本地 outbox 模式，但需要增加实现和运维复杂度。

## 17. 实施计划

### 阶段一：数据模型和统一标识

- 确定 `conversation_id`、`turn_id`、`message_id`、`event_id` 规范；
- 确定 `correlation_id` 跨系统传递方式；
- 确定消息类型、事件类型和 JSON 版本；
- 建立四张核心表和必要索引；
- 同步更新项目 schema 版本；
- 在本地 PostgreSQL 环境执行 setup 和 verify。

### 阶段二：接入写入链路

按以下顺序接入：

1. 工作台普通问答；
2. 工作台自然语言派工；
3. 确认、取消、改期和改派；
4. Chainlit；
5. 主智能体与 Disp MCP 调用。

同时补齐异常、取消、超时和幂等重试测试。

### 阶段三：切换前端读取

- 增加会话列表和历史接口；
- 页面从服务端恢复消息；
- 支持游标分页；
- 支持失败回合和等待确认状态；
- 将 `sessionStorage` 降级为临时缓存。

### 阶段四：历史回填和运营能力

- 回填可识别的历史普通问答；
- 回填可关联的派工命令和 MCP 调用；
- 增加会话搜索、审计和导出能力；
- 增加长会话摘要和相关历史检索。

## 18. 测试方案

测试和验证必须在本地开发环境进行，不连接生产数据库。

至少覆盖：

- 普通问答完整写入；
- 派工命令完整写入；
- 等待确认后继续执行；
- 多次确认幂等；
- 请求超时重试；
- MCP 调用重试；
- 服务执行中断后的状态；
- SSE/WebSocket 断线重连；
- 消息分页和顺序稳定；
- 不同用户、租户和权限作用域隔离；
- 敏感字段脱敏；
- 大型工具响应截断或外置；
- 历史回填记录的部分完整性标识。

## 19. 验收标准

第一版至少满足：

1. 刷新页面后可以按原顺序恢复服务端已接收的消息；
2. 普通问答和派工命令出现在同一会话时间线；
3. 可以查看关键工具调用和进度事件；
4. 失败、取消、超时和中断回合不会丢失；
5. 重试不会生成重复消息或重复派工；
6. 用户反馈可以定位到具体助手消息；
7. 正式派工记录可以追溯到原始用户消息和处理回合；
8. MCP 调用可以通过 `correlation_id` 追溯到具体会话；
9. 未授权用户无法读取其他用户或其他权限作用域的会话；
10. 前端不再依赖 `sessionStorage` 作为唯一历史来源。

## 20. 风险与待确认事项

### 主要风险

- 多入口接入不完整，导致部分请求仍然漏记；
- JSON payload 无版本管理，未来前端无法恢复旧卡片；
- 原始工具结果包含敏感或体积过大的数据；
- 消息记录和业务事务之间出现不一致；
- 历史接口权限校验不完整；
- 事件数量增长过快，影响存储和查询性能。

### 待评审确认

1. 会话记录和过程事件的保留周期；
2. 哪些工具输入输出允许保存原文；
3. 用户确认是否作为独立回合；
4. MCP 调用沿用上游会话还是创建独立会话的判断规则；
5. 第一版是否需要恢复进度卡片，还是只恢复用户和助手消息；
6. 会话记录失败是否阻断普通问答或正式派工；
7. 是否需要引入 outbox 保证业务事务和会话事件最终一致；
8. 历史数据回填的时间范围。

## 21. 最终建议

采用以下四层统一会话模型：

```text
conversation
  + turn
  + message
  + event
```

现有反馈表、派工业务表和集成日志继续保留，通过统一 ID 与会话账本关联。实施时先统一标识和写入链路，再切换前端历史读取，最后进行历史回填和运营能力建设。

该方案技术上可行，对现有派工业务模型侵入较小。首要工作不是一次性补齐所有查询界面，而是确保所有入口都稳定经过统一记录器，并把幂等、权限、敏感数据和异常终态作为第一版必备能力。

## 22. 历史对话加载、继续执行和展示设计

这一部分需要明确区分三个动作：

1. **加载历史**：把服务端已有记录恢复到前端；
2. **继续执行**：基于历史状态开始一个新的回合，或恢复一个未完成回合；
3. **展示历史**：把消息、卡片和关键过程事件按用户可理解的方式呈现。

三者不能简单地等同为“把所有历史文本重新发给模型”。

### 22.1 增加会话状态投影

消息和事件采用追加式保存后，还需要一个可快速读取的当前状态投影：

`disp_agent_conversation_state`

建议字段：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话主键 |
| `version` | 会话状态版本，采用乐观锁 |
| `latest_seq` | 已投影到的最后消息/事件序号 |
| `active_turn_id` | 当前未结束回合，可为空 |
| `conversation_summary` | 长会话摘要 |
| `context_json` | 可继续执行所需的结构化上下文 |
| `pending_action_json` | 待确认、待补槽或待恢复的动作 |
| `updated_at` | 最近更新时间 |

`conversation_state` 是读取和继续执行的加速投影，不替代原始消息和事件。发生不一致时，可以通过事件重新构建投影。

对于需要跨进程恢复的长耗时回合，再增加：

`disp_agent_conversation_checkpoint`

保存 `turn_id`、执行阶段、最后完成的工具、可重试标记、checkpoint payload 和 checkpoint 序号。派工业务状态仍以现有 `disp_agent_dispatch_*` 表为准。

### 22.2 页面打开时如何加载历史

推荐流程：

```text
打开会话
  │
  ├─ GET 会话元数据、状态投影和最近消息
  │
  ├─ 返回 active_turn / pending_action
  │
  ├─ 前端渲染最近消息和状态卡片
  │
  ├─ 使用 cursor 向上分页加载更早消息
  │
  └─ 从 last_seq 连接 SSE，接收后续事件
```

建议接口：

```text
GET /api/workbench/conversations/{conversation_id}
GET /api/workbench/conversations/{conversation_id}/timeline?cursor=&limit=50
GET /api/workbench/conversations/{conversation_id}/state
GET /api/workbench/conversations/{conversation_id}/stream?after_seq=<last_seq>
```

首次打开不需要一次性加载全部历史，默认加载最近 50 条可展示消息。用户向上滚动时，根据游标分页加载更早消息。

服务端响应建议包含：

```json
{
  "conversation": {
    "conversation_id": "...",
    "title": "...",
    "status": "active",
    "latest_seq": 128
  },
  "state": {
    "summary": "...",
    "active_turn_id": null,
    "pending_action": null
  },
  "items": [],
  "next_cursor": "..."
}
```

前端以 `message_id` 和 `event_id` 去重，以服务端 `seq` 排序，不依赖客户端时间戳排序。

### 22.3 如何基于历史继续执行普通问答

普通问答继续执行时，不重新跑历史回合，也不直接把所有原始事件交给模型。

后端按以下顺序构建本次模型上下文：

1. 读取当前用户和权限作用域；
2. 读取 `disp_agent_conversation_state`；
3. 读取最近若干个已完成回合的用户消息和助手最终回答；
4. 读取长会话摘要；
5. 读取当前仍有效的派工草稿或业务状态；
6. 根据当前问题按需检索更早的相关消息；
7. 生成新的 `turn`，使用 `expected_version` 做乐观锁校验；
8. 执行新回合并追加新的消息和事件。

接口示例：

```text
POST /api/workbench/conversations/{conversation_id}/turns
{
  "client_request_id": "...",
  "idempotency_key": "...",
  "parent_message_id": "...",
  "expected_version": 128,
  "user_text": "继续帮我查看刚才的派工状态"
}
```

`parent_message_id` 用于确定用户是基于哪一条消息继续，`expected_version` 用于防止多个页面或多个请求同时修改同一会话。

### 22.4 如何恢复未完成回合

需要把未完成回合分成三种情况处理。

#### 情况一：等待用户确认

例如派工草稿已经准备好，当前状态是 `waiting_confirmation`：

- 页面加载时读取 `pending_action`；
- 展示原来的确认卡片；
- 用户点击确认、取消或修改时，调用带幂等键的确认接口；
- 不重新识别原始命令，直接继续该回合的确认阶段。

建议接口：

```text
POST /api/workbench/turns/{turn_id}/confirm
POST /api/workbench/turns/{turn_id}/cancel
POST /api/workbench/turns/{turn_id}/revise
```

#### 情况二：执行进程仍在运行

- 页面连接 `stream?after_seq=...`；
- 服务端补发断线期间已经产生的事件；
- 后续事件继续实时推送；
- 前端按 `event_id` 去重。

#### 情况三：进程已中断

- 根据 checkpoint 判断最后完成阶段；
- 检查现有派工业务表和幂等记录，确认是否已经产生业务副作用；
- 如果未产生副作用，从安全 checkpoint 重试；
- 如果已经提交或状态不明确，先查询业务结果，禁止盲目重新提交；
- 将恢复动作记录为新的事件，必要时创建新的 retry turn。

对于 CREATE/MUTATION 类命令，恢复必须依赖现有命令锁定、提交记录和幂等键，不能只依赖聊天文本判断是否已经执行过。

### 22.5 如何继续一个新的派工命令

新的派工命令仍然创建新的 `turn`，但上下文来源包括：

- 最近对话中的用户和助手消息；
- 当前会话摘要；
- 最新派工草稿快照；
- 当前命令锁定状态；
- 当前用户权限作用域。

如果上一个回合处于 `waiting_confirmation`，默认不允许直接创建第二个可变更回合，应先处理确认、取消或修改。这样可以避免一个会话同时存在两个互相覆盖的派工草稿。

建议第一版采用“一会话一个 active turn”的并发策略：

- 有 active turn 时，新的写操作返回 `409 TURN_IN_PROGRESS`；
- 只读历史查询仍然可用；
- 后续如果确有需求，再扩展为多个并行 turn，但必须引入草稿分支和冲突合并规则。

### 22.6 历史对话如何展示

前端不要直接展示所有原始事件，而是构造一个可读时间线：

```text
用户消息
  ↓
助手回答
  ↓
派工草稿卡片 / 确认卡片
  ↓
工具调用（默认折叠）
  ↓
工具结果摘要（默认折叠）
  ↓
最终结果
```

建议展示规则：

- `user` 和 `assistant/final` 作为主聊天气泡；
- `card`、`confirmation`、`progress` 使用结构化组件；
- `tool_started/tool_finished` 合并成一个可展开的“执行详情”；
- 原始工具输入输出默认折叠，并按权限决定是否可见；
- 错误、取消和超时显示明确的终态；
- 历史回填记录显示“历史补录，内容可能不完整”；
- 老消息如果 payload 版本过旧，使用兼容渲染器或降级为文本摘要。

### 22.7 实时更新和断线恢复

推荐使用 SSE 作为第一版实时通道：

```text
GET /api/workbench/conversations/{conversation_id}/stream?after_seq=128
```

每条事件必须携带：

- `event_id`
- `seq`
- `event_type`
- `turn_id`
- `message_id`（如果有）
- `payload_version`

断线重连时前端发送上次收到的 `after_seq`，服务端先补发缺失事件，再切换到实时推送。若序号已经超出保留窗口，服务端返回“需要重新加载时间线”，前端重新拉取最新快照。

### 22.8 “展示历史”和“恢复执行”的安全边界

历史记录只能证明过去发生了什么，不能直接授予当前执行权限。

继续执行前必须重新校验：

- 当前登录用户；
- 当前租户和权限作用域；
- 派工对象是否仍然有效；
- 草稿和命令是否已过期；
- 业务状态是否已经发生变化；
- 原确认是否仍然在有效期内。

尤其是取消、改期、改派和正式提交，恢复时必须重新经过受控变更服务的校验，不能因为历史中存在“用户确认”就无条件执行。

## 23. 推荐的端到端示例

### 23.1 用户刷新页面

1. 前端读取当前 `conversation_id`；
2. 请求最近 50 条消息和会话状态；
3. 展示历史气泡和最后一次派工结果；
4. 如果存在待确认动作，展示确认卡片；
5. 使用最后的 `latest_seq` 建立 SSE；
6. 后续进度和最终结果实时补到当前时间线。

### 23.2 用户继续提问

1. 前端提交 `user_text + client_request_id + expected_version`；
2. 服务端创建新的 `turn` 和用户消息；
3. 读取摘要、最近消息和当前派工状态；
4. 执行智能体；
5. 按事件和消息实时推送；
6. 完成后更新会话状态投影。

### 23.3 服务中途重启

1. 页面重新加载会话状态；
2. 发现回合处于 `running` 但没有活动 worker；
3. 读取 checkpoint 和派工幂等记录；
4. 判断是安全重试、等待业务查询，还是标记为人工处理；
5. 前端展示“已恢复”“等待确认”或“执行状态待核验”，不会把请求当成从未发生。

## 24. 本方案对前一版的补充结论

前一版的四层模型仍然保留，但要增加一个读取和续接用的状态投影：

```text
conversation
  ├─ state projection       ← 加载和继续执行的当前状态
  ├─ turn
  │    ├─ message            ← 恢复聊天展示
  │    └─ event              ← 恢复进度和审计时间线
  └─ checkpoint              ← 恢复中断的长流程
```

最终原则是：

1. **历史展示**从消息表和可展示事件读取；
2. **继续执行**从状态投影、摘要、最新业务状态和必要历史构建上下文；
3. **中断恢复**从 checkpoint 加幂等业务记录判断是否可以重试；
4. **正式变更**永远重新做权限、时效和业务状态校验；
5. **前端缓存**只做加速，不作为可靠历史或执行依据。

## 25. 当前实施版本：轻量 MVP

结合当前用户数量和使用方式，第一版不采用前面完整方案中的全部能力，先实现简单、可用、容易维护的版本。

### 25.1 第一版只保留三张核心表

```text
disp_agent_conversation
  └── disp_agent_conversation_turn
          └── disp_agent_conversation_message
```

第一版暂不新增或不启用：

- 权限作用域校验；
- `conversation_state` 状态投影；
- checkpoint 和断点恢复；
- 取消、重置和中止执行；
- 复杂事件重放；
- SSE/WebSocket 实时补发；
- 多回合并行执行；
- 长会话自动摘要。

### 25.2 MVP 表职责

#### `disp_agent_conversation`

每个 `thread_id` 一条会话记录，保存：

- `conversation_id`
- `thread_id`
- `user_id`（可选，第一版只做记录，不做权限判断）
- `title`
- `created_at`
- `last_activity_at`
- `status`

#### `disp_agent_conversation_turn`

每次用户发送一条消息创建一条回合，保存：

- `turn_id`
- `conversation_id`
- `user_message_id`
- `assistant_message_id`
- `intent_type`
- `status`
- `tool_trace_json`（可选，保存简要工具轨迹）
- `created_at`
- `completed_at`

#### `disp_agent_conversation_message`

保存实际展示给用户的消息：

- `message_id`
- `conversation_id`
- `turn_id`
- `seq`
- `role`
- `message_type`
- `content_text`
- `payload_json`
- `created_at`

第一版至少保存：

- 用户文本；
- 助手最终文本；
- 派工草稿或确认卡片（如果前端需要恢复）；
- 错误提示。

工具的完整原始输入输出暂不要求全部保存，只保存现有 `tool_trace` 或摘要。

### 25.3 MVP 如何加载历史

页面打开时直接按 `thread_id` 查询消息：

```text
GET /api/workbench/conversations/{thread_id}/messages
```

第一版可以直接按时间顺序返回全部消息；为防止单个会话无限增长，建议先设置一个简单上限，例如最近 200 条消息。超过上限后，再增加分页，不在第一版引入复杂游标机制。

前端展示：

- `role=user` 显示用户气泡；
- `role=assistant` 显示助手气泡；
- `message_type=card` 按现有卡片组件展示；
- `message_type=error` 显示错误状态；
- `sessionStorage` 只做临时缓存，不再作为唯一历史来源。

### 25.4 MVP 如何继续对话

用户发送新消息时，后端执行以下简单流程：

1. 根据 `thread_id` 获取或创建会话；
2. 查询该会话最近若干条消息；
3. 创建新的 `turn` 和用户消息；
4. 将最近历史加上当前问题发送给智能体；
5. 保存助手最终回答；
6. 更新回合状态为 `completed` 或 `failed`；
7. 返回本回合的用户消息和助手消息。

上下文建议先沿用现有约束：最近 10 个已完成回合或约 6000 字符，避免把整张历史表全部发给模型。

接口可以简化为：

```text
POST /api/workbench/conversations/{thread_id}/messages
{
  "user_text": "继续帮我处理刚才的派工"
}
```

第一版不实现“恢复上次中断进程”。如果上一轮执行失败、超时或服务重启，直接把该回合标记为 `failed`，用户重新发问即可。重新发问时，系统仍然会读取最近历史和当前派工草稿状态。

### 25.5 派工命令如何处理

现有派工业务表继续负责真实业务状态：

- 草稿仍读 `disp_agent_dispatch_submission_draft`；
- 命令准备结果仍读 `disp_agent_dispatch_prepare_outcome`；
- 正式提交仍读 `disp_agent_dispatch_submission`。

统一会话表只负责把用户原话、助手回答和必要卡片串起来。用户下一次重新发问时，后端读取最近对话加当前草稿，不需要恢复旧进程。

第一版不做额外的“继续执行”按钮，也不根据旧消息自动重复提交 CREATE/MUTATION 命令。需要继续时，由用户重新输入或确认，仍走现有幂等和受控变更逻辑。

### 25.6 MVP 验收标准

第一版只要求：

1. 每次用户消息和助手最终回答都能写入数据库；
2. 刷新页面后能按顺序恢复历史气泡；
3. 新问题能够读取最近历史并继续对话；
4. 派工草稿和确认卡片可以恢复展示；
5. 普通问答和派工命令使用同一个 `thread_id` 时间线；
6. 失败时保存失败状态，用户重新发问即可；
7. 不重复改造现有派工业务表；
8. 不引入权限、断点恢复和复杂实时通道。

### 25.7 后续再考虑的能力

只有当实际使用中出现需求时，再增加：

- 会话权限隔离；
- 状态投影；
- checkpoint 和断点恢复；
- SSE 实时补发；
- 分页和全文检索；
- 长会话摘要；
- 审计事件时间线；
- 多用户并发和会话分支。

## 26. 关于三张表的取舍：当前 MVP 建议改为两张表

三张表分别代表：

- 会话：一条聊天线程；
- 回合：一次用户输入到处理结束；
- 消息：用户、助手和卡片等具体内容。

这种拆分适合需要独立管理回合状态、重试、审计和并发控制的系统，但对当前 MVP 来说偏复杂。

### 26.1 当前建议的两张表

```text
disp_agent_conversation
  └── disp_agent_conversation_message
```

#### `disp_agent_conversation`

只保存会话级信息：

- `conversation_id`
- `thread_id`
- `title`
- `created_at`
- `last_activity_at`
- `status`

#### `disp_agent_conversation_message`

保存所有消息，并使用 `turn_id` 把一次用户输入和对应输出归为同一回合：

- `message_id`
- `conversation_id`
- `turn_id`
- `role`
- `message_type`
- `content_text`
- `payload_json`
- `intent_type`
- `status`
- `tool_trace_json`
- `created_at`

同一个 `turn_id` 下可以有：

```text
user       用户原话
assistant   助手最终回答
card        派工草稿或确认卡片（可选）
error       错误提示（可选）
```

### 26.2 两张表如何满足当前需求

加载历史：

```sql
SELECT *
FROM disp_agent_conversation_message
WHERE conversation_id = :conversation_id
ORDER BY created_at, message_id;
```

继续对话：

1. 查询最近若干条消息；
2. 创建新的 `turn_id`；
3. 插入用户消息；
4. 调用智能体；
5. 插入助手回答或卡片；
6. 更新会话的 `last_activity_at`。

失败处理：

- 插入一条 `message_type=error` 的消息；
- 将该消息的 `status` 设为 `failed`；
- 用户重新输入时创建新的 `turn_id`；
- 不做旧回合恢复和中断重置。

### 26.3 为什么仍然保留会话表

如果完全只用一张消息表，也能实现当前功能，但会话表仍然有几个低成本价值：

- 保存会话标题和最近活动时间；
- 以后做会话列表时不需要从消息表聚合；
- 可以标记会话是否关闭或归档；
- 以后增加权限、摘要或保留期限时有放置位置。

因此当前最终建议是：**两张表，不单独建回合表；用 `turn_id` 做逻辑分组。**

如果后续出现独立的回合状态、重试、并发控制或断点恢复需求，再把 `turn_id` 从消息字段升级为独立的 `disp_agent_conversation_turn` 表，不影响前端消息模型。

## 27. 最终开发交接说明（两表 MVP）

本节是交给其他模型实施的最终版本。除本节明确列出的内容外，第一版不要扩展到权限、断点恢复、事件重放或复杂实时通道。

### 27.1 第一版范围

必须实现：

1. 每个 `thread_id` 对应一条会话记录；
2. 每轮用户输入和助手输出都写入消息表；
3. 派工草稿、确认卡片和错误提示可以作为消息恢复展示；
4. 页面刷新后从数据库加载历史；
5. 用户重新提问时读取最近历史继续对话；
6. 普通问答和派工命令使用同一个会话时间线；
7. 失败后保存失败消息，用户重新输入即可。

明确不实现：

- 权限过滤和跨用户隔离；
- 中止、取消、重置和断点续跑；
- checkpoint、事件表和状态投影；
- SSE/WebSocket 补发；
- 会话分支和并行回合；
- 自动摘要和历史全文检索。

### 27.2 表结构草案

以下为逻辑结构，开发时按项目现有 ORM、命名规范和 schema 版本机制落地，不要直接在生产库执行临时 DDL。

#### `disp_agent_conversation`

```sql
conversation_id   UUID PRIMARY KEY
thread_id         VARCHAR(128) NOT NULL UNIQUE
user_id           VARCHAR(128) NULL
title             TEXT NULL
status            VARCHAR(20) NOT NULL DEFAULT 'active'
created_at        TIMESTAMPTZ NOT NULL
last_activity_at  TIMESTAMPTZ NOT NULL
updated_at        TIMESTAMPTZ NOT NULL
```

建议索引：

```text
idx_disp_agent_conversation_last_activity
```

`user_id` 第一版只保留，不做权限判断；后续需要权限时可以直接使用。

#### `disp_agent_conversation_message`

```sql
message_id        UUID PRIMARY KEY
conversation_id   UUID NOT NULL
turn_id           UUID NOT NULL
seq               BIGINT NOT NULL
role              VARCHAR(20) NOT NULL
message_type      VARCHAR(30) NOT NULL DEFAULT 'text'
content_text      TEXT NULL
payload_json      JSONB NULL
intent_type       VARCHAR(50) NULL
status            VARCHAR(20) NOT NULL DEFAULT 'completed'
tool_trace_json   JSONB NULL
client_request_id VARCHAR(128) NULL
created_at        TIMESTAMPTZ NOT NULL
updated_at        TIMESTAMPTZ NOT NULL
```

建议约束和索引：

```text
UNIQUE (conversation_id, seq)
INDEX  (conversation_id, created_at, message_id)
INDEX  (conversation_id, turn_id)
```

`role` 第一版支持：

```text
user / assistant / system
```

`message_type` 第一版支持：

```text
text / card / confirmation / error
```

工具过程不单独建事件表；如果必须展示工具过程，将摘要放进 `tool_trace_json` 或一条 `system` 消息中。

### 27.3 写入流程

建议封装一个轻量的 `ConversationRecorder`，至少提供：

```text
get_or_create_conversation(thread_id)
append_message(...)
next_seq(conversation_id)
```

普通问答或派工请求的流程：

1. 根据 `thread_id` 获取或创建会话；
2. 生成新的 `turn_id`；
3. 插入用户消息；
4. 查询最近 10 个回合或约 6000 字符作为模型上下文；
5. 执行现有智能体逻辑；
6. 派工草稿、确认卡片如需恢复展示，则插入 `card` 或 `confirmation` 消息；
7. 正常完成时插入 `assistant/text` 消息；
8. 失败时插入 `error` 消息；
9. 更新会话的 `last_activity_at`。

用户消息和最终助手消息必须落库。中间卡片可以按现有前端需要落库，不要求保存每个 token。

### 27.4 推荐接口

接口路径以现有工作台路由为准，以下是最小能力集合：

```text
GET  /api/workbench/conversations/{thread_id}
GET  /api/workbench/conversations/{thread_id}/messages
POST /api/workbench/conversations/{thread_id}/messages
```

发送消息请求：

```json
{
  "user_text": "继续帮我处理刚才的派工",
  "client_request_id": "optional-client-id"
}
```

返回内容至少包含：

```json
{
  "thread_id": "...",
  "turn_id": "...",
  "messages": [
    {
      "message_id": "...",
      "role": "user",
      "message_type": "text",
      "content_text": "..."
    },
    {
      "message_id": "...",
      "role": "assistant",
      "message_type": "text",
      "content_text": "..."
    }
  ]
}
```

历史接口第一版可以返回最近 200 条消息。超过 200 条时先返回最近 200 条并在响应中预留 `has_more` 字段，真正分页可以后续增加。

### 27.5 前端改造要求

页面初始化：

1. 读取当前 `thread_id`；
2. 请求历史消息接口；
3. 按 `seq` 渲染用户气泡、助手气泡和卡片；
4. 请求失败时保留本地缓存作为临时降级展示。

发送消息：

1. 发送到后端；
2. 以返回结果为准更新当前消息；
3. 成功后更新本地缓存；
4. 失败时显示已持久化的错误消息；
5. 不把 `sessionStorage` 当作唯一历史来源。

第一版可以继续使用现有同步响应或已有流式机制，但最终回答完成后必须统一写入消息表。

### 27.6 现有业务表的兼容方式

现有业务表继续按当前逻辑使用：

- 普通问答继续写 `disp_agent_feedback`；
- 派工草稿、准备结果和正式提交继续写现有 `disp_agent_dispatch_*` 表；
- MCP 调用继续写 `disp_agent_integration_call_log`。

会话消息表负责完整聊天展示，不替换业务表。第一版不强制修改这些表的字段；如需追溯，可在后续增加 `conversation_id` 或 `message_id` 关联字段。

### 27.7 测试与验收

开发模型至少需要补充以下测试：

1. 新 `thread_id` 自动创建会话；
2. 同一线程按 `seq` 保存多轮消息；
3. 刷新后历史顺序正确；
4. 普通问答保存用户消息和助手回答；
5. 派工命令保存用户原话和必要卡片；
6. 智能体异常时保存 `error` 消息；
7. 最近历史能够被下一轮请求读取；
8. 超过 200 条消息时不影响最近消息加载；
9. 同一个 `conversation_id + seq` 不产生重复序号；
10. 测试使用项目规定的本地测试库，不连接生产库。

### 27.8 Review 清单

其他模型开发完成后，重点 review：

- 是否确实只有两张新增会话表；
- 是否没有偷偷引入权限、checkpoint、事件表或复杂恢复机制；
- 普通问答路径是否写入消息表；
- `command-completed` 派工路径是否也写入消息表；
- 失败路径是否落库，而不是只返回错误；
- 前端刷新是否从数据库加载历史；
- 新问题是否使用最近历史而不是全部历史；
- `seq` 是否由服务端生成并保持稳定；
- 是否保留现有 `disp_agent_feedback` 和派工业务流程；
- 测试是否遵守本地数据库约束。

### 27.9 最终决策

当前版本最终采用：

```text
两张新增表
  ├─ disp_agent_conversation
  └─ disp_agent_conversation_message

turn_id 只作为消息表中的逻辑分组字段
失败后用户重新提问
不实现中止、重置、断点恢复和权限控制
```
