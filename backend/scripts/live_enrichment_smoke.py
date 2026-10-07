"""STAGE 6A live validation: does a REAL LLM write usable lecture content inside the fixed structure?

This is NOT part of pytest (pytest never calls an LLM). It is a manual, cost-aware check.

    python scripts/live_enrichment_smoke.py <lecture file> --smoke                  # 1 slide, 1 call
    python scripts/live_enrichment_smoke.py <lecture file> --slides 8               # 8 representative slides x A/B/C
    python scripts/live_enrichment_smoke.py <lecture file> --slides 8 --source-policy source_only,source_first
    python scripts/live_enrichment_smoke.py <lecture file> --dry-run                # show prompts, no call
    python scripts/live_enrichment_smoke.py <lecture file> --fallback-check         # provoke real API failures

The SlideSpecification is built ONCE (from --base) and enriched under every profile, so the
structure is identical and only the wording may differ. Outputs (default `data/live_reports/`):
`live_result.json` (everything, incl. the raw LLM answers) and `live_report.md` (for a human).

Credentials: LLM_API_KEY (or OPENAI_API_KEY) from the environment / backend/.env. The key is never
printed, logged or written to a report. Without a key nothing is called and nothing is faked:
"Live test not executed: API key not configured".
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402,F401  (loads backend/.env)
from app.llm.base import ContentLLMClient, SlideResult  # noqa: E402
from app.llm.factory import build_openai_content_client  # noqa: E402
from app.llm.prompts import PROMPT_VERSION, build_batch_system_prompt, build_batch_user_message  # noqa: E402
from app.models.lecture_profile import LectureOptionsInput  # noqa: E402
from app.services.document_parser import DocumentParser  # noqa: E402
from app.services.enrichment_cache import InMemoryCache, JsonFileCache  # noqa: E402
from app.services.grounding_validator import DESTRUCTIVE_RE, ENV_RE  # noqa: E402
from app.services.lecture_analyzer import HeuristicAnalyzer  # noqa: E402
from app.services.lecture_planner import LecturePlanner  # noqa: E402
from app.services.lecture_profile_service import build_profile  # noqa: E402
from app.services.llm_client import LLMError  # noqa: E402
from app.services.slide_content_enricher import SlideContentEnricher  # noqa: E402
from app.services.slide_planner import SlidePlanner  # noqa: E402
from app.services.source_context import CHARS_PER_SECOND, build_requests  # noqa: E402
from app.services.text_utils import (  # noqa: E402
    fact_numbers, known_latin, latin_tokens, split_sentences, violates_scope,
)

PROFILES = {
    "beginner-theory": dict(audience_level="university_beginner", difficulty="beginner",
                            lecture_type="theory", explanation_depth="detailed"),
    "intermediate-practice": dict(audience_level="university_intermediate", difficulty="intermediate",
                                  lecture_type="practice", explanation_depth="standard"),
    "professional-advanced": dict(audience_level="professional", difficulty="advanced",
                                  lecture_type="theory", explanation_depth="concise"),
}
LABEL = {"beginner-theory": "A 대학 초급 / 이론", "intermediate-practice": "B 대학 중급 / 실습",
         "professional-advanced": "C 전문가 / 고급"}
TYPE_ORDER = ["definition", "concept", "comparison", "architecture", "workflow", "practice", "summary",
              "quiz", "example", "code", "diagram"]
DEFAULT_MODEL = "gpt-4o-mini"
SKIPPED = "not_requested"

WARNING_CATEGORIES = [  # (category, substrings of the validator's warning text)
    ("policy", ("source_only", "원문에 없는 용어", "원문에 없는 명령", "다시 쓴 내용", "LLM이 만든 예시", "원문에 없는 예시", "원문에 없는 비유")),
    ("scope", ("다루지 않는다고",)),
    ("safety", ("위험", "실행 환경", "버전", "시스템 설정")),
    ("contradiction", ("반대",)),
    ("provenance", ("낮췄", "출처 표시가 없어", "suggested")),
    ("size", ("줄였", "짧습니다", "길어")),
    ("visual", ("visual_instruction:",)),
    ("field", ("필요하지 않아", "speaker_notes")),
]


# --------------------------------------------------------------------------- client wrapper
class LiveRecorder(ContentLLMClient):
    """Wraps the real client: records latency / raw answers, and only forwards the selected slides."""

    def __init__(self, inner: ContentLLMClient):
        self.inner = inner
        self.provider, self.model = inner.provider, inner.model
        self.calls: list[dict] = []
        self.raw: dict[int, dict] = {}
        self.error_types: Counter = Counter()

    @property
    def usage(self) -> dict:
        return getattr(self.inner, "usage", {}) or {}

    def enrich_slide(self, request):  # not used: the enricher calls enrich_batch
        return self.inner.enrich_slide(request)

    def enrich_batch(self, requests):
        numbers = [r.slide.slide_number for r in requests]
        t0 = time.perf_counter()
        error = None
        try:
            results = self.inner.enrich_batch(requests)
        except LLMError as exc:
            error = type(exc).__name__
            results = [SlideResult(n, error=exc) for n in numbers]
        seconds = time.perf_counter() - t0
        for r in results:
            if r.output is not None and isinstance(r.output, dict):
                self.raw[r.slide_number] = copy.deepcopy(r.output)
            if r.error is not None:
                self.error_types[type(r.error).__name__] += 1
        self.calls.append({"slides": numbers, "seconds": round(seconds, 2), "error": error})
        return results


class SubsetEnricher(SlideContentEnricher):
    """Script-only: ask the LLM for the selected slides only (the others are reported as not requested)."""

    selected: set[int] = set()

    def _call_llm(self, pending, by_number, validator, profile, outcomes, run):
        keep = [r for r in pending if r.slide.slide_number in self.selected]
        for r in pending:
            if r.slide.slide_number not in self.selected:
                outcomes[r.slide.slide_number] = self._failed(by_number[r.slide.slide_number], profile, SKIPPED)
        super()._call_llm(keep, by_number, validator, profile, outcomes, run)


# --------------------------------------------------------------------------- helpers
def resolve_key() -> str | None:
    return os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or None


def build_structure(path: Path, base_name: str, policy: str):
    material = DocumentParser().parse(path, path.name, source_id=uuid.uuid4().hex)
    analysis = HeuristicAnalyzer().analyze(material)
    opts = LectureOptionsInput(**PROFILES[base_name], duration_minutes=60, source_policy=policy)
    profile = build_profile("p" * 32, opts)
    plan = LecturePlanner().plan(analysis, profile)
    spec = SlidePlanner().plan(plan, analysis)
    return material, analysis, plan, spec


def make_profile(name: str, policy: str, overrides: dict):
    opts = LectureOptionsInput(**PROFILES[name], duration_minutes=60, source_policy=policy, **overrides)
    return build_profile("p" * 32, opts)


def select_slides(spec, n: int, concept: str, explicit: list[int] | None) -> list[int]:
    if explicit:
        return sorted(x for x in explicit if 1 <= x <= spec.slide_count)
    by_type: dict[str, list] = {}
    for s in spec.slides:
        by_type.setdefault(s.slide_type.value, []).append(s)

    def rank(s):
        return (0 if concept.lower() in [c.lower() for c in s.concepts] else 1, s.slide_number)

    chosen: list[int] = []
    for t in TYPE_ORDER:
        if t in by_type and len(chosen) < n:
            chosen.append(sorted(by_type[t], key=rank)[0].slide_number)
    rest = [s for s in spec.slides if s.slide_number not in chosen and s.slide_type.value not in ("title",)]
    for s in sorted(rest, key=rank):
        if len(chosen) >= n:
            break
        chosen.append(s.slide_number)
    return sorted(chosen)


def categorize(warnings: list[str]) -> Counter:
    c: Counter = Counter()
    for w in warnings:
        for cat, keys in WARNING_CATEGORIES:
            if any(k in w for k in keys):
                c[cat] += 1
                break
        else:
            c["other"] += 1
    return c


def texts_of(e) -> list[tuple[str, str]]:
    """(field, sentence) for every sentence a listener could hear or read (visual instruction excluded)."""
    out: list[tuple[str, str]] = []

    def add(f, v):
        for s in split_sentences(v or ""):
            out.append((f, s))

    for f in ("key_message", "explanation", "example", "analogy", "presenter_notes", "summary_message"):
        add(f, getattr(e, f))
    for p in e.body_points:
        add("body_points", p)
    if e.practice_instruction:
        p = e.practice_instruction
        for v in [p.goal, p.expected_result, *p.prerequisites, *p.steps, *p.cautions]:
            add("practice_instruction", v)
    if e.code_explanation:
        c = e.code_explanation
        for v in [c.purpose, *c.key_lines, *c.execution_flow, *c.cautions]:
            add("code_explanation", v)
    if e.quiz_content:
        q = e.quiz_content
        for v in [q.question, q.answer, q.explanation, *q.choices]:
            add("quiz_content", v)
    return out


def audit(es, source_text: str, policy: str, scope_notes: list[str], vocab: set[str], numbers: set[str]) -> list[str]:
    """Independent check of what is LEFT after the GroundingValidator (a miss = validator weakness)."""
    issues: list[str] = []
    if es.enriched is None:
        return issues
    planner_text = " ".join([es.title, es.original.key_message, *es.original.key_points])
    local = latin_tokens(planner_text)
    numbers = numbers | set(fact_numbers(planner_text))  # figures the rule engine itself put on the slide
    for f, s in texts_of(es.enriched):
        if violates_scope(s, scope_notes):
            issues.append(f"scope: [{f}] {s[:90]}")
        if DESTRUCTIVE_RE.search(s):
            issues.append(f"destructive: [{f}] {s[:90]}")
        for m in ENV_RE.finditer(s):
            if m.group(0).lower() not in source_text.lower():
                issues.append(f"environment guess '{m.group(0)}': [{f}] {s[:90]}")
        if policy == "source_only":
            new = sorted(t for t in latin_tokens(s) if not known_latin(t, vocab | local))
            nums = sorted(n for n in fact_numbers(s) if n not in numbers)
            if new or nums:
                issues.append(f"source_only new term/number {new + nums}: [{f}] {s[:90]}")
    return issues


def run_one(client, spec, plan, analysis, material, profile, policy, selected, cache, force, batch_size, concurrency):
    rec = LiveRecorder(client)
    before = dict(rec.usage)
    enr = SubsetEnricher(rec, batch_size=batch_size, workers=concurrency, cache=cache)
    enr.selected = set(selected)
    t0 = time.perf_counter()
    result = enr.enrich(spec, plan, profile, analysis, source_text=material.raw_text, force=force)
    wall = time.perf_counter() - t0
    after = dict(rec.usage)
    usage = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    return result, rec, wall, usage


def metrics_of(result, rec, wall, usage, selected, policy, material, plan, analysis, price):
    slides = [s for s in result.slides if s.slide_number in selected]
    ok = [s for s in slides if s.enriched is not None]
    failed = [s for s in slides if s.enriched is None]
    warnings = [w for s in ok for w in s.grounding.warnings]
    from app.services.source_context import scope_notes_of

    scope = scope_notes_of(plan, analysis)
    vocab = latin_tokens(material.raw_text)
    numbers = fact_numbers(material.raw_text)
    leaks = {s.slide_number: audit(s, material.raw_text, policy, scope, vocab, numbers) for s in ok}
    notes = [(s, len(s.enriched.presenter_notes or "")) for s in ok if s.enriched.presenter_notes]
    ratios = [n / (s.estimated_explanation_time * CHARS_PER_SECOND * (0.4 if s.slide_type.value == "practice" else 1.0))
              for s, n in notes]
    calls = rec.calls
    slides_called = sum(len(c["slides"]) for c in calls)
    total_call_s = sum(c["seconds"] for c in calls)
    m = {
        "slides_requested": len(slides),
        "structure_preserved": result.validation.passed and result.slide_count == len(result.slides),
        "enriched": len(ok),
        "failed": len(failed),
        "failures": [{"slide": s.slide_number, "reason": s.failure_reason} for s in failed],
        "successful_enrichment_rate": round(len(ok) / len(slides), 3) if slides else 0,
        "fallback_rate": round(len(failed) / len(slides), 3) if slides else 0,
        "grounding_warning_count": len(warnings),
        "warnings_by_category": dict(categorize(warnings)),
        "source_policy_violations_removed_by_validator": categorize(warnings).get("policy", 0),
        "leaks_after_validation": {k: v for k, v in leaks.items() if v},
        "leak_count": sum(len(v) for v in leaks.values()),
        "avg_presenter_note_chars": round(sum(n for _, n in notes) / len(notes), 1) if notes else 0,
        "notes_length_vs_time": {
            "target_chars_per_second": CHARS_PER_SECOND,
            "avg_ratio": round(sum(ratios) / len(ratios), 2) if ratios else None,
            "min_ratio": round(min(ratios), 2) if ratios else None,
            "max_ratio": round(max(ratios), 2) if ratios else None,
        },
        "avg_body_points": round(sum(len(s.enriched.body_points) for s in ok) / len(ok), 2) if ok else 0,
        "analogy_count": sum(1 for s in ok if s.enriched.analogy),
        "example_count": sum(1 for s in ok if s.enriched.example),
        "practice_step_count": sum(len(s.enriched.practice_instruction.steps) for s in ok if s.enriched.practice_instruction),
        "quiz_count": sum(1 for s in ok if s.enriched.quiz_content),
        "cache_hits": sum(1 for s in slides if s.from_cache),
        "cache_hit_rate": round(sum(1 for s in slides if s.from_cache) / len(slides), 3) if slides else 0,
        "llm_calls": len(calls),
        "slides_sent_to_llm": slides_called,
        "avg_latency_per_slide_s": round(total_call_s / slides_called, 2) if slides_called else None,
        "total_latency_s": round(total_call_s, 2),
        "wall_clock_s": round(wall, 2),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "retries": usage.get("retries", 0),
        "call_errors": dict(rec.error_types),
    }
    if price and usage.get("input_tokens") is not None:
        m["estimated_cost_usd"] = round(
            usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1], 5)
    return m


def slide_record(es, raw, spec_slide):
    e = es.enriched
    return {
        "slide_number": es.slide_number, "slide_type": es.slide_type.value, "title": es.title,
        "seconds": es.estimated_explanation_time, "status": es.status.value, "failure_reason": es.failure_reason,
        "original": es.original.model_dump(mode="json"),
        "enriched": e.model_dump(mode="json") if e else None,
        "provenance": {k: v.value for k, v in es.provenance.items()},
        "grounded": es.grounding.grounded, "warnings": es.grounding.warnings, "from_cache": es.from_cache,
        "raw_llm_output": raw,
    }


# --------------------------------------------------------------------------- report
def md_slide(rec: dict) -> list[str]:
    e = rec["enriched"]
    L = [f"### Slide {rec['slide_number']} — {rec['title']}  (`{rec['slide_type']}`, {rec['seconds']}s, {rec['status']})", ""]
    o = rec["original"]
    L += ["**Original (rule engine):**", f"- key_message: {o['key_message']}"]
    L += [f"- key_point: {p}" for p in o["key_points"]]
    L.append("")
    if e is None:
        L += [f"**Fallback:** `{rec['failure_reason']}` — the original content is used.", ""]
        return L
    L += ["**Enriched:**", f"- display_title: {e['display_title']}", f"- key_message: {e['key_message']}"]
    L += [f"- body_point: {p}" for p in e["body_points"]]
    for f in ("explanation", "example", "analogy", "summary_message"):
        if e.get(f):
            L.append(f"- {f}: {e[f]}")
    if e.get("practice_instruction"):
        p = e["practice_instruction"]
        L += [f"- practice goal: {p['goal']}"] + [f"  {i}. {s}" for i, s in enumerate(p["steps"], 1)]
        L += [f"- practice expected: {p['expected_result']}"]
        L += [f"- practice caution: {c}" for c in p["cautions"]]
    if e.get("code_explanation"):
        L.append(f"- code_explanation: {json.dumps(e['code_explanation'], ensure_ascii=False)}")
    if e.get("quiz_content"):
        L.append(f"- quiz: {json.dumps(e['quiz_content'], ensure_ascii=False)}")
    L += ["", "**Presenter Notes:**", "", f"> {e['presenter_notes'] or '(none)'}", "",
          "**Visual Instruction:**", "", f"> {e['visual_instruction'] or '(none)'}", ""]
    L.append(f"**Grounding:** grounded={rec['grounded']}, provenance={rec['provenance']}")
    L.append("")
    L.append("**Warnings:** " + ("; ".join(rec["warnings"]) if rec["warnings"] else "none"))
    L.append("")
    return L


def write_report(path: Path, env: dict, runs: list[dict], fallback: dict | None) -> None:
    L = ["# Live LLM Enrichment Report", "", "## Environment", ""]
    for k, v in env.items():
        L.append(f"- {k}: {v}")
    L.append("")
    for run in runs:
        L += [f"## Profile {LABEL[run['profile']]} — {run['policy']}", "",
              "Metrics: `" + json.dumps({k: v for k, v in run["metrics"].items() if k not in ("leaks_after_validation",)},
                                        ensure_ascii=False) + "`", ""]
        if run["metrics"]["leaks_after_validation"]:
            L.append("**Left after validation (audit):** " + json.dumps(run["metrics"]["leaks_after_validation"], ensure_ascii=False))
            L.append("")
        for s in run["slides"]:
            L += md_slide(s)
    # ---- comparison
    L += ["## Comparison (same slide, same structure)", ""]
    for policy in dict.fromkeys(r["policy"] for r in runs):
        group = [r for r in runs if r["policy"] == policy]
        if len(group) < 2:
            continue
        L += [f"### source_policy={policy}", "", "| metric | " + " | ".join(LABEL[r['profile']] for r in group) + " |",
              "|---|" + "---|" * len(group)]
        for key in ("enriched", "fallback_rate", "avg_presenter_note_chars", "avg_body_points", "analogy_count",
                    "example_count", "practice_step_count", "quiz_count", "grounding_warning_count", "leak_count",
                    "avg_latency_per_slide_s", "input_tokens", "output_tokens"):
            L.append(f"| {key} | " + " | ".join(str(r["metrics"].get(key)) for r in group) + " |")
        L.append("")
        numbers = sorted({s["slide_number"] for r in group for s in r["slides"]})
        for n in numbers:
            L += [f"#### Slide {n}", ""]
            for r in group:
                s = next((x for x in r["slides"] if x["slide_number"] == n), None)
                e = s and s["enriched"]
                if not e:
                    L.append(f"- **{LABEL[r['profile']]}**: (fallback)")
                    continue
                L += [f"- **{LABEL[r['profile']]}**", f"  - title: {e['display_title']}",
                      f"  - key_message: {e['key_message']}",
                      f"  - explanation: {e.get('explanation')}",
                      f"  - analogy: {e.get('analogy')}", f"  - example: {e.get('example')}",
                      f"  - notes ({len(e.get('presenter_notes') or '')} chars): {(e.get('presenter_notes') or '')[:400]}",
                      f"  - visual: {e.get('visual_instruction')}"]
            L.append("")
    # ---- policy / grounding
    L += ["## Policy violations", ""]
    any_leak = False
    for r in runs:
        for n, issues in r["metrics"]["leaks_after_validation"].items():
            for i in issues:
                any_leak = True
                L.append(f"- [{LABEL[r['profile']]} / {r['policy']}] slide {n}: {i}")
    if not any_leak:
        L.append("- none found by the independent audit (removed by the validator are counted in the warnings)")
    L += ["", "## Grounding warnings", ""]
    for r in runs:
        L.append(f"- [{LABEL[r['profile']]} / {r['policy']}] {r['metrics']['grounding_warning_count']} "
                 f"{r['metrics']['warnings_by_category']}")
        for s in r["slides"]:
            for w in s["warnings"]:
                L.append(f"  - slide {s['slide_number']}: {w}")
    L += ["", "## Latency / Tokens", ""]
    for r in runs:
        m = r["metrics"]
        L.append(f"- [{LABEL[r['profile']]} / {r['policy']}] calls={m['llm_calls']}, slides sent={m['slides_sent_to_llm']}, "
                 f"avg/slide={m['avg_latency_per_slide_s']}s, total={m['total_latency_s']}s, tokens in/out="
                 f"{m['input_tokens']}/{m['output_tokens']}, retries={m['retries']}, cache hits={m['cache_hits']}"
                 + (f", cost≈${m['estimated_cost_usd']}" if 'estimated_cost_usd' in m else ""))
    if fallback:
        L += ["", "## Fallback check (real API failures)", ""]
        for k, v in fallback.items():
            L.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
    L += ["", "## Manual review checklist", "",
          "natural Korean · educational clarity · audience fit · difficulty fit · lecture-type fit · source faithfulness · "
          "hallucination · presenter-note usability · visual-instruction quality · repetition", ""]
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- fallback check
def fallback_check(spec, plan, analysis, material, base_url, selected_one: int) -> dict:
    """Real failing calls: a wrong (non-secret) key and a model that does not exist."""
    out = {}
    profile = make_profile("intermediate-practice", "source_first", {})
    cases = {"invalid_key": ("invalid-key-for-fallback-check", DEFAULT_MODEL)}
    real = resolve_key()
    if real:
        cases["unknown_model"] = (real, "no-such-model-xyz")
    for name, (key, model) in cases.items():
        client = build_openai_content_client(api_key=key, model=model, base_url=base_url, timeout=30, temperature=0.1, retries=1)
        rec = LiveRecorder(client)
        enr = SubsetEnricher(rec, batch_size=5, cache=InMemoryCache())
        enr.selected = {selected_one}
        res = enr.enrich(spec, plan, profile, analysis, source_text=material.raw_text)
        s = res.slides[selected_one - 1]
        out[name] = {
            "slide": selected_one, "status": s.status.value, "failure_reason": s.failure_reason,
            "original_kept": bool(s.original.key_message), "structure_ok": res.validation.passed,
            "error_types": dict(rec.error_types), "retries": rec.usage.get("retries", 0),
        }
    return out


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--profile", default="all", help="beginner-theory,intermediate-practice,professional-advanced or all")
    ap.add_argument("--base", default="intermediate-practice", choices=list(PROFILES), help="profile that builds the SlideSpecification")
    ap.add_argument("--slides", type=int, default=8, help="number of representative slides (default 8)")
    ap.add_argument("--slide-numbers", help="explicit slide numbers, e.g. 3,9,11")
    ap.add_argument("--concept", default="MQTT", help="prefer slides of this concept")
    ap.add_argument("--source-policy", default="source_first", help="source_only|source_first|expanded (comma list = compare)")
    ap.add_argument("--set", action="append", default=[], metavar="OPTION=VALUE", help="extra lecture option, e.g. speaker_notes=full")
    ap.add_argument("--smoke", action="store_true", help="1 slide, 1 profile, 1 call")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--cache-dir", type=Path, default=BACKEND / "data" / "live_cache")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the cache (calls the LLM again)")
    ap.add_argument("--outdir", type=Path, default=BACKEND / "data" / "live_reports")
    ap.add_argument("--output", default="live_result.json")
    ap.add_argument("--report", default="live_report.md")
    ap.add_argument("--price-in", type=float, help="USD per 1M input tokens (cost is only computed when given)")
    ap.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent; no call")
    ap.add_argument("--fallback-check", action="store_true", help="also provoke real API failures (wrong key / unknown model)")
    a = ap.parse_args()

    if not a.file.is_file():
        print(f"file not found: {a.file}")
        return 1
    overrides = dict(kv.split("=", 1) for kv in a.set)
    policies = [p.strip() for p in a.source_policy.split(",") if p.strip()]
    names = list(PROFILES) if a.profile == "all" else [p.strip() for p in a.profile.split(",")]
    if a.smoke:
        names, policies, a.slides = [names[0] if a.profile != "all" else "intermediate-practice"], policies[:1], 1

    material, analysis, plan, spec = build_structure(a.file, a.base, "source_first")
    explicit = [int(x) for x in a.slide_numbers.split(",")] if a.slide_numbers else None
    selected = select_slides(spec, a.slides, a.concept, explicit)
    if a.smoke and not explicit:  # the concept slide of the lecture's main term
        cand = [s.slide_number for s in spec.slides if s.slide_type.value == "concept" and a.concept.lower() in [c.lower() for c in s.concepts]]
        selected = cand[:1] or selected[:1]
    types = {n: spec.slides[n - 1].slide_type.value for n in selected}
    print(f"source: {analysis.title}  |  spec: {spec.slide_count} slides  |  selected {len(selected)}: {types}")

    key = resolve_key()
    model = a.model or os.getenv("ENRICHMENT_MODEL") or os.getenv("LLM_MODEL") or DEFAULT_MODEL
    base_url = a.base_url or os.getenv("LLM_BASE_URL") or None

    if a.dry_run:
        prof = make_profile(names[0], policies[0], overrides)
        reqs = [r for r in build_requests(spec, plan, prof, analysis, PROMPT_VERSION) if r.slide.slide_number in selected]
        print("\n--- system prompt (batch) ---\n" + build_batch_system_prompt(reqs))
        user = build_batch_user_message(reqs)
        print(f"\n--- user message: {len(user)} chars for {len(reqs)} slides (whole source: {len(material.raw_text)} chars) ---")
        print(user[:1500])
        return 0

    if not key:
        print("Live test not executed: API key not configured (set LLM_API_KEY in backend/.env, or OPENAI_API_KEY).")
        return 2

    client = build_openai_content_client(api_key=key, model=model, base_url=base_url, timeout=a.timeout,
                                         temperature=a.temperature, retries=a.retries)
    if client is None:
        print("Live test not executed: the 'openai' package is not installed.")
        return 2
    print(f"provider=openai-compatible model={model} base_url={'default' if not base_url else 'custom'} "
          f"temperature={a.temperature} policies={policies} profiles={names}")

    price = (a.price_in, a.price_out) if a.price_in is not None and a.price_out is not None else None
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    a.outdir.mkdir(parents=True, exist_ok=True)
    src_hash = hashlib.sha256(material.raw_text.encode("utf-8")).hexdigest()[:8]
    runs: list[dict] = []
    t_all = time.perf_counter()
    for policy in policies:
        for name in names:
            profile = make_profile(name, policy, overrides)
            cache = InMemoryCache() if a.no_cache else JsonFileCache(
                a.cache_dir / f"{src_hash}_{name}_{policy}_{re.sub(r'[^A-Za-z0-9.-]', '_', model)}.json")
            result, rec, wall, usage = run_one(client, spec, plan, analysis, material, profile, policy, selected,
                                               cache, a.force, a.batch_size, a.concurrency)
            m = metrics_of(result, rec, wall, usage, set(selected), policy, material, plan, analysis, price)
            slides = [slide_record(s, rec.raw.get(s.slide_number), spec.slides[s.slide_number - 1])
                      for s in result.slides if s.slide_number in selected]
            runs.append({"profile": name, "policy": policy, "metrics": m, "slides": slides,
                         "structure": [(s.slide_number, s.section_id, s.slide_type.value, s.estimated_explanation_time)
                                       for s in result.slides],
                         "profile_options": {k: (v.value if hasattr(v, "value") else v) for k, v in
                                             profile.model_dump().items() if k in (
                                                 "audience_level", "difficulty", "lecture_type", "explanation_depth",
                                                 "speaker_notes", "visual_level", "example_level", "practice_level",
                                                 "quiz_mode", "source_policy")}})
            print(f"[{name:22s} {policy:12s}] enriched {m['enriched']}/{m['slides_requested']}  "
                  f"warn={m['grounding_warning_count']}  leaks={m['leak_count']}  calls={m['llm_calls']}  "
                  f"{m['total_latency_s']}s  tokens={m['input_tokens']}/{m['output_tokens']}  "
                  f"cache_hits={m['cache_hits']}  fails={m['failures']}")

    structures = {json.dumps(r["structure"]) for r in runs}
    inner = getattr(client, "_llm", None)
    env = {
        "Provider": "openai-compatible (real API)", "Model": model, "Prompt version": PROMPT_VERSION,
        "Temperature": f"{a.temperature} (sent: {getattr(inner, '_send_temperature', 'n/a')})",
        "Structured output": "json_schema (strict)" if getattr(inner, "_schema_ok", False) else "json_object (provider rejected json_schema)",
        "Source": f"{analysis.title} ({a.file.name})", "Source policy": ", ".join(policies),
        "Slides in specification": spec.slide_count, "Slides tested": f"{len(selected)} {types}",
        "Structure identical across all runs": len(structures) == 1,
        "Batch size / concurrency": f"{a.batch_size} / {a.concurrency}", "Total wall clock": f"{time.perf_counter() - t_all:.1f}s",
        "Lecture option overrides": overrides or "none",
    }
    fb = fallback_check(spec, plan, analysis, material, base_url, selected[0]) if a.fallback_check else None
    a.outdir.mkdir(parents=True, exist_ok=True)
    (a.outdir / a.output).write_text(json.dumps({"environment": env, "runs": runs, "fallback_check": fb},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(a.outdir / a.report, env, runs, fb)
    print(f"structure identical across runs: {len(structures) == 1}")
    if fb:
        print("fallback check:", json.dumps(fb, ensure_ascii=False))
    print(f"wrote {a.outdir / a.output} and {a.outdir / a.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
