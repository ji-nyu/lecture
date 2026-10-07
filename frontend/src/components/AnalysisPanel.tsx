import type { SourceAnalysis } from "../types";

const CATEGORY_LABELS: Record<string, string> = {
  definition: "정의",
  principle: "원리",
  architecture: "구조",
  process: "절차",
  example: "예시",
  practice: "실습",
  code: "코드",
  formula: "수식",
  caution: "주의",
  summary: "요약",
};

const LEVEL_LABELS: Record<string, string> = {
  introductory: "입문",
  beginner: "초급",
  intermediate: "중급",
  advanced: "고급",
};

/** Read-only view of the STAGE 2 SourceAnalysis (for instructor review). */
export default function AnalysisPanel({ analysis }: { analysis: SourceAnalysis }) {
  return (
    <section className="card analysis">
      <h2>자료 분석 결과</h2>
      <p className="hint">
        {analysis.title} · 섹션 {analysis.sections.length}개 · 개념 {analysis.concepts.length}개 ·
        자료 난이도 추정:{" "}
        <strong>{LEVEL_LABELS[analysis.estimated_complexity.level] ?? analysis.estimated_complexity.level}</strong>{" "}
        (규칙 기반 추정)
      </p>
      <p>{analysis.summary}</p>
      {analysis.warnings.map((w) => (
        <p className="error" key={w}>{w}</p>
      ))}

      <h3>주요 주제</h3>
      <ol className="topics">
        {analysis.main_topics.map((t) => (
          <li key={t.title + t.section_ids[0]}>
            <strong>{t.title}</strong>
            <div className="hint">{t.summary}</div>
          </li>
        ))}
      </ol>

      <details>
        <summary>핵심 개념 {analysis.concepts.length}개</summary>
        <table className="concepts">
          <thead>
            <tr><th>개념</th><th>분류</th><th>중요도</th><th>원문 설명</th></tr>
          </thead>
          <tbody>
            {analysis.concepts.map((c) => (
              <tr key={c.name}>
                <td>
                  <strong>{c.name}</strong>
                  {c.aliases.length > 0 && <div className="hint">{c.aliases.join(", ")}</div>}
                </td>
                <td>{CATEGORY_LABELS[c.category] ?? c.category}</td>
                <td>{"★".repeat(c.importance)}</td>
                <td>
                  {c.description}
                  <div className="hint">
                    § {c.source_location.section_title}
                    {c.prerequisite.length > 0 && ` · 선행: ${c.prerequisite.join(", ")}`}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>

      {analysis.scope_notes.length > 0 && (
        <details>
          <summary>원문의 강의 범위 지침 {analysis.scope_notes.length}건</summary>
          <ul>
            {analysis.scope_notes.map((n) => (
              <li key={n.text}>{n.text}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
