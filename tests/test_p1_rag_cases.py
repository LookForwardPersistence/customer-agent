"""P1-5/P1-8：rag_cases.json 是 RAG 评估的单一事实源，必须与知识库保持自洽。

这些是静态校验（不需要 LLM），因此在 CI 里每次都跑：
- 用例数量足够（评审建议 30–50 条）；
- 每条 expected_fact 都是所引用 KB 条目的**原文子串**——否则
  run_eval 的引用精度判定（cited KB 是否真的支撑该事实）就失去意义；
- 知识库每一条都至少被一个用例覆盖（避免评估盲区）；
- id 唯一、kind 取值在白名单内。
"""

import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
KB = {e["id"]: e["content"] for e in json.loads((ROOT / "app/data/knowledge_base.json").read_text(encoding="utf-8"))["entries"]}
CASES = json.loads((ROOT / "evaluation/rag_cases.json").read_text(encoding="utf-8"))["cases"]

VALID_KINDS = {"paraphrase", "mixed", "conflict", "negative_answerable", "no_answer"}
MIN_CASES = 30
MAX_CASES = 50


def test_case_count_in_recommended_range():
    assert MIN_CASES <= len(CASES) <= MAX_CASES, f"{len(CASES)} cases (want {MIN_CASES}-{MAX_CASES})"


def test_ids_unique():
    dupes = [i for i, n in Counter(c["id"] for c in CASES).items() if n > 1]
    assert dupes == [], f"duplicate case ids: {dupes}"


def test_kinds_are_known():
    unknown = {c["id"]: c["kind"] for c in CASES if c["kind"] not in VALID_KINDS}
    assert unknown == {}, f"unknown kinds: {unknown}"


def test_every_kb_entry_is_covered():
    covered = {k for c in CASES for k in c.get("kb_ids", [])}
    assert set(KB) - covered == set(), f"KB entries never exercised: {sorted(set(KB) - covered)}"


def test_expected_facts_are_verbatim_substrings_of_cited_sources():
    """引用精度的前提：事实必须能在被引用的条目里原样找到。"""
    broken = [
        (c["id"], fact, c["kb_ids"])
        for c in CASES
        if not c.get("expect_refusal")
        for fact in c.get("expected_facts", [])
        if not any(fact in KB[k] for k in c["kb_ids"])
    ]
    assert broken == [], f"facts not present in any cited KB entry: {broken}"


def test_cited_kb_ids_exist():
    missing = [(c["id"], k) for c in CASES for k in c.get("kb_ids", []) if k not in KB]
    assert missing == [], f"cases cite unknown KB ids: {missing}"


def test_refusal_cases_carry_no_expected_facts():
    """拒答用例若同时声明 expected_facts，评分逻辑会自相矛盾。"""
    bad = [c["id"] for c in CASES if c.get("expect_refusal") and c.get("expected_facts")]
    assert bad == [], f"refusal cases must not assert facts: {bad}"


def test_alt_facts_align_with_expected_facts():
    """alt_facts[i] 是 expected_facts[i] 的等价表述，必须逐位对应，否则语义错位。"""
    bad = [
        (c["id"], len(c["alt_facts"]), len(c["expected_facts"]))
        for c in CASES
        if c.get("alt_facts") and len(c["alt_facts"]) != len(c.get("expected_facts", []))
    ]
    assert bad == [], f"alt_facts length mismatch (id, alt, facts): {bad}"


def test_alt_facts_are_not_empty():
    bad = [
        (c["id"], i)
        for c in CASES
        for i, alts in enumerate(c.get("alt_facts", []))
        if not alts or any(not a.strip() for a in alts)
    ]
    assert bad == [], f"empty alt_facts entries: {bad}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_case_shape(case):
    assert case["id"].startswith("RAG-")
    assert case["query"].strip()
    if not case.get("expect_refusal"):
        assert case.get("expected_facts"), f"{case['id']} needs expected_facts"
        assert case.get("kb_ids"), f"{case['id']} needs kb_ids"
