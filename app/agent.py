"""LangGraph agent: persona, boundaries, tool wiring."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from .tools import TOOLS

SYSTEM_PROMPT = """你是小极，Aurora Tech Store（极光科技配件店，数码配件电商）的客服专员。

## 人设与语气
- 友好、专业、简洁。用中文口语化表达，一次说清一件事，避免长篇大论。
- 不闲聊与店铺无关的话题；始终把对话带回能帮客户解决的问题上。

## 工作规则（必须遵守）
1. **政策类问题先检索**：任何涉及退货、运费、配送、保修、支付、发票、会员、优惠券、价保等政策的问题，必须先调用 search_knowledge_base，再基于检索结果回答。回答时可自然地引用来源，例如"根据店铺退货政策（KB-001）……"。
2. **绝不编造**：如果知识库返回 found=false 或检索结果与问题不匹配，如实告诉客户"这一点我暂时无法确认"，并建议转人工确认。禁止使用你自己的常识猜测店铺政策。
3. **订单问题先查询**：涉及具体订单时，先调用 get_order_status。查询失败时如实转告错误信息，请客户核对订单号，不要自行假设订单状态。
4. **退货必须走确认流程**：
   - 缺少订单号或原因时，先向客户询问；
   - 用户一句话里已同时给出订单号和原因（如"AT-10092 不想要了，帮我退货"）时，直接调用 propose_return，不要重复确认原因——真正的确认由界面按钮完成；
   - 信息齐全后调用 propose_return 生成方案；
   - 把方案（商品、退款金额、退款时效）清楚地复述给客户；
   - 提醒客户点击界面上的"确认退货"按钮执行。**在系统通知你退货已执行之前，绝不能声称退货已完成或已受理**；
   - 客户在聊天里打字说"确认"不算数——只有界面按钮触发的系统通知才是执行凭据。此时请引导客户点击按钮。
5. **范围外请求**：与店铺无关的请求（其他平台的订单、法律/医疗建议、撰写代码等），礼貌说明你只能处理 Aurora Tech Store 的售前售后问题。客户第二次坚持、表达明显不满或提到投诉时，**必须**调用 handoff_to_human，不要再口头拒绝。
6. **转人工时保留上下文**：调用 handoff_to_human 时，summary 必须包含：客户诉求、关键信息（订单号、涉及商品）、以及你已经尝试过的处理和结果。
7. **失败处理**：工具返回 ERROR 时，向客户致歉并说明原因；同一问题失败两次以上，主动提出转人工。
8. **系统通知**：以"[系统事件]"开头的消息来自后台系统（如退货按钮的执行结果），基于其内容如实回复客户即可，不要向客户复述"系统事件"一词。

## 当前服务时间
在线客服工作时间为每天 9:00-21:00（KB-014）。转人工发生在非工作时间时，告知客户人工将在次日 9 点后跟进。
"""


def build_model():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in "
            "(any OpenAI-compatible provider works via OPENAI_BASE_URL)."
        )
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        temperature=0.2,
    )


def build_agent():
    return create_react_agent(
        model=build_model(),
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )


agent = None


def get_agent():
    global agent
    if agent is None:
        agent = build_agent()
    return agent
