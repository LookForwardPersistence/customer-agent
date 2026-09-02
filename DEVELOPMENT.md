# Aurora Tech Store 客服智能体 · 开发设计文档

> 配套文档：`ARCHITECTURE.md`（架构决策）· 本文档聚焦**工程实现**：模块详细设计、接口契约、数据结构、前端组件、评估设计与调试指南。
> 技术栈：Python 3.10+ / LangChain 1.x / LangGraph / FastAPI / 原生 JS

---

## 1. 开发环境与工程约定

### 1.1 环境搭建

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填 OPENAI_API_KEY（任何 OpenAI 兼容服务商）
```

`.env` 配置项（`app/main.py` 启动时 `load_dotenv()` 加载）：

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | LLM 服务商 key | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `OPENAI_MODEL` | 模型名 | `deepseek-chat` |

### 1.2 依赖清单（刻意保持最小）

| 包 | 用途 |
| --- | --- |
| `langchain` / `langchain-openai` | 模型接入（`ChatOpenAI`） |
| `langgraph` | ReAct agent 编排 + MemorySaver checkpointer |
| `fastapi` / `uvicorn` | HTTP 服务 |
| `python-dotenv` | .env 加载 |

**不引入**：向量库、embedding 服务、前端框架、ORM——检索用零依赖 bigram，前端单文件原生 JS，数据 JSON 文件直读。理由见 ARCHITECTURE.md §1 时间盒约束。

### 1.3 编码约定

- 所有工具返回 **JSON 字符串**（`json.dumps(..., ensure_ascii=False)`），LLM 消费、测试可 `json.loads` 断言
- 业务错误不抛异常穿越工具边界：工具内捕获 `OrderAPIError`，转成 `{"ok": false, "error_code", "message"}` 结构化返回
- 中文 docstring 写在工具函数上——它就是 LLM 看到的工具说明，参数说明必须清楚
- 模块级单例：`kb`、`order_api`、`sessions` 全局唯一，与真实服务客户端同生命周期

---

## 2. 模块详细设计

### 2.1 `app/knowledge_base.py` — 知识库与检索

```python
class KnowledgeBase:
    def __init__(self, path: Path | None = None)   # 加载 JSON，预计算 bigram 索引
    def search(self, query: str, top_k: int = 3, min_score: float = 0.12) -> list[dict]

kb = KnowledgeBase()   # 模块级单例
```

**检索算法**：查询与条目各取字符 bigram 集合（`topic` 权重 ×3 拼接 `content`），分数 = 交集大小 / 查询 bigram 数。返回 `[{id, topic, content, score}]`，按分数降序截取 top_k。

**实测分数分布**（决定 confidence 阈值 0.25 的依据）：

| 查询类型 | top 分数 |
| --- | --- |
| 强相关（退货政策/运费/保修） | 0.26 – 0.56 |
| 无关（手机壳颜色） | 0.217 |

**替换向量检索**：只需重写 `search()` 保持签名不变，上层（工具层/prompt）零改动。

### 2.2 `app/mock_backend.py` — 订单后端（可替换契约）

```python
class OrderAPIError(Exception):
    code: str      # 稳定错误码，映射见下表
    message: str   # 面向客户的中文说明

class OrderAPI(Protocol):
    def get_order(self, order_id: str) -> dict
    def create_return(self, order_id: str, reason: str) -> dict

class MockOrderAPI:          # 实现 OrderAPI，内存态
    def validate_return(self, order_id: str, reason: str) -> dict   # 只读预校验

order_api: OrderAPI = MockOrderAPI()   # ★ 接真实 OMS 时改这一行
```

**错误码契约**（agent 失败处理与评估用例共同依赖，改动需跑回归）：

| code | 触发条件 | 演示订单 |
| --- | --- | --- |
| `ORDER_NOT_FOUND` | 订单号不存在 | AT-99999 |
| `ORDER_ALREADY_RETURNED` | 已有退货流程 | 重复申请任意单 |
| `RETURN_NOT_ALLOWED` | 定制商品非质量问题 | AT-10077 |
| `BACKEND_TIMEOUT` | 注入的瞬时故障 | AT-10099 |

**`validate_return` 业务规则**（只读，返回方案不写入）：

1. 订单存在性检查
2. `return_status` 非空 → 拒绝重复退货
3. 商品名含"定制" 且原因不含 质量/坏了/故障 → 政策拒绝（呼应 KB-001 定制条款）
4. 计算退款额 `Σ price × qty`，返回 `{order_id, reason, items, refund_amount, policy}`

**`create_return`**：重跑 validate → AT-10099 注入超时 → 加锁生成 `RT-` 工单、写 `order.return_status`。**只有 API 层的确认端点调用它**。

### 2.3 `app/tools.py` — 工具层（LLM 可见边界）

| 工具 | 类型 | 返回关键结构 |
| --- | --- | --- |
| `search_knowledge_base(query)` | 只读 | `{found, confidence: high/low, hint?, results[]}` |
| `get_order_status(order_id)` | 只读 | `{ok, order{status, items, total, tracking, eta, return_status}}` |
| `propose_return(order_id, reason)` | **提议**（不写） | `{status: NEEDS_CONFIRMATION, proposal{...}}` 或 `{status: ERROR, error_code}` |
| `handoff_to_human(reason, summary)` | 记录 | `{status: HANDOFF_QUEUED, reason, summary}` |

confidence 规则：`top score ≥ 0.25 → high`；否则 `low` 且附 hint"仅当某条结果确实能回答问题时才使用它；否则如实告知无法确认"。这是防误杀的设计——阈值只做初筛（min_score=0.12），语义判断交给 LLM 消化 hint。

**安全不变量**：`TOOLS` 列表刻意不含 `create_return`。验证方式：`grep -n "create_return" app/tools.py` 只出现在注释；`TOOLS = [...]` 四项均无写操作。

### 2.4 `app/store.py` — SessionStore（确认状态机载体）

```python
class SessionStore:
    def set_pending(sid, action) -> None          # 挂起待确认方案
    def pop_pending(sid) -> dict | None           # 原子取出并清空（确认/取消共用）
    def get_pending(sid) -> dict | None
    def set_handoff(sid, reason, context) -> dict # 生成 HO-xxxx 记录
    def log(sid, event) -> None                   # 审计事件追加
    def snapshot(sid) -> dict                     # {pending_action, handoff, events}

sessions = SessionStore()   # 线程安全（threading.Lock），进程内存
```

**审计事件类型**（`events` 列表，每条带 `ts`）：

`user_message` → `return_proposed` → `confirmed_by_user` / `cancelled_by_user` → `return_executed` / `return_failed`；另有 `handoff`、`config_error`。

**设计要点**：`pop_pending` 是原子操作，天然防止重复执行（取出即失效，第二次 confirm 返回 400）。

### 2.5 `app/agent.py` — LangGraph ReAct 智能体

```python
build_model() -> ChatOpenAI        # temperature=0.2；无 key 时 raise RuntimeError（上层友好提示）
build_agent() -> ReAct agent       # create_react_agent(model, TOOLS, SYSTEM_PROMPT, MemorySaver)
get_agent()                        # 懒加载单例
```

**System prompt 结构**（8 条规则，全部服务于作业评分点）：

| 规则 | 服务的要求 | 关键约束 |
| --- | --- | --- |
| 人设与语气 | #1 对话体验 | 中文口语、一次说清一件事 |
| 1 店铺信息先检索 | #2 grounded | 政策/商品/流程一律先调 KB，禁止跳过检索 |
| 2 绝不编造 | #2 grounded | found=false →"暂时无法确认"+ 建议转人工 |
| 3 订单问题先查询 | #2/#3 | 失败如实转告，不假设状态 |
| 4 退货走确认流程 | #3 安全操作 | 信息齐→直接 propose；打字"确认"不算数 |
| 5 范围外请求 | #4 边界 | 第二次坚持/投诉 → **必须**转人工 |
| 6 转人工保留上下文 | #4 边界 | summary 必含诉求+订单号+已尝试处理 |
| 7 失败处理 | #3 失败分支 | 两次失败主动转人工 |
| 8 系统通知 | #3 可观测 | `[系统事件]` 前缀的回注消息，不复述该词 |

**多轮记忆**：`MemorySaver` checkpointer，`thread_id = session_id`——每个会话独立消息历史，LangGraph 自动按 thread 存取。

### 2.6 `app/main.py` — FastAPI 服务层

**核心函数**：

```python
_extract_turn_info(new_messages) -> dict
    # 遍历本轮新增消息，提取 reply / sources / trace / pending / handoff
    # ai 消息：有 tool_calls → 记 trace；无 → 取 content 为 reply
    # tool 消息：按工具名分流——search→sources，propose→pending，handoff→handoff
    # ★ 只提取工具级事件，LLM 推理永不出服务端

_run_agent(sid, human_text) -> dict
    # 无 key → 友好提示（不崩）；否则 invoke → diff 出本轮新消息 → 提取 →
    # pending 存 SessionStore + 审计；handoff 建 HO 记录 + 审计 → 返回会话快照
```

**Turn 提取技巧**：invoke 前记录消息数 `before`，取 `result["messages"][before:]` 只处理本轮增量——多轮对话下每轮 trace 只含当轮事件。

**确认状态机端点**（写路径唯一入口）：

```python
POST /api/session/confirm   # pop_pending → create_return → 成功/失败包装为
                            # "[系统事件]…" 消息回注 agent 播报 → 审计
POST /api/session/cancel    # pop_pending → 作废事件回注 → 审计
```

回注而非直接拼回复的理由：执行结果需要 agent 用人设语气告知客户（含下一步指引），且失败时按规则 7 处理——即**确定性执行 + LLM 表达**的分工。

---

## 3. API 接口契约

### 3.1 `POST /api/chat`

**请求**：`{"session_id": "abc123", "message": "AT-10092 不想要了，帮我退货"}`（message 非空，否则 400）

**响应**：

```json
{
  "reply": "好的，已为您生成退货方案……请点击确认按钮。",
  "sources": [{"id": "KB-001", "topic": "退货政策"}],
  "trace": [
    {"type": "tool_call", "name": "get_order_status", "detail": "{\"order_id\": \"AT-10092\"}"},
    {"type": "tool_result", "name": "get_order_status", "detail": "{\"ok\": true, ...}"}
  ],
  "pending_action": {
    "action": "create_return", "order_id": "AT-10092",
    "items": ["无线蓝牙耳机"], "refund_amount": 499.0,
    "policy": "审核通过后 3-5 个工作日内原路退回", "proposed_at": "2026-09-02 17:20:11"
  },
  "handoff": null,
  "events": [{"ts": "17:20:10", "event": "user_message", "text": "AT-10092…"}]
}
```

字段语义：`sources` 仅当检索命中；`pending_action` 仅当本轮生成方案（否则回落快照值）；`trace` 每项 detail 截断 300–400 字符。

### 3.2 其余端点

| 端点 | 请求 | 响应 / 错误 |
| --- | --- | --- |
| `POST /api/session/new` | — | `{"session_id": "12位hex"}` |
| `POST /api/session/confirm` | `{"session_id"}` | 同 chat 响应（含执行结果播报）；无 pending → 400 |
| `POST /api/session/cancel` | `{"session_id"}` | 同上（方案作废播报）；无 pending → 400 |
| `GET /api/session/{sid}/state` | — | `{pending_action, handoff, events}` 快照 |
| `GET /` | — | 聊天 UI（static/index.html） |

---

## 4. 数据结构

### 4.1 `app/data/knowledge_base.json`

```json
{
  "store": {"name": "Aurora Tech Store", "service_hours": "9:00-21:00"},
  "entries": [
    {"id": "KB-001", "topic": "退货政策", "content": "自签收之日起 30 天内……定制类商品（刻字手机壳）不支持无理由退货……"}
  ]
}
```

14 条覆盖：退货、运费、配送、保修、支付、发票、会员、优惠券、价保、服务时间等。`id` 是来源 chip 与评估断言的锚点，**编号稳定不复用**。

### 4.2 `app/data/orders.json`

```json
{"orders": [{
  "order_id": "AT-10092", "status": "待发货",
  "items": [{"name": "无线蓝牙耳机", "qty": 1, "price": 499.0}],
  "total": 499.0, "placed_at": "2026-08-25",
  "carrier": null, "tracking_no": null, "eta": null, "return_status": null
}]}
```

演示数据矩阵：AT-10086（已发货，查物流）、AT-10092（待发货，退货主场景）、AT-10077（定制商品，政策拒绝）、AT-10099（执行超时注入）、AT-10050（已有退货流程）。

---

## 5. 前端设计（`app/static/index.html`，单文件约 315 行）

**状态**：`state = { sessionId, busy }`——busy 期间禁用发送，防并发请求打乱消息序。

| 组件 | 触发 | 行为 |
| --- | --- | --- |
| 气泡 `addBubble(role, text)` | 每轮 | user 右侧品牌色 / bot 左侧带头像；`textContent` 赋值（防 XSS） |
| 来源 chip `addMetaSources` | `sources` 非空 | 答案下方绿色胶囊 `KB-001 退货政策` |
| 确认卡片 `renderPending` | `pending_action` 非空 | 琥珀色卡片：订单/商品/退款额/政策 + 确认/取消按钮；点击后禁用按钮（防双击重复提交） |
| 转人工横幅 `renderHandoff` | `handoff` 非空 | 红色横幅 + HO 编号 + 上下文摘要（折叠于白色块） |
| Trace 面板 `renderTrace` | 每轮 | 右侧 320px 可折叠面板：`→ 调用` / `← 结果` 等宽字体条目 |
| typing 指示 | 请求期间 | 三点闪烁动画 |

**交互流**：页面加载即 `POST /api/session/new` 取 sessionId；快捷问题 chip 一键发送；确认/取消走 `resolveAction(act)` → `POST /api/session/{act}`，响应统一走 `handleResponse` 渲染（与 chat 完全同构）。

---

## 6. 评估设计（`evaluation/`）

### 6.1 三层结构与运行方式

```bash
python -m evaluation.run_eval                        # 确定性层（无需 key）
OPENAI_API_KEY=... python -m evaluation.run_eval     # + agent 层 live 用例
```

Runner 顶层 `load_dotenv()`；结束后自动重写 `results.md`（表格 + 覆盖映射）。

| 层 | 用例 | 断言方式 |
| --- | --- | --- |
| retrieval | TC-01/02/03/13 前置、TC-14 前置 | 直接 `json.loads(工具返回)` 断言 id/score/confidence |
| tool + state_machine | TC-05~TC-11 | 工具契约断言 + 独立 `MockOrderAPI`+`SessionStore` 实例上跑 propose→confirm/cancel 全链路 |
| agent | TC-04/06-agent/12/13/14 | `agent.invoke` 后遍历消息：检查调用了哪些工具、最终回复内容、**全程无 create_return 字样** |

### 6.2 用例 → 需求映射（15 个）

| 作业要求 | 用例 |
| --- | --- |
| Grounded 回答 | TC-01、TC-02（命中+来源） |
| 未知/模糊不编造 | TC-03、TC-04、TC-14 |
| 知识冲突纠正 | TC-13（客户称终身保修 → 基于 KB-004 纠正） |
| 安全操作 | TC-05、TC-06、TC-06-agent（确认前零写入） |
| 确认/取消 | TC-07（取消无写入）、TC-08（确认生成工单） |
| 失败处理 | TC-09（订单不存在）、TC-11（后端超时） |
| 政策边界 | TC-10（定制商品拒绝） |
| 转人工 | TC-12（拒绝→坚持→转接+摘要） |

### 6.3 LLM 非确定性容错（实测踩坑固化）

- TC-12：第二轮坚持后未转接时，**允许追加第三轮**"我就是要投诉，赶紧给我转人工"再断言（LLM 偶发只在口头拒绝）
- TC-13 断言只检查"没有**认可**终身保修"（回复引用客户原话来否定是合法的），不做"词不存在"的脆断言
- 断言工具调用比断言回复文本可靠：优先 `any(t[0] == "propose_return" for t in tools)` 这类结构性检查

**开发铁律：改 prompt 或工具层必须跑全量回归**——TC-13/14 正是靠该流程发现"商品类问题跳过检索"漏洞的。

---

## 7. 调试指南

```bash
# 1. 单测式调试某一层（不启服务）
python -c "from app.tools import search_knowledge_base as t; print(t.invoke({'query': '退货政策'}))"

# 2. 看 agent 实际轨迹（工具调用序列 + 最终回复）
python -c "
from dotenv import load_dotenv; load_dotenv()
from app.agent import get_agent
out = get_agent().invoke({'messages': [('user', 'AT-10092 退货')]}, {'configurable': {'thread_id': 'dbg'}})
for m in out['messages']:
    if m.type == 'ai':
        [print('CALL:', c['name'], c['args']) for c in (m.tool_calls or [])]
        if not m.tool_calls: print('REPLY:', m.content)
"

# 3. API 层冒烟（不起 uvicorn）
python -c "
from app.main import app
from fastapi.testclient import TestClient
c = TestClient(app)
print(c.post('/api/chat', json={'session_id': 's', 'message': '你好'}).json()['reply'])
"

# 4. 服务运行
uvicorn app.main:app --reload --port 8000
```

**常见问题**：

| 现象 | 原因与处理 |
| --- | --- |
| 回复"无法连接大脑" | `.env` 未配 key 或未重启服务 |
| 评估 agent 层显示 `[skip]` | runner 找不到 key——确认在项目根目录运行且 `.env` 存在 |
| confirm 返回 400 | pending 已被消费（确认/取消只能一次），属预期防重设计 |
| 检索不到预期条目 | 打印 `kb.search(query)` 看 score；语义改写弱是 bigram 已知限制 |

---

## 8. 扩展开发指引

| 需求 | 改动点 | 不动的部分 |
| --- | --- | --- |
| 换真实 OMS | 实现 `OrderAPI` Protocol，改 `order_api =` 一行 | 工具/agent/前端 |
| 换向量检索 | 重写 `KnowledgeBase.search()` 同签名 | 其余全部 |
| 新增客户操作（如改地址） | 复制 propose/confirm 模式：提议工具 + pending 状态 + confirm 端点分支 | 架构不变 |
| 持久化会话 | MemorySaver → Redis/Postgres checkpointer；SessionStore 落库 | 接口契约不变 |
| 接真实工单系统 | `set_handoff` 处改为调用工单 API 建 ticket | 前端横幅结构不变 |
