"""Compose a spoken lecture script whose length matches each slide's time.

The SlidePlanner's presenter_instruction is a direction, not words to read.
This builder writes words the lecturer can say for about `estimated_explanation_time`
seconds (about SCRIPT_CHARS_PER_SECOND characters per second of spoken Korean).

  * enriched presenter_notes, when they already fill the time
  * otherwise a composed script from the slide and the source

No new facts: only the lecture title/objectives, the slide's own text, and verbatim
source sentences, plus short speaking cues that add no content. Deterministic.
"""

from __future__ import annotations

import re

from ..models.enriched_slide_spec import EnrichedSlideSpecification, SlideEnrichmentStatus
from ..models.lecture_plan import LecturePlan
from ..models.lecture_profile import LectureProfile
from ..models.lecture_script import LectureScript, SlideScript
from ..models.slide_spec import Slide, SlideSpecification, SlideType
from ..models.source import SourceAnalysis, SourceMaterial
from .prompt_builder import (
    _is_label_point,
    _is_structural_message,
    _norm_text,
    _one_line,
    _sentences,
)
# Default neural TTS reads Korean faster than the 5자/초 note budget.
# Scripts use this rate so a 20-minute lecture is actually spoken, not padded.
SCRIPT_CHARS_PER_SECOND = 6.6

_AUDIENCE = {
    "general": "처음 접하는 분",
    "high_school": "고등학생",
    "university_beginner": "대학에서 처음 배우는 분",
    "university_intermediate": "기본을 아는 분",
    "university_advanced": "전공을 깊게 다루는 분",
    "graduate": "대학원 과정",
    "professional": "실무를 하시는 분",
}
_ORD = ("첫째", "둘째", "셋째", "넷째", "다섯째", "여섯째", "일곱째", "여덟째")
_CUE = (
    "화면에 나온 문장을",
    "눈에 담아",
    "되짚어 보세요",
    "기억해 두세요",
    "자료에는 이렇게 적혀",
    "이 문장을 함께 읽겠습니다",
    "다시 한 번 말하면 이렇습니다",
    "강의에서 꼭 기억할 내용입니다",
    "핵심을 다시 정리합니다",
)
_LABEL_HEADS = ("선행 개념:", "연관 개념:", "강의 구성:", "언어:")
LOW, HIGH = 0.88, 1.12


class LectureScriptBuilder:
    def build(
        self,
        *,
        project_id: str,
        plan: LecturePlan,
        spec: SlideSpecification,
        profile: LectureProfile | None = None,
        analysis: SourceAnalysis | None = None,
        material: SourceMaterial | None = None,
        enriched: EnrichedSlideSpecification | None = None,
    ) -> LectureScript:
        notes = _enriched_notes(enriched)
        slides: list[SlideScript] = []
        said: list[str] = []
        for slide in spec.slides:
            text, origin = self._for_slide(
                plan, profile, slide, analysis, material, notes.get(slide.slide_number), said
            )
            slides.append(
                SlideScript(
                    slide_number=slide.slide_number,
                    title=slide.title,
                    slide_type=slide.slide_type.value,
                    estimated_seconds=slide.estimated_explanation_time,
                    spoken_seconds=_spoken_seconds(text),
                    text=text,
                    source=origin,
                )
            )
        result = LectureScript(
            project_id=project_id,
            title=plan.title,
            slide_count=len(slides),
            total_seconds=sum(s.estimated_seconds for s in slides),
            slides=slides,
        )
        return result

    def _for_slide(
        self,
        plan: LecturePlan,
        profile: LectureProfile | None,
        slide: Slide,
        analysis: SourceAnalysis | None,
        material: SourceMaterial | None,
        notes: str | None,
        said: list[str] | None = None,
    ) -> tuple[str, str]:
        target = _target_chars(slide)
        imported = _is_imported(plan)
        recap = slide.slide_type == SlideType.summary
        if notes and _chars(notes) >= int(target * LOW) and not imported:
            text = _spoken_form(_trim(notes, int(target * HIGH)))
            _remember_claims(said, text)
            return text, "enriched"
        if slide.slide_type == SlideType.summary:
            facts = _facts(slide, []) if imported else _summary_facts(plan, analysis, slide)
            parts = ["지금까지 배운 내용을 한 흐름으로 정리합니다.", *facts]
            if plan.learning_objectives and not imported:
                parts.append("오늘 목표로 두었던 것은 이렇습니다.")
                parts.extend(plan.learning_objectives)
            if notes:
                parts = [_one_line(notes), *parts]
            extras = facts or ([] if imported else plan.learning_objectives)
            text = _fill(parts, extras, target, stretch=True)
            _remember_claims(said, text)
            return text, "composed"
        if slide.slide_type in (SlideType.title, SlideType.agenda):
            parts = _opening(plan, profile, slide)
            extras = _facts(slide, []) if imported else list(plan.learning_objectives)
            if imported:
                extras = _without_said(extras, said)
                parts = parts + extras
            if notes:
                parts = [_one_line(notes), *parts]
            text = _fill(parts, extras, target, stretch=imported)
            if imported:
                text = _strip_said_text(text, said)
                if _chars(text) < int(target * LOW):
                    kept = [p for p in text.split("\n\n") if p]
                    text = _fill(kept or parts, extras, target, stretch=True)
            _remember_claims(said, text)
            return text, "composed"
        extra = _imported_page_lines(slide, material) if imported else _source_sentences(slide, plan, analysis, material)
        if imported:
            facts = _without_said(_prefer(slide, _facts(slide, extra)), said)
        else:
            facts = _prefer(slide, _facts(slide, _without_said(extra, said)))
        parts = _opening(plan, profile, slide) + _deliver(slide, facts, numbered=not imported)
        if notes:
            parts = [_one_line(notes), *parts]
        text = _fill(parts, facts, target, stretch=True)
        if imported and not recap:
            text = _strip_said_text(text, said)
            if _chars(text) < int(target * LOW):
                kept = [p for p in text.split("\n\n") if p]
                text = _fill(kept or parts, facts, target, stretch=True)
        _remember_claims(said, text)
        return text, "composed"


def _is_imported(plan: LecturePlan) -> bool:
    return getattr(plan, "planner", "") == "imported-deck-v1"


def speakable_chars(slide: Slide) -> int:
    """How much of this slide can actually be read aloud."""
    n = 0
    for raw in (slide.key_message, *slide.key_points):
        line = _usable_line(raw, slide)
        if line:
            n += len(line)
    return max(20, n)


def rebalance_imported_times(spec: SlideSpecification, total_seconds: int) -> bool:
    """Spread the lecture duration by speakable text, not page chrome."""
    from .deck_importer import _allocate_speakable

    times = _allocate_speakable([speakable_chars(s) for s in spec.slides], total_seconds)
    changed = False
    for slide, seconds in zip(spec.slides, times):
        if slide.estimated_explanation_time != seconds:
            slide.estimated_explanation_time = seconds
            changed = True
    if spec.metrics is not None:
        spec.metrics.total_seconds = total_seconds
    return changed


def _enriched_notes(enriched: EnrichedSlideSpecification | None) -> dict[int, str]:
    if enriched is None:
        return {}
    out: dict[int, str] = {}
    for s in enriched.slides:
        if s.status != SlideEnrichmentStatus.enriched or s.enriched is None:
            continue
        notes = (s.enriched.presenter_notes or "").strip()
        if notes:
            out[s.slide_number] = notes
    return out


def _target_chars(slide: Slide) -> int:
    share = 0.4 if slide.slide_type == SlideType.practice else 1.0
    return max(80, int(slide.estimated_explanation_time * SCRIPT_CHARS_PER_SECOND * share))


def _spoken_seconds(text: str) -> int:
    return max(1, round(_chars(text) / SCRIPT_CHARS_PER_SECOND))


def _chars(text: str) -> int:
    return len(text.replace("\r", ""))


_PLAIN_ENDINGS = (
    ("해야 한다", "해야 합니다"),
    ("해야한다", "해야 합니다"),
    ("수 있다", "수 있습니다"),
    ("수 없다", "수 없습니다"),
    ("때문이다", "때문입니다"),
    ("것이다", "것입니다"),
    ("점이다", "점입니다"),
    ("선택이다", "선택입니다"),
    ("기능이다", "기능입니다"),
    ("모델이다", "모델입니다"),
    ("프로토콜이다", "프로토콜입니다"),
    ("중심이다", "중심입니다"),
    ("장치다", "장치입니다"),
    ("아니다", "아닙니다"),
    ("가능하다", "가능합니다"),
    ("필요하다", "필요합니다"),
    ("일반적이다", "일반적입니다"),
    ("메시지다", "메시지입니다"),
    ("주제다", "주제입니다"),
    ("범위다", "범위입니다"),
    ("까지다", "까지입니다"),
    ("같다", "같습니다"),
    ("쉽다", "쉽습니다"),
    ("어렵다", "어렵습니다"),
    ("쓰인다", "쓰입니다"),
    ("다룬다", "다룹니다"),
    ("해야 해요", "해야 합니다"),
    ("이에요", "입니다"),
    ("예요", "입니다"),
    ("해요", "합니다"),
    ("않는다", "않습니다"),
    ("못한다", "못합니다"),
    ("모른다", "모릅니다"),
    ("따른다", "따릅니다"),
    ("맡는다", "맡습니다"),
    ("만든다", "만듭니다"),
    ("보낸다", "보냅니다"),
    ("받는다", "받습니다"),
    ("쓴다", "씁니다"),
    ("준다", "줍니다"),
    ("온다", "옵니다"),
    ("간다", "갑니다"),
    ("진다", "집니다"),
    ("된다", "됩니다"),
    ("었다", "었습니다"),
    ("였다", "였습니다"),
    ("한다", "합니다"),
    ("많다", "많습니다"),
    ("적다", "적습니다"),
    ("크다", "큽니다"),
    ("좋다", "좋습니다"),
    ("있다", "있습니다"),
    ("없다", "없습니다"),
    ("이다", "입니다"),
)
_END_MARK = re.compile(r"[.!?…」\"']")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.+_-]*")


_PAREN = re.compile(r"\s*[\(（][^)）]{0,80}[\)）]")
_TIME_SPAN = re.compile(r"\d+\s*분(?:짜리)?(?:\s*동안)?|\d+\s*초(?:\s*동안)?")
_SLIDE_INDEX = re.compile(r"\b\d{1,2}\s*[/\-─–—]\s*\d{1,2}\b")
_ARROW = re.compile(r"\s*[→←⇒➡➔➝➞]+\s*")
_DASH_WRAP = re.compile(r"[—–─―]\s*([^—–─―]{1,80}?)\s*[—–─―]")
_DASH = re.compile(r"\s*[—–─―]+\s*")
_MIDDOT = re.compile(r"\s*[·•∙]\s*")
_SLASH_WORDS = re.compile(r"(?<=[가-힣A-Za-z])\s*/\s*(?=[가-힣A-Za-z])")
_CHROME_LINE = re.compile(
    r"^(DEFINITION|FOR EXAMPLE|EXAMPLE|SUMMARY|AGENDA|CONTENTS|TITLE|KEY POINT|"
    r"상황|동작|결과|판단|용어 구성)$"
    r"|^\d{1,2}\s*[/\-─–—]\s*\d{1,2}$"
    r"|^SECTION\s+\d+",
    re.I,
)
_BAD_CONCEPT = {"SECTION", "DEFINITION", "EXAMPLE", "TITLE", "AGENDA", "CONTENTS"}
_ORD_PREFIX = re.compile(r"^(첫째|둘째|셋째|넷째|다섯째|여섯째|일곱째|여덟째),\s*")
_SKIP_SCRIPT_HEADS = (
    "화면에 나온",
    "화면의 ‘",
    "화면의 '",
    "화면에 있는",
    "강의는 다음 순서로",
    "오늘은 ‘",
    "오늘은 '",
    "지금까지 배운",
    "방금 배운",
    "풀어서 말하면",
    "같은 내용을 이어서",
    "이 점을 강의 말로",
    "다시 말하면",
    "같은 사실을 이어서",
    "정리하면",
    "이 화면에서 기억할 점은",
    "한 줄로 모으면",
    "뜻을 풀면",
    "핵심만 남기면",
    "앞에서 본 대로",
    "이어서 설명하면",
    "강의에서는 이렇게",
    "같은 정의를 유지하면",
    "이 문장만 따라가면",
    "이 강의를 마치면",
    "처음 접하는 분",
    "이 순서를 기억해",
    "오늘 목표로 두었던",
)


def _drop_parens(text: str) -> str:
    """Spoken Korean does not read the bracketed gloss. '사물인터넷(IoT)' -> '사물인터넷'."""
    if not text:
        return text
    out = text
    prev = None
    while prev != out:
        prev = out
        out = _PAREN.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.replace(" ,", ",").replace(" .", ".").strip()


def _is_chrome_line(text: str) -> bool:
    line = _one_line(text)
    return bool(line) and bool(_CHROME_LINE.match(line))


def _display_title(title: str) -> str:
    t = _one_line(title)
    t = re.sub(r"^SECTION\s*\d+\s*[·.\-–—:]*\s*", "", t, flags=re.I)
    t = re.sub(r"^(용어 정의|예시)\s*:\s*", "", t)
    return t.strip(" ·.-")


def _topic_name(slide: Slide) -> str:
    title = _make_speakable(_display_title(slide.title)) or _display_title(slide.title)
    if title:
        return title
    for name in slide.concepts:
        if name and name.upper() not in _BAD_CONCEPT:
            return name
    return _make_speakable(slide.title) or slide.title


def _make_speakable(text: str) -> str:
    """Turn slide marks into words a lecturer can say. No clock phrases."""
    s = _one_line(text)
    if not s or _is_chrome_line(s):
        return ""
    s = _TIME_SPAN.sub("", s)
    s = _SLIDE_INDEX.sub("", s)
    s = _DASH_WRAP.sub(r"\1", s)
    s = _ARROW.sub(", ", s)
    s = _DASH.sub(" ", s)
    s = _MIDDOT.sub(" ", s)
    s = _SLASH_WORDS.sub("과 ", s)
    s = re.sub(r"(?<=\d)\s*/\s*(?=\d)", ", ", s)
    s = s.replace("/", "과 ")
    s = s.replace("→", ", ").replace("←", ", ").replace("⇒", ", ")
    s = s.replace("—", " ").replace("–", " ").replace("─", " ").replace("―", " ")
    s = s.replace("·", " ").replace("•", " ")
    s = re.sub(r"\s*,\s*,+", ", ", s)
    s = re.sub(r"([A-Za-z0-9])\s+을\b", r"\1를", s)
    s = re.sub(r",\s*강의입니다", " 강의입니다", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,")


_CASE_HEAD = re.compile(r"^사례\s*0*(\d+)\s+(.+)$")
_DONE = re.compile(r"(다|요|니다|까|죠)[.!?…]*$")
_ACTION = (
    "감지", "추정", "소등", "측정", "통보", "학습", "비교", "파악",
    "연결", "전달", "저장", "판단", "구독", "발행", "수집", "조정", "통보",
)


def _is_broken_spoken(text: str) -> bool:
    """Slide chrome that is not a finished spoken sentence."""
    t = _one_line(text)
    if not t:
        return True
    if re.search(r"(지만|거나|고|며|거쳐)입니다[.!?]*$", t):
        return True
    if "," in t and re.search(r"(가능|없음|있음)입니다[.!?]*$", t):
        return True
    if len(t) < 80 and ("…" in t or "..." in t):
        return True
    if len(t) < 48 and t.endswith(("고", "며", "거쳐", "고,", "며,", "지만", "거나", "을", "를", "은", "는")):
        return True
    return False


def _speak_point(text: str) -> str:
    """Turn a slide fragment into one spoken sentence. No new facts."""
    t = _one_line(text)
    if not t or "…" in t or "..." in t:
        return ""
    case = _CASE_HEAD.match(t)
    if case:
        n = int(case.group(1))
        place = case.group(2).strip()
        head = _ORD[n - 1] if 1 <= n <= len(_ORD) else f"{n}번째"
        return f"{head} 사례는 {place}입니다"
    if _DONE.search(t):
        return t
    t = t.replace("학습 비교", "학습하고 비교")
    t = re.sub(r"값을 모음$", "값을 모읍니다", t)
    t = re.sub(r"(?<![가-힣])모음$", "모읍니다", t)
    t = re.sub(r"를 모음$", "를 모읍니다", t)
    t = re.sub(r"을 모음$", "을 모읍니다", t)
    if t.endswith("모음"):
        t = t[:-2] + "모읍니다"
    if t.endswith(("고", "며", "거쳐", "지만", "거나", "을", "를", "은", "는")):
        return ""
    for stem in _ACTION:
        if t.endswith(stem) and len(t) >= 12:
            return t + "합니다"
    if t.endswith("예") and len(t) >= 10:
        return t + "입니다"
    if len(t) >= 16 and not _DONE.search(t) and not t.endswith("합니다") and "," not in t:
        return t + "입니다"
    return t


def _spoken_form(text: str) -> str:
    """합니다체, no parenthetical glosses, no unreadable marks or clock phrases."""
    if not text:
        return text
    kept: list[str] = []
    for para in text.replace("\r", "").split("\n\n"):
        line = _make_speakable(_drop_parens(para))
        if not line or _is_chrome_line(line) or _is_broken_spoken(line):
            continue
        kept.append(_latin_particles(_polite_line(line)))
    return "\n\n".join(kept)


def _polite(text: str) -> str:
    """Turn 한다/이다 endings into spoken 합니다체. Facts stay the same."""
    if not text:
        return text
    return "\n\n".join(_polite_line(p) for p in text.replace("\r", "").split("\n\n"))


def script_body_points(text: str, limit: int) -> list[str]:
    """Teaching sentences from a spoken script, for the slide that script belongs to."""
    out: list[str] = []
    seen: set[str] = set()
    for para in (text or "").replace("\r", "").split("\n\n"):
        line = _ORD_PREFIX.sub("", _one_line(para))
        if not line or _looks_cue(line) or _looks_meta(line):
            continue
        if any(line.startswith(h) for h in _SKIP_SCRIPT_HEADS):
            continue
        key = _norm_text(line)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _latin_particles(text: str) -> str:
    """English tokens have no 받침: 와/를/가/는, not 과/을/이/은."""

    def swap(match: re.Match[str]) -> str:
        word = match.group(1)
        particle = match.group(2)
        fixed = {"과": "와", "을": "를", "이": "가", "은": "는"}.get(particle, particle)
        return word + fixed

    return re.sub(rf"({_LATIN_TOKEN.pattern})(과|을|이|은)(?=[은는이가을를와과\s,.!?]|$)", swap, text)


def _obj_particle(name: str) -> str:
    core = (name or "").strip(" ‘’'")
    if re.search(r"[A-Za-z0-9]$", core):
        return "를"
    return "을"


def _polite_line(text: str) -> str:
    s = text.strip()
    if not s:
        return s
    for src, dst in _PLAIN_ENDINGS:
        s = _replace_ending(s, src, dst)
    return s


def _replace_ending(text: str, src: str, dst: str) -> str:
    out: list[str] = []
    i = 0
    n = len(src)
    while i < len(text):
        j = text.find(src, i)
        if j < 0:
            out.append(text[i:])
            break
        end = j + n
        nxt = text[end : end + 1]
        if nxt == "" or _END_MARK.match(nxt) or nxt.isspace():
            out.append(text[i:j])
            out.append(dst)
            i = end
        else:
            out.append(text[i : j + 1])
            i = j + 1
    return "".join(out)


def _same_claim(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb or na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 24 and long.startswith(short)


def _already_covered(line: str, said: list[str] | None) -> bool:
    if not said:
        return False
    nl = _norm_text(line)
    if not nl:
        return False
    for prev in said:
        if _same_claim(line, prev):
            return True
        np = _norm_text(prev)
        if len(np) >= 28 and np in nl and len(nl) <= len(np) + 16:
            return True
    return False


def _without_said(facts: list[str], said: list[str] | None) -> list[str]:
    return [f for f in facts if not _already_covered(f, said)]


def _remember_claims(said: list[str] | None, text: str) -> None:
    if said is None:
        return
    for para in (text or "").replace("\r", "").split("\n\n"):
        line = _ORD_PREFIX.sub("", _one_line(para))
        if not line or _looks_cue(line) or _looks_meta(line):
            continue
        if any(line.startswith(h) for h in _SKIP_SCRIPT_HEADS):
            continue
        if len(_norm_text(line)) < 20:
            continue
        if not _already_covered(line, said):
            said.append(line)


def _strip_said_text(text: str, said: list[str] | None) -> str:
    if not text or not said:
        return text
    kept: list[str] = []
    for para in text.replace("\r", "").split("\n\n"):
        line = _one_line(para)
        if not line:
            continue
        if _already_covered(line, said):
            continue
        kept.append(line)
    return "\n\n".join(kept)


def _facts(slide: Slide, extra: list[str]) -> list[str]:
    raw: list[str] = []
    if slide.key_message:
        raw.append(slide.key_message)
    raw.extend(p for p in slide.key_points if p)
    raw.extend(extra)
    out: list[str] = []
    for item in raw:
        line = _usable_line(item, slide)
        if not line:
            continue
        if any(_same_claim(line, prev) for prev in out):
            continue
        out.append(line)
    return out


def _prefer(slide: Slide, facts: list[str]) -> list[str]:
    t = slide.slide_type
    def_like = [f for f in facts if _looks_definition(f)]
    ex_like = [f for f in facts if _looks_example(f)]
    other = [f for f in facts if f not in def_like and f not in ex_like]
    if t == SlideType.definition:
        return def_like + other + ex_like
    if t == SlideType.example:
        return ex_like + other + def_like
    if t in (SlideType.concept, SlideType.architecture, SlideType.workflow, SlideType.diagram):
        return other + def_like + ex_like
    return facts


def _looks_definition(text: str) -> bool:
    return any(k in text for k in ("약자로", "를 말한다", "을 말한다", "란 무엇인가", "의미는"))


def _looks_example(text: str) -> bool:
    return text.startswith("예를") or text.startswith("예:") or "예를 들어" in text


def _looks_meta(text: str) -> bool:
    """Source preamble / writing notes, not lecture content."""
    return any(
        k in text
        for k in (
            "개념명이 아니다",
            "아래에서 따로 정의",
            "강의노트이다",
            "문장 속에 데이터가 있다",
            "일상 문장은",
        )
    )


def _looks_cue(text: str) -> bool:
    """Stage directions and empty-time fillers a lecturer would not say."""
    return any(k in text for k in _CUE)


def _speak_label(text: str) -> str | None:
    """Turn a planner label into a spoken sentence, or '' to drop it."""
    t = _one_line(text)
    if t.startswith("선행 개념:") or t.startswith("연관 개념:"):
        return ""
    if t.startswith("강의 구성:"):
        rest = re.sub(r"\s*\(\d+분\)", "", t.split(":", 1)[1])
        rest = re.sub(r"\s*[→\-]+\s*", ", ", rest)
        rest = re.sub(r"\s+", " ", rest).strip(" ,")
        return f"순서는 {rest}입니다." if rest else ""
    if t.startswith("언어:"):
        return ""
    return None


def _usable_line(text: str, slide: Slide | None = None) -> str | None:
    raw = _one_line(text)
    if not raw or _looks_meta(raw) or _looks_cue(raw) or _is_chrome_line(raw):
        return None
    spoken = _speak_label(raw)
    if spoken is not None:
        return _make_speakable(_drop_parens(spoken)) or None
    line = _make_speakable(_drop_parens(raw))
    if not line or _is_chrome_line(line):
        return None
    if slide is not None and (_is_label_point(line, slide) or _is_label_point(raw, slide)):
        return None
    if _is_structural_message(line) or _is_structural_message(raw) or len(line) < 8:
        return None
    if not re.search(r"[가-힣]", line) and len(line.split()) <= 2:
        return None
    spoken = _polite_line(_speak_point(line))
    if not spoken or _is_broken_spoken(spoken) or _is_broken_spoken(line):
        return None
    if len(spoken) <= 12 and spoken.endswith(("이", "가", "을", "를", "은", "는")):
        return None
    if not re.search(r"(습니다|입니다|니다|까)[.!?…]*$", spoken):
        return None
    return spoken


def _summary_facts(plan: LecturePlan, analysis: SourceAnalysis | None, slide: Slide) -> list[str]:
    names = list(dict.fromkeys(slide.concepts or [c for s in plan.sections for c in s.concepts]))
    by_name = {x.name: x for x in analysis.concepts} if analysis else {}
    out: list[str] = []
    for name in names:
        info = by_name.get(name)
        if info and info.description and not _looks_meta(info.description):
            out.append(_one_line(info.description))
    return out


def _imported_page_lines(slide: Slide, material: SourceMaterial | None) -> list[str]:
    """Only this PPT page's on-screen lines. No other slides, no long speaker notes."""
    if material is None or not material.sections:
        return []
    page = slide.source_reference.page if slide.source_reference and slide.source_reference.page else slide.slide_number
    out: list[str] = []
    pending = ""
    for sec in material.sections:
        if sec.page != page or not sec.text:
            continue
        for raw in sec.text.splitlines():
            piece = _make_speakable(_drop_parens(_one_line(raw)))
            if not piece or len(piece) > 120:
                continue
            if pending:
                piece = f"{pending} {piece}"
                pending = ""
            if piece.endswith(("고", "며", "거쳐", "고,", "며,")):
                pending = piece
                continue
            line = _usable_line(piece, slide)
            if line:
                out.append(line)
        if pending:
            line = _usable_line(pending, slide)
            if line:
                out.append(line)
            pending = ""
    return out


def _source_sentences(
    slide: Slide,
    plan: LecturePlan,
    analysis: SourceAnalysis | None,
    material: SourceMaterial | None,
) -> list[str]:
    cands: list[str] = []
    ref = slide.source_reference
    section = next((s for s in plan.sections if s.id == slide.section_id), None)
    ids = list(section.source_section_ids) if section else []
    if ref and ref.section_id and ref.section_id not in ids:
        ids.insert(0, ref.section_id)
    titles = [ref.section_title] if ref and ref.section_title else []
    if material and material.sections:
        for sec in material.sections:
            take = (sec.id in ids) or any(t and t in (sec.title or "") for t in titles)
            if take and sec.text:
                cands.extend(_sentences(sec.text))
    if section:
        cands.extend(section.source_points)
    if analysis:
        by_name = {x.name: x for x in analysis.concepts}
        for name in slide.concepts:
            info = by_name.get(name)
            if info and info.description:
                cands.extend(_sentences(info.description) or [_one_line(info.description)])
        for d in analysis.definitions:
            if d.term in slide.concepts and d.definition:
                cands.extend(_sentences(d.definition) or [_one_line(d.definition)])
        for ip in analysis.important_points:
            loc = ip.source_location
            if not ip.text:
                continue
            if loc and (loc.section_id in ids or (ref and loc.section_id == ref.section_id)):
                cands.append(_one_line(ip.text))
        for sec in analysis.sections:
            if sec.id in ids and sec.summary:
                cands.extend(_sentences(sec.summary) or [_one_line(sec.summary)])
    return cands


def _opening(plan: LecturePlan, profile: LectureProfile | None, slide: Slide) -> list[str]:
    t = slide.slide_type
    name = _topic_name(slide)
    if t == SlideType.title:
        if _is_imported(plan):
            return [f"오늘은 ‘{plan.title}’를 살펴보겠습니다."]
        return _title_parts(plan, profile)
    if t == SlideType.agenda:
        return _agenda_parts(slide, plan)
    if _is_imported(plan):
        if t == SlideType.definition:
            return [f"‘{name}’의 정의입니다."]
        if t == SlideType.example or any("사례" in (p or "") for p in slide.key_points[:6]):
            return [f"‘{name}’ 사례입니다."]
        if t == SlideType.summary:
            return ["핵심을 정리합니다."]
        return [f"‘{name}’{_obj_particle(name)} 설명합니다."]
    if t == SlideType.definition:
        lead = f"화면에 나온 ‘{name}’ 정의를 함께 읽겠습니다."
    elif t == SlideType.example:
        lead = f"화면에 있는 ‘{name}’ 사례를 따라가 보겠습니다."
    elif t == SlideType.summary:
        return ["지금까지 배운 내용을 한 흐름으로 정리합니다."]
    elif t == SlideType.practice:
        lead = f"이번에는 ‘{slide.title}’을 단계대로 따라 가겠습니다."
    elif t == SlideType.quiz:
        lead = "방금 배운 내용을 확인해 보겠습니다."
    elif t == SlideType.code:
        lead = f"자료에 있는 ‘{slide.title}’ 코드를 위에서부터 읽어 보겠습니다."
    else:
        lead = f"화면의 ‘{name}’ 항목을 순서대로 살펴보겠습니다."
    return [lead]


def _title_parts(plan: LecturePlan, profile: LectureProfile | None) -> list[str]:
    audience = _AUDIENCE.get(plan.audience, "여러분")
    if profile is not None:
        audience = _AUDIENCE.get(profile.audience_level.value, audience)
    parts = [
        f"오늘은 ‘{plan.title}’를 살펴보겠습니다.",
        f"{audience}과 함께 진행합니다.",
    ]
    if plan.learning_objectives:
        parts.append("이 강의를 마치면 다음을 할 수 있습니다.")
        parts.extend(plan.learning_objectives)
    return parts


def _agenda_parts(slide: Slide, plan: LecturePlan) -> list[str]:
    parts = ["강의는 다음 순서로 진행합니다."]
    points = [line for p in slide.key_points if p and (line := _usable_line(p, slide))]
    if not points:
        points = [s.title for s in plan.sections]
    parts.extend(points)
    parts.append("이 순서를 기억해 두고 본론으로 들어가겠습니다.")
    return parts


def _deliver(slide: Slide, facts: list[str], *, numbered: bool = True) -> list[str]:
    if not facts:
        name = _topic_name(slide)
        return [f"이 슬라이드에서는 ‘{name}’{_obj_particle(name)} 다룹니다."]
    if not numbered:
        return list(facts)
    out: list[str] = []
    for i, fact in enumerate(facts):
        if i < len(_ORD) and len(facts) > 1:
            out.append(f"{_ORD[i]}, {fact}")
        else:
            out.append(fact)
    return out


_CLAUSE_SPLIT = re.compile(
    r"(?<=다)\.\s+|(?<=요)\.\s+|(?<=니다)\.\s+|(?<=고)\s+|(?<=며)\s+|(?<=지만)\s+"
)
_ABBR = re.compile(r"(.{1,40}?)는\s+(.+?)의 약자")
_TAILS = (
    "이 점이 이 화면의 핵심입니다.",
    "이 내용으로 이해하면 됩니다.",
    "이 설명이 이 슬라이드에서 말할 내용입니다.",
    "이 뜻을 기준으로 보면 됩니다.",
    "이 문장이 이 화면의 중심입니다.",
    "이 사실을 그대로 가져가면 됩니다.",
    "이 정의가 이 장의 출발점입니다.",
    "이 내용만 분명히 하면 됩니다.",
    "이 말을 기준으로 이어가면 됩니다.",
    "같은 화면의 다음 설명으로 이어집니다.",
)


def _fill(parts: list[str], facts: list[str], target: int, *, stretch: bool = True) -> str:
    min_c, max_c = int(target * LOW), int(target * HIGH)
    pool: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        line = _usable_line(raw)
        if not line or _looks_cue(line) or _looks_meta(line):
            return
        key = _norm_text(line)
        if not key or key in seen:
            return
        seen.add(key)
        pool.append(line)

    for raw in parts:
        add(raw)
    for fact in facts:
        add(fact)
    units: list[str] = []
    for fact in facts:
        if _one_line(fact):
            units.append(fact)
        units.extend(_expand_fact(fact))
    if not units:
        units = list(pool)
    if stretch:
        for extra in units:
            if _chars("\n\n".join(pool)) >= min_c:
                break
            add(extra)
        more = [piece for line in list(pool) for piece in _expand_fact(line)]
        for extra in more:
            if _chars("\n\n".join(pool)) >= min_c:
                break
            add(extra)
        stems: list[str] = []
        for raw in [*units, *pool]:
            line = _usable_line(raw) or _one_line(raw)
            if _stretchable(line) and line not in stems:
                stems.append(line)
        for width in range(2, min(5, len(stems) + 1)):
            for i in range(len(stems)):
                if _chars("\n\n".join(pool)) >= min_c:
                    break
                add(" ".join(stems[(i + k) % len(stems)] for k in range(width)))
        n = 0
        while stems and _chars("\n\n".join(pool)) < min_c and n < 80:
            line = stems[n % len(stems)]
            nxt = stems[(n + 1) % len(stems)]
            tail = _TAILS[n % len(_TAILS)]
            if n < len(stems) * len(_TAILS):
                add(f"{line} {tail}")
            else:
                add(f"{line} {nxt} {tail}")
            n += 1
    if not pool:
        return "이 슬라이드의 내용을 함께 보겠습니다."
    if not stretch:
        return _spoken_form("\n\n".join(pool))
    return _spoken_form(_join(pool, max_c))


def _stretchable(line: str | None) -> bool:
    """Only expand real teaching sentences, not short slide labels."""
    t = _one_line(line or "")
    if len(t) < 18:
        return False
    if re.search(r"사례는\s+\S{1,12}입니다", t):
        return False
    return bool(re.search(r"(습니다|입니다|니다)[.!?]*$", t))


def _finish_clause(chunk: str) -> str:
    c = chunk.strip(" ,.")
    if not c:
        return ""
    if c.endswith("약자로"):
        return c[:-1] + "입니다"
    if c.endswith((
        "면", "며", "고", "거나", "지만", "든",
        "을", "를", "은", "는", "에", "의", "와", "과", "으로", "로", "만",
    )):
        return c
    return _speak_point(c)


def _expand_fact(text: str) -> list[str]:
    """Same source sentence, spoken as smaller units. No new claims."""
    line = _one_line(text)
    if len(line) < 20:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def keep(raw: str) -> None:
        spoken = _finish_clause(raw)
        key = _norm_text(spoken)
        if not spoken or _is_broken_spoken(spoken) or key == _norm_text(line) or key in seen:
            return
        seen.add(key)
        out.append(spoken)

    abbr = _ABBR.search(line)
    if abbr:
        keep(f"{abbr.group(1).strip()}는 {abbr.group(2).strip()}의 약자입니다")
    for chunk in _CLAUSE_SPLIT.split(line):
        if len(chunk.strip(" ,.")) >= 10:
            keep(chunk)
    if len(line) >= 40:
        for chunk in re.split(r",\s+", line):
            if len(chunk.strip(" ,.")) >= 12:
                keep(chunk)
    return out


def _unpack(text: str) -> list[str]:
    """Split a long source sentence into spoken pieces. No new facts."""
    return _expand_fact(text)


def _join(parts: list[str], max_chars: int) -> str:
    out: list[str] = []
    n = 0
    for raw in parts:
        p = _one_line(raw)
        if not p:
            continue
        extra = (2 if out else 0) + len(p)
        if out and n + extra > max_chars:
            break
        out.append(p)
        n += extra
    return "\n\n".join(out) or parts[0]


def _trim(text: str, max_chars: int) -> str:
    t = text.strip()
    if _chars(t) <= max_chars:
        return t
    parts = [p.strip() for p in t.split("\n\n") if p.strip()]
    return _join(parts, max_chars)
