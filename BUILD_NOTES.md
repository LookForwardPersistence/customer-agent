# Build Notes

## Time split (approx.)

**Initial build (inside the 2–4 hour time box):**

| Activity | Time |
| --- | --- |
| Scoping, architecture, data/knowledge-base design | ~30 min |
| LangGraph agent + tools + confirmation state machine | ~45 min |
| FastAPI backend + chat UI (sources, trace, confirm, handoff) | ~45 min |
| Evaluation harness + test cases + fixes | ~30 min |
| README / build notes / verification guide | ~15 min |
| **Initial build total** | **~2 h 45 min** |

**Follow-up hardening passes** (auth scoping, state-machine hardening, XSS
hardening, regression suite, live-agent eval, demo video — separate sessions
after the initial build, listed here for full transparency):

| Activity | Approx. time |
| --- | --- |
| Bearer-token auth + per-customer order scoping (`app/auth.py`) | ~45 min |
| Action-lifecycle hardening: addressable `action_id`, CAS on confirm, proposal fingerprint, idempotency, `UNKNOWN` + read-back recovery, confirm-timeout sweeper | ~45 min |
| Deterministic server-rendered execution replies + trusted `SystemMessage` channel (LLM never on the write path) | ~20 min |
| XSS hardening (CSP, externalized JS, `textContent` rendering) + DOM behavior harness | ~30 min |
| P0/P1/P2 pytest regression suite (currently **126 tests**) | ~30 min |
| Live agent-layer eval + RAG final-answer benchmark (45 cases) | ~60 min |
| Durable state: `StateBackend` protocol (memory/SQLite), SQLite checkpointer, restart-survival tests | ~45 min |
| Demo video + verification docs | ~30 min |

## Use of AI coding tools (and what I verified myself)

Per the assignment's technical guidelines, here is the split:

**What AI tools (CodeBuddy / Claude) did** — scaffolding and boilerplate: project
skeleton and dependency setup, the FastAPI route wiring, the chat UI markup and
CSS, and first drafts of documentation.

**What I specified myself** — every decision that carries engineering weight:

- the propose-confirm security model (LLM can propose; only the server/UI can
  execute) and its state-machine invariants (addressable `action_id`, atomic
  CAS, fingerprint + idempotency-key checks)
- the bearer-token auth model and the decision to enforce order ownership at
  the data layer, not in the prompt
- the knowledge-base content and the grounding rules in the system prompt
- the error-code contract on the mock backend (`OrderAPI` protocol)
- the evaluation cases and their expected behaviour, including the adversarial
  ones (prompt injection, customer claims that conflict with the KB,
  cross-customer confirmation attempts)

**What I verified myself** — I did not accept generated output on trust:

- ran the full deterministic suite and read the actual outputs, not just the
  pass/fail line; this is how the retrieval-threshold fragility and the
  `sources`-overwrite bug were found
- drove the live agent against DeepSeek (`deepseek-chat`) for every agent-layer
  case, debugging two real behavioural defects (the agent re-asking for an
  already-stated return reason; an eval assertion that was wrong, not the agent)
- ran a dedicated re-verification of the grounding requirement, which surfaced
  and fixed a gap: product-type questions previously skipped retrieval
  (TC-13/TC-14 were added in the same pass)
- adversarial spot-checks: asked the agent to skip confirmation and execute a
  return directly — refused, and a repo-wide `grep` confirms `create_return` is
  absent from the tool list exposed to the LLM; cross-customer confirmation
  attempts return HTTP 409
- verified no secrets reached the repo (`git ls-files` contains no `.env`, no
  `sk-` strings)

All fixes found this way are recorded in `REQUIREMENTS_VERIFICATION.md`.

## Dependency management

`uv.lock` is the source of truth (Python 3.12, pinned via `.python-version`;
`requires-python = ">=3.12,<3.14"`). `requirements.txt` is a pinned export of
it — including transitive dependencies at exact versions — so CI can install
with plain `pip` and still get identical versions:

```bash
uv lock                                                              # re-resolve after editing pyproject.toml
uv export --format requirements-txt --no-hashes --extra dev -o requirements.txt  # regenerate the export
uv lock --check                                                      # CI gate: lock matches pyproject
```

Both files are committed; changing a dependency requires re-running the export,
which is exactly the kind of drift the `lockfile` CI job is there to catch.

Verified locally: a clean virtualenv built from `requirements.txt` plus
`pip install -e . --no-deps` runs the full suite (126 passed). One environment
gotcha worth recording — on machines whose pip points at a PyPI *mirror*, the
export may fail with "No matching distribution found", because mirrors lag the
real index. That is a mirror problem, not a lockfile problem; `pip install -i
https://pypi.org/simple -r requirements.txt` confirms it.

## Intentionally NOT built (and why)

- **SQLite persistence is built, production-grade replication is not** — sessions,
  tokens, and conversation checkpoints are durable via a `StateBackend` protocol
  (stdlib sqlite3, WAL + `synchronous=FULL`), so a restart keeps proposals
  confirmable and orphaned `CONFIRMING` actions recover via the startup sweep.
  What is *not* built is multi-node plumbing: Redis/Postgres backends, token
  refresh/revocation endpoints, and a real OMS. The `StateBackend`/`OrderAPI`
  protocols and the `AuthenticatedCustomer` dependency are the seams for each.
- **No streaming responses** — request/response JSON keeps the trace and
  confirmation flow simple; streaming is a UX polish, not a reliability feature.
- **No embedding/vector retrieval** — a dependency-free character-bigram scorer
  is accurate enough for a 14-entry Chinese FAQ KB and runs offline; the tool
  contract (`search(query) -> entries`) is the swap point for a vector store.
  Trade-off: recall on heavy paraphrases will degrade as the KB grows.
- **No multi-agent orchestration, no RAG chunking pipeline, no elaborate design
  system** — the assignment explicitly values the smallest credible version.
- **No real human-agent queue / ticket system** — handoff produces a
  structured `HandoffPayload` (server-side facts: intent, order ids, sentiment,
  attempts, last error) plus a transcript reference, but it stops at the UI
  banner; wiring it to a real ticket system is a one-function change.
- **Deliverable note** — the walkthrough demo is provided as a separate file
  (`演示视频.mp4`); `VERIFICATION.md` remains as a ~10-minute reproducible,
  scenario-by-scenario path through every requirement, which is stronger
  evidence than a single video and stays in sync as the code changes.

## Key trade-off: safety vs. fluency on state-changing actions

The agent *cannot* execute the return itself — `create_return` is deliberately
not a tool. Confirmation must come from the UI button, which the server verifies
against the exact `action_id` shown to the user. Cost: a user typing "确认" gets
redirected to the button (slightly less fluid). Benefit: the write path is
immune to prompt injection, at-most-once by construction (CAS + idempotency
key = action_id), and "did the user really confirm?" has an unambiguous,
auditable answer in the session log.

A second, related decision: execution results are rendered by deterministic
server templates and re-injected into the agent over a trusted `SystemMessage`
channel (user-forged "[系统事件]" prefixes are stripped). The LLM is never on
the write path, so a model timeout or missing key cannot swallow a persisted
business result.

## Failure handling demonstrated

- Unknown order **or another customer's order** → `ORDER_NOT_FOUND`, surfaced
  honestly with the same response for both (anti-enumeration) (TC-09, TC-AUTH).
- Backend timeout on execution (`AT-10099`) → execution falls to `UNKNOWN`;
  the client recovers by a read-only `GET /api/session/action/{action_id}` —
  no blind retry (TC-11).
- Expired / superseded / already-consumed confirm or cancel → HTTP 409 with an
  explicit reason; the UI refreshes and prompts the user to re-initiate.
- Policy refusal (custom engraved item) → explains the policy and the
  quality-issue path instead of a hard "no" (TC-10).
- Repeated failure / out-of-scope insistence → `handoff_to_human` with a
  structured payload containing request, order ids, attempts, and last error
  (TC-12).

## Evaluation results

Latest live run: **19/19 passed** — full output reproduced by running
`evaluation/run_eval.py` (current code, agent layer on DeepSeek
`deepseek-chat`; timestamped run snapshots under `evaluation/` are transient and
not kept in the repo). Deterministic
retrieval/tool/state-machine/auth cases run without an LLM; the agent-layer
cases and the RAG benchmark require `OPENAI_API_KEY`.

| Layer | Cases | Result |
| --- | --- | --- |
| Retrieval | TC-01/02/03 (+ TC-13/TC-14 preconditions) | PASS — hits + sources, low/no-match must not invent |
| Tool / state machine | TC-05–TC-11 | PASS — query, propose, confirm, cancel, policy refusal, failures |
| Agent (live LLM) | TC-04, TC-06-agent, TC-12, TC-13, TC-14 | PASS — clarify vs. guess, propose end-to-end with zero pre-confirm writes, refuse→handoff with retained context, correct a KB-conflicting claim, search-first + honest unknown |
| Auth | TC-AUTH | PASS — cross-customer confirm rejected (409), other customer's order → `ORDER_NOT_FOUND` |
| RAG final-answer benchmark | 4 metrics over **45 queries** | PASS — groundedness, claim-level citation support, refusal accuracy (threshold ≥ 80%), plus a non-gating over-citation diagnostic |

Regression suite (`tests/`): **126 passed** in ~3 s (pytest; auth, action
lifecycle, idempotency/read-back, concurrency, XSS behavior via a DOM harness,
demo reset, handoff payload structure, RAG benchmark self-consistency,
persistence — backend consistency matrix plus restart survival).

### Two RAG metric defects found by expanding to 45 cases

The 15-case benchmark passed cleanly; at 45 cases it exposed two flaws in the
scorer itself, both of which were scoring formatting rather than grounding:

1. **Whitespace-sensitive fact matching.** The reply wrote「12个月质保」while the
   fact string was「12 个月」— counted as an ungrounded answer. Spacing is not a
   signal, so matching is now whitespace-insensitive, with per-case `alt_facts`
   for genuine paraphrases（「无法再修改」≡「无法修改」）.
2. **Citation precision was the wrong gate.** It failed any reply that cited an
   extra topically-related entry (e.g. quoting delivery times when asked about
   remote-area shipping fees). That is verbosity, not fabrication. The gate is
   now **claim-level support** — every asserted fact must be backed by at least
   one cited source — while over-citation is reported separately as a
   diagnostic. A claim with no source is a fabrication; a chatty citation is not.

Both properties are mechanically checkable, so `rag_cases.json` requires every
`expected_fact` to be a verbatim substring of a cited KB entry, and
`tests/test_p1_rag_cases.py` enforces that (plus full KB coverage and
`alt_facts` alignment) on every run — the benchmark cannot silently drift out of
sync with the knowledge base.

TC-13/TC-14 exist because of a dedicated re-verification pass over the grounding
requirement, which also found and fixed the skip-retrieval gap — the evaluation
suite is treated as a regression net for any prompt or tool change.

## Next three improvements

1. **Real retrieval upgrade** — swap bigram scoring for embeddings
   (bge-small-zh) behind the same `search()` contract, plus per-answer citation
   confidence thresholds tuned on a larger query set.
2. **Persistence + production identity** — Redis/Postgres checkpointer and
   persisted `SessionStore`/`TokenService`, JWT refresh/revocation or
   gateway-level session management, then a real OMS behind `OrderAPI`
   (handoff payload maps directly to a Wati ticket).
3. **Streaming + typed trace events** — SSE streaming for replies and a typed
   event stream (`source_hit` / `action_proposed` / `action_confirmed` /
   `handoff`), persisted so the UI trace panel becomes an auditable timeline and
   an offline LLM-as-judge quality monitor can run over real traffic.

## Assumptions made (documented per the assignment)

- Chinese-language store and conversations (realistic for the chosen business
  context; the UI, KB, and persona are Chinese, code and docs are English).
- Demo customers and orders are known and seeded in `orders.json`; identity
  comes from a bearer token issued by `POST /api/session/new` — switching the
  demo customer means requesting a new token (multi-customer isolation is
  exercised end-to-end, not just assumed).
- LLM via any OpenAI-compatible API, configured by env vars; no secrets in the
  repo.
- Service hours (9:00–21:00) are stated in the KB and reflected in handoff
  messaging outside hours.
