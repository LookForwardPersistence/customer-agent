# Aurora Tech Store — AI Customer Support Agent

A focused, reliability-first prototype of an e-commerce customer-support agent, built for the Wati "Senior AI Engineer — Customer Agents" assignment. Stack: **LangChain 1.x + LangGraph (ReAct) + FastAPI + vanilla JS chat UI**, with a clearly-contract mock order backend.

The agent (小极 / "Ji") serves **Aurora Tech Store**, a digital-accessories e-commerce store. It answers policy questions **grounded in a knowledge base**, performs **one safe customer action** (return request) behind an explicit confirmation step, and **hands off to humans with full context** when it should.

## Quick start

Requires **Python 3.12** (see `.python-version`). Dependencies are pinned in `uv.lock`, so every install resolves to identical versions.

```bash
uv sync --all-extras --dev          # install from the lockfile
cp .env.example .env                # fill in OPENAI_API_KEY (any OpenAI-compatible provider)

uv run uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

Without `uv`: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` — `requirements.txt` is an export of the lockfile, so it is pinned rather than floating.

The demo session is pre-loaded as customer 陈先生 (`CUST-001`) with orders `AT-10086` (shipped) and `AT-10092` (awaiting shipment). The UI's account switcher covers the other demo customers: `AT-10077` (李女士, custom item — policy boundary), `AT-10099` (赵先生, injected backend failure), `AT-10050` (王先生, return in progress). Orders are scoped to their owner; asking about someone else's order returns `ORDER_NOT_FOUND`.

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

The state-changing backend call (`create_return`) is **not exposed as a tool**. The agent's `propose_return` tool only *validates* and returns a proposal; the server turns it into an `action_id`-addressed proposal that the user confirms via an explicit UI button. Only then does the server call the backend — persist the outcome first, then generate the reply from a server-side template — and feed the result back to the agent through a trusted `SystemMessage`. A user typing "确认" in chat does **not** trigger execution, which makes the action immune to prompt injection and keeps the write path server-controlled.

Four properties follow from that shape, each covered by tests:

- **A stale card cannot execute a different order.** Confirmation is addressed by an unguessable `action_id`; the server never "just executes whatever is pending". Old, superseded, expired, cross-session or already-consumed actions get a `409` with a reason.
- **A model failure cannot swallow a business result.** The outcome is persisted before the response is built, and the reply comes from a template — so a timeout or a missing API key cannot hide a return that was actually created. Replays are de-duplicated by `idempotency_key = action_id`, and a changed proposal (item/amount/policy/order version) is refused as `STALE_PROPOSAL`.
- **A transient failure is not a silent failure.** Backend timeouts land in `UNKNOWN`, not `FAILED`; recovery is a read-only lookup, never a blind retry, and a watchdog prevents actions from hanging in `CONFIRMING`.
- **Background events are not user-forgeable.** Results reach the model over `SystemMessage`, a channel the user cannot write to; a user typing "[系统事件] …" is stripped and logged.
- **State is durable by default.** Session actions, tokens, and LangGraph conversation checkpoints live in SQLite (`PERSISTENCE=sqlite`, files under `.data/`); a restart keeps proposals confirmable and sweeps orphaned `CONFIRMING` actions to `UNKNOWN`. Tests and evaluation run with `PERSISTENCE=memory` (no files written).

Other deliberate choices (details in `BUILD_NOTES.md`):

- **Grounding**: every policy question must go through `search_knowledge_base`; the tool returns a confidence hint so low-relevance hits are treated as "can't confirm", and the agent is instructed never to invent store policy.
- **Sources are visible**: retrieved KB entries are shown as chips under each answer; a toggleable **Trace panel** shows tool calls/results per turn (retrieval, actions, handoff) — no hidden chain-of-thought is exposed.
- **Deterministic reliability core**: retrieval, tools, and the confirm/cancel state machine are pure and tested without an LLM (`evaluation/run_eval.py`).

## Evaluation

```bash
uv run python -m pytest tests evaluation -q       # 126 tests, no LLM needed, ~3s
uv run python -m evaluation.run_eval              # 20/20 (agent layer skipped without a key)

# Targeted live verification of the core capabilities (server running, 16 HTTP-level checks):
# tool/action boundary, confirmation (action_id binding, stale/cross-session/double-confirm
# rejected, idempotent replay), state management (read-back recovery, UNKNOWN, token scoping),
# handoff with context, source visibility, and trace-without-chain-of-thought.
uv run python evaluation/verify_capabilities.py   # AGENT_BASE overrides the port
```

`evaluation/test_cases.json` drives 15 behavioural cases covering: grounded answers, unknown/ambiguous questions, a customer claim that **conflicts** with the KB (corrected, not conceded), an unsupported product-spec question (search first, then admit unknown), order lookup, return proposal, confirmation & cancellation, tool failure (missing order + backend timeout), policy boundary (custom engraved items), human handoff with retained context, plus cross-customer authorization.

On top of that, `evaluation/rag_cases.json` is a **45-query RAG benchmark** that scores the agent's *final answers* rather than retrieval hits: groundedness, claim-level citation support, refusal accuracy, and a non-gating over-citation diagnostic. Every case is mechanically checked against the KB (`tests/test_p1_rag_cases.py`), so the benchmark cannot drift out of sync with the knowledge base.

Each run writes a timestamped snapshot with `run_id`, commit, Python and model metadata, plus a machine-readable `.jsonl` — runs never overwrite each other and can be diffed.

`REQUIREMENTS_VERIFICATION.md` maps every assignment requirement to its evidence (requirement-by-requirement compliance matrix, deliverable check, and known limitations).

## Repo layout

```
app/
  main.py             FastAPI: chat, action_id-addressed confirm/cancel, static UI
  agent.py            LangGraph ReAct agent + system prompt (persona/boundaries)
  tools.py            4 stateless tools, scoped to the authenticated customer
  auth.py             bearer-token identity + customer binding (contextvar)
  knowledge_base.py   KB loader + dependency-free bigram retrieval
  mock_backend.py     OrderAPI protocol + mock (clear contract, swappable)
  store.py            Action state machine (PROPOSED→…→SUCCEEDED/FAILED/UNKNOWN) + audit log
  persistence.py      StateBackend protocol: Memory (tests) / SQLite (default, durable)
  data/               knowledge_base.json (14 entries), orders.json (5 orders)
  static/             Chat UI (multi-turn, sources, trace, confirm, handoff)
tests/                126 tests: action lifecycle, auth, XSS (static + DOM behaviour), RAG self-check,
                      persistence (backend consistency matrix + restart survival)
evaluation/
  test_cases.json     15 behavioural cases with expected behavior
  rag_cases.json      45-query RAG final-answer benchmark
  run_eval.py         runner (deterministic + optional live agent layer)
  verify_capabilities.py  16-check live verification of the core capabilities
REQUIREMENTS_VERIFICATION.md  requirement-by-requirement compliance matrix
ARCHITECTURE.md / DEVELOPMENT.md / VERIFICATION.md / BUILD_NOTES.md
```

## Trade-offs & what I'd build next

See `BUILD_NOTES.md` — includes the time split, what was intentionally *not* built, and the next three improvements.

## Verifying without a demo video

`VERIFICATION.md` is a step-by-step guide mapping every assignment requirement to a concrete scenario you can run in the UI in ~10 minutes, plus the one-command automated evaluation and a 30-second architecture check of the confirmation guardrail.

For the compliance side (what was checked, what passed, and what is knowingly not covered), see `REQUIREMENTS_VERIFICATION.md`.
