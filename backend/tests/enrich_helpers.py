"""Shared helpers for the STAGE 6A tests. No test here ever calls a real LLM or the network."""

from __future__ import annotations

import copy
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app.llm.base import ContentLLMClient, EnrichmentRequest, SlideResult
from app.llm.mock import MockLLMClient
from app.services.llm_client import LLMError
from app.services.slide_content_enricher import SlideContentEnricher
from app.services.slide_planner import SlidePlanner

from plan_helpers import BASE, CASE_A, CASE_B, CASE_C, MQTT_DOCS, analyze_text, plan, profile

_TMP = Path(tempfile.mkdtemp(prefix="ailg_6a_"))


@dataclass
class Ctx:
    analysis: Any
    text: str
    plan: Any
    spec: Any
    profile: Any


@lru_cache(maxsize=None)
def _analysis(doc: str):
    if doc == "mqtt":
        text = MQTT_DOCS[0].read_text(encoding="utf-8")
    else:
        text = doc
    material, analysis = analyze_text(_TMP, text, f"doc{abs(hash(doc))}.md")
    return analysis, material.raw_text


@lru_cache(maxsize=None)
def _ctx(doc: str, opts: tuple) -> Ctx:
    analysis, text = _analysis(doc)
    o = dict(opts)
    p = plan(analysis, **o)
    return Ctx(analysis, text, p, SlidePlanner().plan(p, analysis), profile(**o))


def make_ctx(doc: str = "mqtt", **opts) -> Ctx:
    """(analysis, plan, slide spec, profile) of a document; cached, so treat it as read-only."""
    return _ctx(doc, tuple(sorted(opts.items())))


def spec_copy(ctx: Ctx):
    return copy.deepcopy(ctx.spec)


def run(ctx: Ctx, client: ContentLLMClient | None = None, *, profile_override=None, **kw):
    cache = kw.pop("cache", None)
    enr = SlideContentEnricher(
        client if client is not None else MockLLMClient(), cache=cache,
        batch_size=kw.pop("batch_size", 5), workers=kw.pop("workers", 1),
    )
    return enr.enrich(
        kw.pop("spec", ctx.spec), ctx.plan, profile_override or ctx.profile, ctx.analysis,
        source_text=ctx.text, **kw,
    )


class ScriptedLLM(ContentLLMClient):
    """A (mis)behaving LLM. It starts from what the MockLLMClient would write and lets a hook
    change the answer for a slide, raise an error, or return anything at all.

        patch(request, output) -> output      # e.g. add a forbidden field
        error(request) -> Exception | None    # raise for this slide
        raw(request) -> Any                   # replace the whole answer (e.g. a string)
        batch(requests, results) -> results   # change the batch answer (add/drop/duplicate slides)
    """

    provider = "scripted"
    model = "scripted-1"

    def __init__(self, patch=None, error=None, raw=None, batch=None, delay: Callable[[int], float] | None = None):
        self.base = MockLLMClient()
        self.patch, self.error, self.raw, self.batch, self.delay = patch, error, raw, batch, delay
        self.slide_calls: list[int] = []
        self.batch_calls: list[list[int]] = []

    def enrich_slide(self, request: EnrichmentRequest):
        n = request.slide.slide_number
        self.slide_calls.append(n)
        if self.delay:
            time.sleep(self.delay(n))
        if self.error:
            exc = self.error(request)
            if exc is not None:
                raise exc
        if self.raw:
            r = self.raw(request)
            if r is not None:
                return r
        out = self.base.enrich_slide(request)
        return self.patch(request, out) if self.patch else out

    def enrich_batch(self, requests: list[EnrichmentRequest]) -> list[SlideResult]:
        self.batch_calls.append([r.slide.slide_number for r in requests])
        results = super().enrich_batch(requests)
        return self.batch(requests, results) if self.batch else results


def only(numbers: set[int], exc: Exception):
    """error hook: raise `exc` for the given slide numbers only."""
    return lambda req: exc if req.slide.slide_number in numbers else None


def first_of(spec, slide_type: str, concept: str | None = None):
    """First slide of a type (of a concept: slides of a concept are titled with it)."""
    for s in spec.slides:
        if s.slide_type.value == slide_type and (
            concept is None or concept in getattr(s, "concepts", []) or s.title == concept
        ):
            return s
    raise LookupError(slide_type)


__all__ = [
    "BASE", "CASE_A", "CASE_B", "CASE_C", "Ctx", "LLMError", "MockLLMClient", "ScriptedLLM", "first_of",
    "make_ctx", "only", "run", "spec_copy",
]
