# Aurora Tech Store — AI Customer Support Agent

A focused, reliability-first prototype of an e-commerce customer-support agent, built for the Wati "Senior AI Engineer — Customer Agents" assignment. Stack: **LangChain 1.x + LangGraph (ReAct) + FastAPI + vanilla JS chat UI**, with a clearly-contract mock order backend.

The agent (小极 / "Ji") serves **Aurora Tech Store**, a digital-accessories e-commerce store. It answers policy questions **grounded in a knowledge base**, performs **one safe customer action** (return request) behind an explicit confirmation step, and **hands off to humans with full context** when it should.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # fill in OPENAI_API_KEY (any OpenAI-compatible provider)

uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

The demo session is pre-loaded as customer 陈先生 with orders `AT-10086` (shipped) and `AT-10092` (awaiting shipment). `AT-10077` demos the custom-item return policy, `AT-10099` demos a backend failure on execution.

## Architecture

See `ARCHITECTURE.md` for the full design document (layered diagram, confirmation state machine, request sequence, design decisions, and production roadmap), and `DEVELOPMENT.md` for the development design (module-level specs, API contracts, data schemas, frontend components, evaluation design, and a debugging guide). Summary:

```
Browser (chat UI)
   │  POST /api/chat            POST /api/session/confirm|cancel
   ▼
FastAPI (app/main.py)  ── session store: pending actions / handoff / audit log
   │
   ▼
LangGraph ReAct agent (app/agent.py)  ── persona & boundaries in system prompt
   │  tools (stateless, app/tools.py)
   ├─ search_knowledge_base   (grounded answers, app/knowledge_base.py)
   ├─ get_order_status        (read-only)
   ├─ propose_return          (validates, returns NEEDS_CONFIRMATION — never writes)
   └─ handoff_to_human        (records reason + context summary)
   ▼
Mock backend (app/mock_backend.py)  ── OrderAPI protocol, swappable for a real OMS
```

### Key design decision: the LLM can propose, only the UI can execute

The state-changing backend call (`create_return`) is **not exposed as a tool**. The agent's `propose_return` tool only *validates* and returns a proposal; the server turns it into a `pending_action` that the user confirms via an explicit UI button. Only then does the server call the backend and feed the result back to the agent as a `[系统事件]` message. A user typing "确认" in chat does **not** trigger execution — this makes the action immune to prompt injection and keeps the write path server-controlled.

Other deliberate choices (details in `BUILD_NOTES.md`):

- **Grounding**: every policy question must go through `search_knowledge_base`; the tool returns a confidence hint so low-relevance hits are treated as "can't confirm", and the agent is instructed never to invent store policy.
- **Sources are visible**: retrieved KB entries are shown as chips under each answer; a toggleable **Trace panel** shows tool calls/results per turn (retrieval, actions, handoff) — no hidden chain-of-thought is exposed.
- **Deterministic reliability core**: retrieval, tools, and the confirm/cancel state machine are pure and tested without an LLM (`evaluation/run_eval.py`).

## Evaluation

```bash
python -m evaluation.run_eval                       # deterministic layer, no LLM needed
OPENAI_API_KEY=... python -m evaluation.run_eval    # + live agent tests (TC-04, TC-06-agent, TC-12)

# Targeted live verification of the core capabilities (server running, 16 checks):
# tool/action boundary, confirmation (typed "确认" never executes, double-confirm rejected),
# state management (cross-turn references, session isolation), handoff with context,
# source visibility across multi-retrieval turns, and trace-without-chain-of-thought.
python evaluation/verify_capabilities.py
```

15 cases in `evaluation/test_cases.json` cover: grounded answers, unknown/ambiguous questions, a customer claim that **conflicts** with the KB (corrected, not conceded), an unsupported product-spec question (search first, then admit unknown), order lookup, return proposal, confirmation & cancellation, tool failure (missing order + backend timeout), policy boundary (custom engraved items), and human handoff with retained context. Results: `evaluation/results.md` (15/15).

`REQUIREMENTS_VERIFICATION.md` maps every assignment requirement to its evidence (requirement-by-requirement compliance matrix, deliverable check, and known limitations).

## Repo layout

```
app/
  main.py             FastAPI: chat, confirm/cancel endpoints, static UI
  agent.py            LangGraph ReAct agent + system prompt (persona/boundaries)
  tools.py            4 stateless tools
  knowledge_base.py   KB loader + dependency-free bigram retrieval
  mock_backend.py     OrderAPI protocol + mock (clear contract, swappable)
  store.py            Session store: pending actions, handoffs, audit log
  data/               knowledge_base.json (14 entries), orders.json (5 orders)
  static/index.html   Chat UI (multi-turn, sources, trace, confirm, handoff)
evaluation/
  test_cases.json     15 cases with expected behavior
  run_eval.py         runner (deterministic + optional live agent layer)
  results.md          results table
  verify_capabilities.py  16-check live verification of the core capabilities
REQUIREMENTS_VERIFICATION.md  requirement-by-requirement compliance matrix
```

## Trade-offs & what I'd build next

See `BUILD_NOTES.md` — includes the time split, what was intentionally *not* built, and the next three improvements.

## Verifying without a demo video

`VERIFICATION.md` is a step-by-step guide mapping every assignment requirement to a concrete scenario you can run in the UI in ~10 minutes, plus the one-command automated evaluation and a 30-second architecture check of the confirmation guardrail.

For the compliance side (what was checked, what passed, and what is knowingly not covered), see `REQUIREMENTS_VERIFICATION.md`.
