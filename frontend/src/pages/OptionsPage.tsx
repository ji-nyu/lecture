import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type VideoIntroItem } from "../api";
import AnalysisPanel from "../components/AnalysisPanel";
import { FIELD_LABELS, labelOf } from "../labels";
import type { LectureProfile, OptionSchema, Project } from "../types";

type Form = Record<string, string | number | null>;

const CHOICE_BASIC = [
  "audience_level",
  "difficulty",
  "lecture_type",
  "explanation_depth",
  "source_policy",
];

export default function OptionsPage() {
  const { projectId } = useParams();
  const [schema, setSchema] = useState<OptionSchema | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [form, setForm] = useState<Form>({});
  const [profile, setProfile] = useState<LectureProfile | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<{ message: string; details: string[] } | null>(null);
  const [saving, setSaving] = useState(false);
  const [intros, setIntros] = useState<VideoIntroItem[]>([]);
  const [introBusy, setIntroBusy] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    (async () => {
      try {
        const [s, p, introList] = await Promise.all([
          api.getOptionSchema(),
          api.getProject(projectId),
          api.listIntros(),
        ]);
        setSchema(s);
        setProject(p);
        setIntros(introList.items);
        const initial: Form = {};
        for (const f of s.basic) initial[f] = null;
        for (const f of s.advanced) initial[f] = null;
        initial.duration_minutes = 60;
        initial.video_intro_file = null;
        // Restore a previously saved profile (advanced fields that were
        // inferred stay "auto" so the server keeps inferring them).
        const saved = p.lecture_profile;
        if (saved) {
          for (const f of s.basic) initial[f] = (saved as never)[f];
          for (const f of s.advanced) {
            initial[f] = saved.inferred_fields.includes(f) ? null : (saved as never)[f];
          }
          initial.video_intro_file = saved.video_intro_file ?? null;
          setProfile(saved);
        }
        setForm(initial);
      } catch (err) {
        setLoadError(err instanceof ApiError ? err.message : "불러오지 못했습니다.");
      }
    })();
  }, [projectId]);

  const set = (field: string, value: string | number | null) =>
    setForm((f) => ({ ...f, [field]: value }));

  async function uploadIntro(file: File | null, input: HTMLInputElement) {
    if (!file) return;
    setIntroBusy(true);
    setError(null);
    try {
      const result = await api.uploadIntro(file);
      setIntros(result.items);
      set("video_intro_file", result.filename);
    } catch (err) {
      if (err instanceof ApiError) setError({ message: err.message, details: err.details });
      else setError({ message: "인트로 영상을 올리지 못했습니다.", details: [] });
    } finally {
      input.value = "";
      setIntroBusy(false);
    }
  }

  const basicComplete =
    schema !== null &&
    CHOICE_BASIC.every((f) => form[f]) &&
    typeof form.duration_minutes === "number" &&
    form.duration_minutes >= schema.duration_minutes.min &&
    form.duration_minutes <= schema.duration_minutes.max;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!projectId || !schema) return;
    setSaving(true);
    setError(null);
    try {
      const payload = { ...form, video_intro_file: form.video_intro_file || null };
      const saved = await api.saveProfile(projectId, payload as never);
      setProfile(saved);
    } catch (err) {
      if (err instanceof ApiError) setError({ message: err.message, details: err.details });
      else setError({ message: "알 수 없는 오류가 발생했습니다.", details: [] });
    } finally {
      setSaving(false);
    }
  }

  if (loadError) return <p className="error">{loadError}</p>;
  if (!schema || !project) return <p>불러오는 중...</p>;

  const select = (field: string, allowAuto: boolean) => (
    <label className="field" key={field}>
      <span>{FIELD_LABELS[field] ?? field}</span>
      <select
        value={(form[field] as string | null) ?? ""}
        onChange={(e) => set(field, e.target.value || null)}
      >
        <option value="">{allowAuto ? "자동 (추천값 사용)" : "선택하세요"}</option>
        {schema.values[field].map((v) => (
          <option key={v} value={v}>
            {labelOf(field, v)}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <>
    <div className="two-col">
      <section className="card">
        <h2>2. 강의 옵션 선택</h2>
        <p className="hint">
          자료: <strong>{project.source_file?.filename ?? "(업로드된 파일 없음)"}</strong>
          {project.title ? ` · 제목: ${project.title}` : ""}
          {project.input_mode === "deck"
            ? " · 업로드한 PPT를 그대로 사용합니다. 강의 시간과 영상 인트로는 아래에서 정합니다."
            : ""}
        </p>
        <form onSubmit={save}>
          <h3>기본 옵션</h3>
          {select("audience_level", false)}

          <label className="field">
            <span>{FIELD_LABELS.duration_minutes}</span>
            <div className="duration">
              <input
                type="number"
                min={schema.duration_minutes.min}
                max={schema.duration_minutes.max}
                value={(form.duration_minutes as number | null) ?? ""}
                onChange={(e) =>
                  set("duration_minutes", e.target.value === "" ? null : Number(e.target.value))
                }
              />
              {schema.duration_minutes.recommended.map((m) => (
                <button
                  type="button"
                  key={m}
                  className={form.duration_minutes === m ? "chip active" : "chip"}
                  onClick={() => set("duration_minutes", m)}
                >
                  {m}분
                </button>
              ))}
            </div>
          </label>

          {select("difficulty", false)}
          {select("lecture_type", false)}
          {select("explanation_depth", false)}
          {select("source_policy", false)}

          <details className="advanced">
            <summary>고급 설정 (선택 · 비워두면 자동 추론)</summary>
            {schema.advanced.filter((f) => f !== "video_intro").map((f) => select(f, true))}
            {select("video_intro", true)}
            {form.video_intro === "include" && (
              <label className="field">
                <span>{FIELD_LABELS.video_intro_file}</span>
                <select
                  value={(form.video_intro_file as string | null) ?? ""}
                  onChange={(e) => set("video_intro_file", e.target.value || null)}
                >
                  <option value="">폴더에서 선택하세요</option>
                  {intros.map((item) => (
                    <option key={item.filename} value={item.filename}>
                      {item.filename}
                    </option>
                  ))}
                </select>
                <input
                  type="file"
                  accept="video/mp4,video/webm,video/quicktime,.mp4,.mov,.webm,.mkv,.m4v"
                  disabled={introBusy}
                  onChange={(e) => void uploadIntro(e.target.files?.[0] ?? null, e.target)}
                />
                <p className="hint">
                  서버 폴더 <code>data/intros</code>에 있는 영상을 고르거나, 파일을 올리면 그 폴더에 저장됩니다.
                </p>
              </label>
            )}
          </details>

          {error && (
            <div className="error">
              <p>{error.message}</p>
              {error.details.length > 0 && (
                <ul>
                  {error.details.map((d) => (
                    <li key={d}>{d}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <button
            type="submit"
            disabled={!basicComplete || saving || (form.video_intro === "include" && !form.video_intro_file)}
          >
            {saving ? "저장 중..." : "강의 프로필 저장"}
          </button>
        </form>
      </section>

      <section className="card">
        <h2>저장된 LectureProfile</h2>
        {profile ? (
          <>
            <p className="ok">서버에 저장되었습니다. (id: {profile.id.slice(0, 8)}…)</p>
            <p>
              <Link className="btn" to={`/projects/${projectId}/preview`} data-testid="go-preview">
                강의 구조 미리보기 →
              </Link>
            </p>
            <table className="profile">
              <tbody>
                {[...schema.basic, ...schema.advanced].map((f) => {
                  const v = (profile as never)[f] as string | number;
                  return (
                    <tr key={f}>
                      <th>{FIELD_LABELS[f] ?? f}</th>
                      <td>
                        {typeof v === "number" ? `${v}분` : labelOf(f, v)}
                        {profile.inferred_fields.includes(f) && (
                          <span className="badge">자동</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {profile.video_intro === "include" && (
                  <tr>
                    <th>{FIELD_LABELS.video_intro_file}</th>
                    <td>{profile.video_intro_file ?? "선택되지 않음"}</td>
                  </tr>
                )}
              </tbody>
            </table>
            <details>
              <summary>JSON 보기</summary>
              <pre>{JSON.stringify(profile, null, 2)}</pre>
            </details>
          </>
        ) : (
          <p className="hint">기본 옵션 6개를 선택하고 저장하면 서버가 생성한 LectureProfile이 여기에 표시됩니다.</p>
        )}
      </section>
    </div>
    {project.source_analysis ? (
      <AnalysisPanel analysis={project.source_analysis} />
    ) : (
      <section className="card">
        <h2>자료 분석 결과</h2>
        <p className="error">
          {project.error_message ?? "아직 분석되지 않았습니다."} 첫 화면에서 자료를 다시 업로드해 주세요.
        </p>
      </section>
    )}
    </>
  );
}
