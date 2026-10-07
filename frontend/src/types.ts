// Mirrors backend/app/models/*.py. Allowed option *values* are fetched from
// GET /lecture-options at runtime, so they are not duplicated here.

export interface SourceFileInfo {
  filename: string;
  extension: string;
  size_bytes: number;
  content_type: string | null;
  uploaded_at: string;
}

export const BASIC_FIELDS = [
  "audience_level",
  "duration_minutes",
  "difficulty",
  "lecture_type",
  "explanation_depth",
  "source_policy",
] as const;

export const ADVANCED_FIELDS = [
  "slide_density",
  "visual_level",
  "example_level",
  "practice_level",
  "code_level",
  "quiz_mode",
  "lecture_tone",
  "speaker_notes",
  "video_intro",
] as const;

export type BasicField = (typeof BASIC_FIELDS)[number];
export type AdvancedField = (typeof ADVANCED_FIELDS)[number];

export interface LectureProfile {
  id: string;
  project_id: string;
  audience_level: string;
  duration_minutes: number;
  difficulty: string;
  lecture_type: string;
  explanation_depth: string;
  source_policy: string;
  slide_density: string;
  visual_level: string;
  example_level: string;
  practice_level: string;
  code_level: string;
  quiz_mode: string;
  lecture_tone: string;
  speaker_notes: string;
  video_intro?: string;
  video_intro_file?: string | null;
  inferred_fields: string[];
  created_at: string;
  updated_at: string;
}

export interface SourceLocation {
  section_id: string | null;
  section_title: string | null;
  line: number | null;
  page: number | null;
}

export interface Concept {
  name: string;
  aliases: string[];
  description: string;
  importance: number;
  prerequisite: string[];
  source_location: SourceLocation;
  category: string;
  mention_count: number;
}

export interface MainTopic {
  title: string;
  summary: string;
  section_ids: string[];
  key_concepts: string[];
}

export interface SourceAnalysis {
  source_id: string;
  title: string;
  summary: string;
  main_topics: MainTopic[];
  concepts: Concept[];
  definitions: { term: string; definition: string; source_location: SourceLocation }[];
  examples: { kind: string; text: string; related_concepts: string[] }[];
  important_points: { text: string; reason: string }[];
  scope_notes: { text: string; source_location: SourceLocation }[];
  sections: { id: string; title: string; level: number }[];
  estimated_complexity: { level: string; score: number };
  warnings: string[];
}

export interface Project {
  id: string;
  title: string | null;
  presentation_status: string | null;
  error_message: string | null;
  source_file: SourceFileInfo | null;
  has_analysis: boolean;
  source_analysis: SourceAnalysis | null;
  lecture_profile: LectureProfile | null;
  has_plan: boolean;
  has_slides: boolean;
  has_enrichment?: boolean;
  has_prompt?: boolean;
  has_presentation?: boolean;
  has_video?: boolean;
  input_mode?: string;
  presentation_provider?: string | null;
  created_at: string;
  updated_at: string;
}

export interface OptionSchema {
  basic: string[];
  advanced: string[];
  duration_minutes: { min: number; max: number; recommended: number[] };
  values: Record<string, string[]>;
}

export interface VideoIntroItem {
  filename: string;
  name: string;
  size_bytes: number;
}

export interface VideoStatus {
  state: string;
  progress: number;
  stage: string | null;
  message: string | null;
  has_result: boolean;
  duration_seconds: number | null;
  slide_count: number | null;
}

export interface PresentationStatus {
  state: string;
  project_status: string | null;
  provider: string;
  is_mock: boolean;
  provider_configured: boolean;
  provider_message: string | null;
  progress: number;
  stage: string | null;
  message: string | null;
  has_result: boolean;
}

export interface LectureOptionsInput {
  audience_level: string;
  duration_minutes: number;
  difficulty: string;
  lecture_type: string;
  explanation_depth: string;
  source_policy: string;
  // advanced: null => let the server infer
  [key: string]: string | number | null;
}


// ---------------------------------------------------------------------------
// STAGE 3 LecturePlan / STAGE 4 SlideSpecification (mirrors backend/app/models)
// ---------------------------------------------------------------------------
export interface PlanMetrics {
  section_count: number;
  slide_count: number;
  concept_count: number;
  definition_count: number;
  example_count: number;
  practice_activity_count: number;
  practice_step_count: number;
  code_count: number;
  quiz_question_count: number;
  explanation_minutes: number;
  practice_minutes: number;
  minutes_by_kind: Record<string, number>;
}

export interface LectureSection {
  id: string;
  order: number;
  title: string;
  purpose: string;
  kind: string;
  duration_minutes: number;
  importance: number;
  concepts: string[];
  estimated_slides: number;
  teaching_notes: string[];
}

export interface LecturePlan {
  id: string;
  project_id: string;
  title: string;
  audience: string;
  duration_minutes: number;
  difficulty: string;
  lecture_type: string;
  explanation_depth: string;
  source_policy: string;
  learning_objectives: string[];
  estimated_slide_count: number;
  sections: LectureSection[];
  metrics: PlanMetrics;
  warnings: string[];
  generated_at: string;
}

export interface Slide {
  slide_number: number;
  section_id: string;
  section_title: string;
  title: string;
  slide_type: string;
  learning_purpose: string;
  key_message: string;
  message_from_source: boolean;
  key_points: string[];
  source_reference: SourceLocation | null;
  visual_instruction: string;
  presenter_instruction: string;
  estimated_explanation_time: number;
  concepts: string[];
  content_origin: "source" | "structural" | "suggested";
}

export interface SlideSpecification {
  lecture_id: string;
  project_id: string;
  title: string;
  duration_minutes: number;
  slide_count: number;
  plan_estimated_slide_count: number;
  slides: Slide[];
  warnings: string[];
  generated_at: string;
}

export interface EnrichedSlide {
  slide_number: number;
  enriched: { presenter_notes: string | null } | null;
}

export interface EnrichedSlideSpecification {
  slides: EnrichedSlide[];
}

export interface SlideScript {
  slide_number: number;
  title: string;
  slide_type: string;
  estimated_seconds: number;
  spoken_seconds: number;
  text: string;
  source: string;
}

export interface LectureScript {
  project_id: string;
  title: string;
  slide_count: number;
  total_seconds: number;
  slides: SlideScript[];
  writer?: string;
  model_name?: string | null;
}
