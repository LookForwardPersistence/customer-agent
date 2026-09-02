# Build Notes

## Time split (approx.)

| Activity | Time |
| --- | --- |
| Scoping, architecture, data/knowledge-base design | ~30 min |
| LangGraph agent + tools + confirmation state machine | ~45 min |
| FastAPI backend + chat UI (sources, trace, confirm, handoff) | ~45 min |
| Evaluation harness + 15 test cases + fixes | ~30 min |
| README / build notes / verification guide | ~15 min |

## Use of AI coding tools (and what I verified myself)

Per the assignment's technical guidelines, here is the split:

**What AI tools (CodeBuddy / Claude) did** — scaffolding and boilerplate: project
skeleton and dependency setup, the FastAPI route wiring, the chat UI markup and
CSS, and first drafts of documentation.

**What I specified myself** — every decision that carries engineering weight:

- the propose-confirm security model (LLM can propose; only the UI can execute)
- the knowledge-base content and the grounding rules in the system prompt
- the error-code contract on the mock backend (`OrderAPI` protocol)
- the evaluation cases and their expected behaviour, including the adversarial
  ones (prompt injection, customer claims that conflict with the KB)

**What I verified myself** — I did not accept generated output on trust:

- ran the full deterministic suite (11 cases) and read the actual outputs, not
  just the pass/fail line; this is how the retrieval-threshold fragility and the
  `sources` overwrite bug were found
- drove the live agent against DeepSeek for every agent-layer case, debugging
  two real behavioural defects (the agent re-asking for an already-stated return
  reason; an eval assertion that was wrong, not the agent)
- adversarial spot-check: asked the agent to skip confirmation and execute a
  return directly — refused, and a repo-wide `grep` confirms `create_return` is
  absent from the tool list exposed to the LLM
- verified no secrets reached the repo (`git ls-files` contains no `.env`, no
  `sk-` strings)

Two fixes came out of these checks rather than from trusting the first draft,
both recorded in `REQUIREMENTS_VERIFICATION.md`.

## Intentionally NOT built (and why)

- **No production backend / auth / persistence** — in-memory session store and JSON mock data; the `OrderAPI` protocol is the seam for a real OMS.
- **No streaming responses** — request/response JSON keeps the trace and confirmation flow simple; streaming is a UX polish, not a reliability feature.
- **No embedding/vector retrieval** — a dependency-free character-bigram scorer is accurate enough for a 14-entry Chinese FAQ KB and runs offline; the tool contract (`search(query) -> entries`) is the swap point for a vector store. Trade-off: recall on heavy paraphrases will degrade as the KB grows.
- **No multi-agent orchestration, no RAG chunking pipeline, no elaborate design system** — the assignment explicitly values the smallest credible version.
- **Single customer identity (no login)** — the demo assumes one known customer; order-scoping per authenticated user would be the first productionization step.
- **No demo video** — replaced by `VERIFICATION.md`, a scenario-by-scenario verification guide (every requirement mapped to concrete UI steps with expected behavior, plus the automated evaluation). All claims in it are reproducible in ~10 minutes, which arguably gives *stronger* evidence than a one-shot video.

## Key trade-off: safety vs. fluency on state-changing actions

The agent *cannot* execute the return itself — `create_return` is deliberately not a tool. Confirmation must come from the UI button, which the server verifies. Cost: a user typing "确认" gets redirected to the button (slightly less fluid). Benefit: the write path is immune to prompt injection, and "did the user really confirm?" has an unambiguous, auditable answer in the session log.

## Failure handling demonstrated

- Unknown order → `ORDER_NOT_FOUND` surfaced honestly, agent asks to re-check (TC-09).
- Backend timeout on execution (`AT-10099`) → agent apologizes, explains, offers retry/human (TC-11).
- Policy refusal (custom engraved item) → explains the policy and the quality-issue path instead of a hard "no" (TC-10).
- Repeated failure / out-of-scope insistence → `handoff_to_human` with a summary containing request, details, and what was already tried (TC-12).

## Evaluation results

`evaluation/results.md` — **15/15 passed**, including the live agent layer (DeepSeek `deepseek-chat`):
TC-04 asks a clarifying question instead of guessing an order, TC-06-agent proposes the return end-to-end without executing, TC-12 refuses then hands off with a retained context summary, TC-13 corrects a customer claim that conflicts with the KB (lifetime warranty vs 12-month policy), and TC-14 searches first on an unsupported product-spec question and admits it cannot confirm. TC-13/TC-14 were added after a dedicated re-verification of the grounding requirement, which also surfaced and fixed a gap: product-type questions previously skipped retrieval.

## Next three improvements

1. **Real retrieval upgrade**: swap bigram scoring for embeddings (bge-small-zh) behind the same `search()` contract, plus per-answer citation confidence thresholds tuned on a larger query set.
2. **Session persistence + auth**: Redis-backed sessions keyed to an authenticated customer, order-scope enforcement (the agent should only see *your* orders — today the mock trusts the order id).
3. **Streaming + typed trace events**: SSE streaming for replies and a typed event stream (source_hit / action_proposed / action_confirmed / handoff) so the UI trace panel becomes an auditable timeline without extra polling.

## Assumptions made (documented per the assignment)

- Chinese-language store and conversations (realistic for the chosen business context; the UI, KB, and persona are Chinese, code and docs are English).
- One demo customer with known orders; no login flow.
- LLM via any OpenAI-compatible API, configured by env vars; no secrets in the repo.
- Service hours (9:00–21:00) are stated in the KB and reflected in handoff messaging outside hours.
