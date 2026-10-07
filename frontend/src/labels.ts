// Display labels only. The set of valid values always comes from the backend;
// an unknown value simply falls back to its raw string.

export const FIELD_LABELS: Record<string, string> = {
  audience_level: "대상 수준",
  duration_minutes: "강의 시간(분)",
  difficulty: "난이도",
  lecture_type: "강의 유형",
  explanation_depth: "설명 깊이",
  source_policy: "자료 활용 정책",
  slide_density: "슬라이드 밀도",
  visual_level: "시각 자료 수준",
  example_level: "예제 수준",
  practice_level: "실습 수준",
  code_level: "코드 수준",
  quiz_mode: "퀴즈 모드",
  lecture_tone: "강의 어조",
  speaker_notes: "발표자 노트",
  video_intro: "강의 영상 인트로",
  video_intro_file: "인트로 영상",
};

export const VALUE_LABELS: Record<string, Record<string, string>> = {
  audience_level: {
    general: "일반",
    high_school: "고등학생",
    university_beginner: "대학 초급",
    university_intermediate: "대학 중급",
    university_advanced: "대학 고급",
    graduate: "대학원",
    professional: "실무 전문가",
  },
  difficulty: {
    introductory: "입문",
    beginner: "초급",
    intermediate: "중급",
    advanced: "고급",
  },
  lecture_type: {
    theory: "이론 중심",
    example_based: "예제 중심",
    practice: "실습 중심",
    mixed: "이론+실습 혼합",
    exam_preparation: "시험 대비",
  },
  explanation_depth: {
    concise: "간결",
    standard: "표준",
    detailed: "상세",
  },
  source_policy: {
    source_only: "원본만 사용",
    source_first: "원본 우선 (필요한 설명만 추가)",
    expanded: "확장 (외부 지식·사례 허용)",
  },
  slide_density: { concise: "간결", normal: "보통", detailed: "상세" },
  visual_level: { low: "낮음", medium: "보통", high: "높음" },
  example_level: { none: "없음", low: "적게", medium: "보통", high: "많이" },
  practice_level: {
    none: "없음",
    simple: "간단",
    guided: "안내형",
    full: "전체 실습",
  },
  code_level: { none: "없음", snippet: "코드 조각", executable: "실행 가능 코드" },
  quiz_mode: {
    none: "없음",
    checkpoint: "중간 점검",
    final: "마무리 퀴즈",
    both: "중간+마무리",
  },
  lecture_tone: {
    academic: "학술적",
    professional: "전문적",
    conversational: "대화체",
  },
  speaker_notes: { none: "없음", concise: "간단", full: "상세" },
  video_intro: { none: "넣지 않음", include: "앞에 넣기" },
};

// Section kinds (LecturePlan) and slide types (SlideSpecification)
export const KIND_LABELS: Record<string, string> = {
  intro: "도입",
  prerequisite: "선수 지식",
  concept: "개념 설명",
  comparison: "비교 정리",
  caution: "한계·주의",
  practice: "실습",
  quiz: "퀴즈",
  summary: "정리",
};

export const SLIDE_TYPE_LABELS: Record<string, string> = {
  title: "제목",
  agenda: "목차",
  concept: "개념",
  definition: "정의",
  comparison: "비교",
  diagram: "다이어그램",
  architecture: "구조도",
  workflow: "흐름도",
  example: "예제",
  practice: "실습",
  code: "코드",
  formula: "수식",
  quiz: "퀴즈",
  summary: "요약",
};

export const labelOf = (field: string, value: string) =>
  VALUE_LABELS[field]?.[value] ?? value;
