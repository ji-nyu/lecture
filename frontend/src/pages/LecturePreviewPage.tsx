import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { KIND_LABELS, SLIDE_TYPE_LABELS, labelOf } from "../labels";
import type { LecturePlan, LectureSection, LectureScript, Project, Slide, SlideScript, SlideSpecification } from "../types";

function formatSeconds(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m && s) return `${m}분 ${s}초`;
  if (m) return `${m}분`;
  return `${s}초`;
}

async function loadScripts(projectId: string): Promise<Record<number, SlideScript>> {
  const result: LectureScript = await api.getScripts(projectId);
  const map: Record<number, SlideScript> = {};
  for (const s of result.slides) {
    if (s.text.trim()) map[s.slide_number] = s;
  }
  return map;
}

type Phase = "loading" | "planning" | "scripting" | "ready" | "no_profile" | "error";

/**
 * STAGE 5 - Lecture preview. The professor checks the lecture DIRECTION before any
 * presentation is generated: title, objectives, duration, slide estimate and the
 * sections (title / duration / concepts / slides).
 *
 * Actions: Approve, Edit Option, Regenerate Plan. Individual slides are shown read-only
 * (so the professor can see why each slide exists) but are deliberately NOT editable:
 * the professor supervises the overall direction, not every slide.
 */
export default function LecturePreviewPage() {
  const { projectId } = useParams();
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<string | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [plan, setPlan] = useState<LecturePlan | null>(null);
  const [spec, setSpec] = useState<SlideSpecification | null>(null);
  const [notesBySlide, setNotesBySlide] = useState<Record<number, SlideScript>>({});
  const [busy, setBusy] = useState(false);
  const [gen, setGen] = useState<"idle" | "working" | "done" | "failed">("idle");
  const [genMessage, setGenMessage] = useState<string | null>(null);
  const [vid, setVid] = useState<"idle" | "working" | "done" | "failed">("idle");
  const [vidMessage, setVidMessage] = useState<string | null>(null);
  const started = useRef<string | null>(null); // React StrictMode runs effects twice in dev

  /** Load the saved plan/slides; create whatever is missing (or everything when `force`). */
  async function load(force: boolean) {
    if (!projectId) return;
    setError(null);
    setPhase(force ? "planning" : "loading");
    try {
      let p = await api.getProject(projectId);
      if (!p.lecture_profile) {
        setProject(p);
        setPhase("no_profile");
        return;
      }
      if (force || !p.has_plan || !p.has_slides) setPhase("planning");
      const nextPlan = force || !p.has_plan ? await api.createPlan(projectId) : await api.getPlan(projectId);
      const needSlides = force || !p.has_plan || !p.has_slides;
      const nextSpec = needSlides ? await api.createSlides(projectId) : await api.getSlides(projectId);
      p = await api.getProject(projectId);
      setProject(p);
      setPlan(nextPlan);
      setSpec(nextSpec);
      setVid(p.has_video ? "done" : "idle");
      setPhase("scripting");
      try {
        const notes = await loadScripts(projectId);
        setNotesBySlide(notes);
        setSpec(await api.getSlides(projectId));
      } catch {
        setNotesBySlide({});
      }
      setPhase("ready");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "알 수 없는 오류가 발생했습니다.");
      setPhase("error");
    }
  }

  useEffect(() => {
    if (!projectId || started.current === projectId) return;
    started.current = projectId;
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function approve() {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      setProject(await api.approvePlan(projectId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "승인하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    setBusy(true);
    await load(true);
    setBusy(false);
  }

  async function generate() {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    setGen("working");
    setGenMessage("프롬프트를 만들고 Genspark에 슬라이드 생성을 요청했습니다. 몇 분 걸릴 수 있습니다.");
    try {
      await api.buildPrompt(projectId);
      await api.createPresentation(projectId);
      const startedAt = Date.now();
      for (;;) {
        const s = await api.presentationStatus(projectId);
        setGenMessage(
          s.stage ||
            (s.is_mock
              ? "자리표시 슬라이드를 만들고 있습니다."
              : "Genspark에서 슬라이드를 만들고 있습니다. 몇 분 걸릴 수 있습니다.")
        );
        if (s.state === "completed") {
          setGen("done");
          setProject(await api.getProject(projectId));
          return;
        }
        if (s.state === "failed" || s.state === "not_configured") {
          setGen("failed");
          setError(s.message || s.provider_message || "프레젠테이션을 만들지 못했습니다.");
          setProject(await api.getProject(projectId));
          return;
        }
        if (Date.now() - startedAt > 40 * 60 * 1000) {
          setGen("failed");
          setError("생성이 너무 오래 걸려 중단했습니다. 상태 새로고침 후 다시 시도해 주세요.");
          return;
        }
        await new Promise((r) => setTimeout(r, 3000));
      }
    } catch (err) {
      setGen("failed");
      setError(err instanceof ApiError ? err.message : "프레젠테이션을 만들지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    if (!projectId) return;
    setError(null);
    try {
      await api.downloadPresentation(projectId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "파일을 받지 못했습니다.");
    }
  }

  async function makeVideo() {
    if (!projectId) return;
    setVid("working");
    setError(null);
    setVidMessage("대본을 읽고 슬라이드와 합치고 있습니다.");
    try {
      await api.createVideo(projectId, true);
      const startedAt = Date.now();
      for (;;) {
        const s = await api.videoStatus(projectId);
        setVidMessage(s.stage || "강의 영상을 만들고 있습니다.");
        if (s.state === "completed") {
          setVid("done");
          setProject(await api.getProject(projectId));
          return;
        }
        if (s.state === "failed") {
          setVid("failed");
          setError(s.message || "강의 영상을 만들지 못했습니다.");
          return;
        }
        if (Date.now() - startedAt > 90 * 60 * 1000) {
          setVid("failed");
          setError("영상 생성이 너무 오래 걸려 중단했습니다. 다시 시도해 주세요.");
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    } catch (err) {
      setVid("failed");
      setError(err instanceof ApiError ? err.message : "강의 영상을 만들지 못했습니다.");
    }
  }

  async function downloadVideo() {
    if (!projectId) return;
    setError(null);
    try {
      await api.downloadVideo(projectId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "영상을 받지 못했습니다.");
    }
  }

  if (phase === "loading") return <p>강의 구조를 불러오는 중...</p>;
  if (phase === "planning")
    return (
      <section className="card">
        <h2>강의 구조 생성 중...</h2>
        <p className="hint">옵션과 자료 분석 결과로 강의 계획과 슬라이드 구성을 만들고 있습니다.</p>
      </section>
    );
  if (phase === "scripting")
    return (
      <section className="card">
        <h2>대본 작성 중...</h2>
        <p className="hint">각 슬라이드에서 읽을 말을 만들고 있습니다. 처음에는 1–2분 걸릴 수 있습니다.</p>
      </section>
    );
  if (phase === "no_profile")
    return (
      <section className="card">
        <h2>강의 옵션이 필요합니다</h2>
        <p className="error">강의 옵션이 아직 저장되지 않았습니다.</p>
        <Link className="btn" to={`/projects/${projectId}/options`}>
          옵션 선택으로 이동
        </Link>
      </section>
    );
  if (phase === "error" || !plan || !spec || !project)
    return (
      <section className="card">
        <h2>강의 구조를 만들지 못했습니다</h2>
        <p className="error">{error ?? "알 수 없는 오류가 발생했습니다."}</p>
        <div className="actions">
          <Link className="btn secondary" to={`/projects/${projectId}/options`}>
            옵션 수정
          </Link>
          <button type="button" onClick={() => void load(true)}>
            다시 시도
          </button>
        </div>
      </section>
    );

  const imported = project.input_mode === "deck";
  const status = project.presentation_status;
  const approved =
    status === "ready_to_generate" ||
    status === "enriched" ||
    status === "enrichment_partial" ||
    status === "ready_for_presentation" ||
    status === "generating" ||
    status === "completed" ||
    (status === "failed" && Boolean(project.has_presentation === false));
  const canGenerate =
    status === "ready_to_generate" ||
    status === "enriched" ||
    status === "enrichment_partial" ||
    status === "ready_for_presentation" ||
    status === "completed" ||
    status === "failed";
  const done = gen === "done" || status === "completed" || Boolean(project.has_presentation);
  const videoDone = vid === "done" || Boolean(project.has_video);
  const perSection = new Map<string, Slide[]>();
  for (const s of spec.slides) perSection.set(s.section_id, [...(perSection.get(s.section_id) ?? []), s]);
  const warnings = [...plan.warnings, ...spec.warnings];
  const m = plan.metrics;

  return (
    <div className="preview">
      <section className="card">
        <h2>3. 강의 구조 미리보기</h2>
        <h1 className="lecture-title" data-testid="lecture-title">{plan.title}</h1>
        <div className="chips" aria-label="강의 설정">
          <span className="tag">{labelOf("audience_level", plan.audience)}</span>
          <span className="tag">{labelOf("difficulty", plan.difficulty)}</span>
          <span className="tag">{labelOf("lecture_type", plan.lecture_type)}</span>
          <span className="tag">{labelOf("explanation_depth", plan.explanation_depth)}</span>
          <span className="tag">{labelOf("source_policy", plan.source_policy)}</span>
        </div>

        <div className="stats">
          <div>
            <span className="stat-value" data-testid="total-duration">{plan.duration_minutes}분</span>
            <span className="stat-label">총 강의 시간</span>
          </div>
          <div>
            <span className="stat-value" data-testid="slide-count">{plan.estimated_slide_count}장</span>
            <span className="stat-label">예상 슬라이드</span>
          </div>
          <div>
            <span className="stat-value">{plan.sections.length}개</span>
            <span className="stat-label">섹션</span>
          </div>
          <div>
            <span className="stat-value">{m.concept_count}개</span>
            <span className="stat-label">다루는 개념</span>
          </div>
        </div>
        <p className="hint">
          용어 정의 {m.definition_count} · 예제 {m.example_count} · 실습 {m.practice_activity_count}
          (단계 {m.practice_step_count}) · 코드 {m.code_count} · 퀴즈 {m.quiz_question_count}문항 · 설명{" "}
          {m.explanation_minutes}분 / 실습 {m.practice_minutes}분
        </p>

        <h3>학습 목표</h3>
        <ol className="objectives">
          {plan.learning_objectives.map((o) => (
            <li key={o}>{o}</li>
          ))}
        </ol>

        {warnings.length > 0 && (
          <div className="notice">
            <strong>확인해 주세요</strong>
            <ul>
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="card">
        <h2>섹션 구성</h2>
        <Timeline sections={plan.sections} total={plan.duration_minutes} />
        <table className="sections">
          <thead>
            <tr>
              <th>#</th>
              <th>섹션</th>
              <th>시간</th>
              <th>개념</th>
              <th>슬라이드</th>
            </tr>
          </thead>
          <tbody>
            {plan.sections.map((s) => (
              <SectionRow
                key={s.id}
                section={s}
                slides={perSection.get(s.id) ?? []}
                notesBySlide={notesBySlide}
              />
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={2}>합계</td>
              <td>{plan.sections.reduce((n, s) => n + s.duration_minutes, 0)}분</td>
              <td />
              <td>{plan.sections.reduce((n, s) => n + s.estimated_slides, 0)}장</td>
            </tr>
          </tfoot>
        </table>
        <p className="hint">
          {imported
            ? "슬라이드 장수와 순서는 올린 PPT와 같습니다. 각 장 배정 시간에 맞춰 대본 분량을 채웁니다."
            : "슬라이드 하나하나는 수정하지 않습니다. 강의 방향(섹션, 시간, 슬라이드 수)이 맞는지 확인하고 승인하거나, 옵션을 바꿔 다시 생성하세요."}
        </p>
      </section>

      <section className="card" data-testid="script-panel">
        <h2>슬라이드 대본</h2>
        <p className="hint">
          각 슬라이드에서 그대로 읽을 수 있는 말입니다. 자료에 있는 사실만 써서 배정 시간에 맞춰 씁니다.
        </p>
        <ol className="script-list">
          {spec.slides.map((s) => (
            <ScriptItem key={s.slide_number} slide={s} script={notesBySlide[s.slide_number]} />
          ))}
        </ol>
      </section>

      <section className="card actions-card">
        {imported ? (
          <p className="ok" role="status" data-testid="approved">
            ✔ 업로드한 PPT를 그대로 사용합니다. 대본을 확인한 뒤 강의 영상을 만들 수 있습니다.
          </p>
        ) : done ? (
          <p className="ok" role="status" data-testid="approved">
            ✔ 프레젠테이션이 준비되었습니다. 아래 버튼으로 받을 수 있습니다.
          </p>
        ) : approved ? (
          <p className="ok" role="status" data-testid="approved">
            ✔ 이 강의 구조를 승인했습니다. ‘PPT 생성’을 누르면 Genspark CLI가 슬라이드를 만듭니다
            (크레딧이 사용됩니다).
          </p>
        ) : (
          <p className="hint">이 구조로 진행하려면 승인하세요. 승인 후 옵션을 바꾸면 승인이 취소됩니다.</p>
        )}
        {gen === "working" && <p className="hint">{genMessage}</p>}
        {vid === "working" && <p className="hint">{vidMessage}</p>}
        {videoDone && (
          <p className="ok" role="status">
            ✔ 강의 영상이 준비되었습니다. 슬라이드를 보여 주며 대본을 읽습니다.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        <div className="actions">
          {!imported && (
            <button type="button" onClick={approve} disabled={busy || approved} data-testid="approve">
              {approved ? "승인됨" : "승인"}
            </button>
          )}
          {!imported && (
            <button
              type="button"
              onClick={() => void generate()}
              disabled={busy || !canGenerate}
              data-testid="generate-ppt"
            >
              {busy && gen === "working" ? "생성 중..." : done ? "다시 생성" : "PPT 생성"}
            </button>
          )}
          <button type="button" className="secondary" onClick={() => void download()} disabled={!done} data-testid="download-ppt">
            PPT 받기
          </button>
          <button
            type="button"
            onClick={() => void makeVideo()}
            disabled={!done || vid === "working" || busy}
            data-testid="generate-video"
          >
            {vid === "working" ? "영상 생성 중..." : videoDone ? "영상 다시 만들기" : "강의 영상 만들기"}
          </button>
          <button
            type="button"
            className="secondary"
            onClick={() => void downloadVideo()}
            disabled={!videoDone}
            data-testid="download-video"
          >
            영상 받기
          </button>
          <Link className="btn secondary" to={`/projects/${projectId}/options`} data-testid="edit-options">
            옵션 수정
          </Link>
          <button type="button" className="secondary" onClick={regenerate} disabled={busy} data-testid="regenerate">
            {busy && gen !== "working" ? "생성 중..." : imported ? "대본 다시 맞추기" : "계획 다시 생성"}
          </button>
        </div>
        <p className="hint">
          {imported
            ? `슬라이드 화면은 올린 PPT 그대로입니다. 강의 시간과 영상 인트로는 옵션에서 고른 뒤 저장하세요. 영상은 한 장 대본을 끝까지 읽은 뒤에만 다음 장으로 넘어갑니다.${
                project.lecture_profile?.video_intro === "include" && project.lecture_profile.video_intro_file
                  ? ` 선택한 인트로(${project.lecture_profile.video_intro_file})를 앞에 붙입니다.`
                  : ""
              }`
            : `같은 옵션으로 다시 생성하면 같은 구조가 나옵니다(규칙 기반). 구조를 바꾸려면 ‘옵션 수정’에서 옵션을 바꾼 뒤 저장하세요. 강의 영상은 만든 PPT 슬라이드를 보여 주며, 한 장 대본을 끝까지 읽은 뒤에만 다음 슬라이드로 넘어갑니다.${
                project.lecture_profile?.video_intro === "include" && project.lecture_profile.video_intro_file
                  ? ` 선택한 인트로(${project.lecture_profile.video_intro_file})를 앞에 붙입니다.`
                  : ""
              }`}
        </p>
      </section>
    </div>
  );
}

/** Proportional bar: one block per section, width = its share of the lecture time. */
function Timeline({ sections, total }: { sections: LectureSection[]; total: number }) {
  return (
    <div className="timeline" role="img" aria-label="섹션별 시간 배분">
      {sections.map((s) => (
        <div
          key={s.id}
          className={`seg kind-${s.kind}`}
          style={{ flexGrow: s.duration_minutes, flexBasis: 0 }}
          title={`${s.title} · ${s.duration_minutes}분 (${Math.round((s.duration_minutes / total) * 100)}%)`}
        >
          {s.duration_minutes / total >= 0.08 ? `${s.duration_minutes}분` : ""}
        </div>
      ))}
    </div>
  );
}

function scriptText(slide: Slide, script?: SlideScript): string {
  return script?.text.trim() || slide.presenter_instruction;
}

function SlideScript({ slide, script }: { slide: Slide; script?: SlideScript }) {
  const text = scriptText(slide, script);
  return (
    <div className="slide-script">
      <span className="badge ok-badge">대본</span>
      {script && (
        <span className="hint">
          {" "}
          대본 {formatSeconds(script.spoken_seconds)} · 배정 {formatSeconds(slide.estimated_explanation_time)}
        </span>
      )}
      <p className="script-text">{text}</p>
    </div>
  );
}

function ScriptItem({ slide, script }: { slide: Slide; script?: SlideScript }) {
  const [open, setOpen] = useState(true);
  const text = scriptText(slide, script);
  return (
    <li className="script-item" data-testid={`script-item-${slide.slide_number}`}>
      <button
        type="button"
        className="script-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="slide-no">{slide.slide_number}</span>
        <span className="badge">{SLIDE_TYPE_LABELS[slide.slide_type] ?? slide.slide_type}</span>
        <strong>{slide.title}</strong>
        <span className="hint">
          {" "}
          · 배정 {formatSeconds(slide.estimated_explanation_time)}
          {script ? ` · 대본 ${formatSeconds(script.spoken_seconds)}` : ""}
          {script?.source === "llm" ? " · LLM" : ""}
        </span>
        <span className="badge ok-badge">대본</span>
        <span className="script-chevron">{open ? "접기" : "보기"}</span>
      </button>
      {open && (
        <div className="script-body">
          <p className="script-text">{text}</p>
        </div>
      )}
    </li>
  );
}

function SectionRow({
  section,
  slides,
  notesBySlide,
}: {
  section: LectureSection;
  slides: Slide[];
  notesBySlide: Record<number, SlideScript>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className={`kind-row kind-${section.kind}`}>
        <td>{section.order}</td>
        <td>
          <strong>{section.title}</strong>
          <span className={`badge kind-badge kind-${section.kind}`}>
            {KIND_LABELS[section.kind] ?? section.kind}
          </span>
          <div className="hint">{section.purpose}</div>
        </td>
        <td className="num">{section.duration_minutes}분</td>
        <td>
          {section.concepts.length === 0 ? (
            <span className="hint">-</span>
          ) : (
            section.concepts.map((c) => (
              <span className="tag" key={c}>
                {c}
              </span>
            ))
          )}
        </td>
        <td className="num">
          {section.estimated_slides}장
          <div>
            <button type="button" className="link" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
              {open ? "접기" : "슬라이드 보기"}
            </button>
          </div>
        </td>
      </tr>
      {open && (
        <tr className="slides-row">
          <td />
          <td colSpan={4}>
            <ol className="slide-list">
              {slides.map((s) => (
                <li key={s.slide_number}>
                  <span className="slide-no">{s.slide_number}</span>
                  <span className="badge">{SLIDE_TYPE_LABELS[s.slide_type] ?? s.slide_type}</span>
                  {s.content_origin === "suggested" && <span className="badge warn">강사 보충</span>}
                  <strong>{s.title}</strong>
                  <span className="hint"> · {formatSeconds(s.estimated_explanation_time)}</span>
                  <div className="hint">목적: {s.learning_purpose}</div>
                  <SlideScript slide={s} script={notesBySlide[s.slide_number]} />
                </li>
              ))}
            </ol>
          </td>
        </tr>
      )}
    </>
  );
}
