import { ApiError } from "../apiError";
import type {
  LectureOptionsInput,
  LectureProfile,
  Project,
  VideoIntroItem,
  VideoStatus,
  PresentationStatus,
} from "../types";
import {
  OPTION_SCHEMA,
  SAMPLE_ANALYSIS,
  SAMPLE_INTROS,
  demoProject,
  emptyProject,
  samplePlan,
  sampleProfile,
  sampleScripts,
  sampleSlides,
} from "./fixtures";

const STORE_KEY = "ailecturegen-ui-preview";

type Store = {
  projects: Record<string, Project>;
  intros: VideoIntroItem[];
};

function now(): string {
  return new Date().toISOString();
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function load(): Store {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    if (raw) return JSON.parse(raw) as Store;
  } catch {
    /* ignore broken session data */
  }
  const stamp = now();
  const seed = demoProject(stamp);
  return { projects: { [seed.id]: seed }, intros: [...SAMPLE_INTROS] };
}

function save(store: Store): void {
  sessionStorage.setItem(STORE_KEY, JSON.stringify(store));
}

function project(store: Store, id: string): Project {
  const found = store.projects[id];
  if (found) return found;
  if (id === "preview-demo") {
    store.projects[id] = demoProject(now());
    save(store);
    return store.projects[id];
  }
  throw new ApiError("해당 강의 프로젝트를 찾을 수 없습니다.", "ProjectNotFound");
}

function touch(p: Project): Project {
  return { ...p, updated_at: now() };
}

function inferProfile(projectId: string, options: LectureOptionsInput, existing: LectureProfile | null): LectureProfile {
  const stamp = now();
  const practice =
    options.practice_level ??
    (options.lecture_type === "practice" ? "full" : options.lecture_type === "mixed" ? "guided" : "none");
  const inferred: string[] = [];
  const pick = (name: string, value: string | number | null | undefined, fallback: string) => {
    if (value === null || value === undefined || value === "") {
      inferred.push(name);
      return fallback;
    }
    return String(value);
  };
  return {
    id: existing?.id ?? "preview-profile",
    project_id: projectId,
    audience_level: String(options.audience_level),
    duration_minutes: Number(options.duration_minutes),
    difficulty: String(options.difficulty),
    lecture_type: String(options.lecture_type),
    explanation_depth: String(options.explanation_depth),
    source_policy: String(options.source_policy),
    slide_density: pick("slide_density", options.slide_density, "normal"),
    visual_level: pick("visual_level", options.visual_level, "medium"),
    example_level: pick("example_level", options.example_level, "medium"),
    practice_level: pick("practice_level", options.practice_level, String(practice)),
    code_level: pick("code_level", options.code_level, "none"),
    quiz_mode: pick("quiz_mode", options.quiz_mode, "none"),
    lecture_tone: pick("lecture_tone", options.lecture_tone, "academic"),
    speaker_notes: pick("speaker_notes", options.speaker_notes, "concise"),
    video_intro: pick("video_intro", options.video_intro, "none"),
    video_intro_file: options.video_intro_file ? String(options.video_intro_file) : null,
    inferred_fields: inferred,
    created_at: existing?.created_at ?? stamp,
    updated_at: stamp,
  };
}

function downloadPlaceholder(name: string, body: string): void {
  const url = URL.createObjectURL(new Blob([body], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function presentationStatus(p: Project): PresentationStatus {
  const done = Boolean(p.has_presentation);
  return {
    state: done ? "completed" : "not_started",
    project_status: p.presentation_status,
    provider: "ui-preview",
    is_mock: true,
    provider_configured: true,
    provider_message: "UI 미리보기에서는 파일을 만들지 않습니다.",
    progress: done ? 100 : 0,
    stage: done ? "완료" : null,
    message: null,
    has_result: done,
  };
}

function videoStatus(p: Project): VideoStatus {
  const done = Boolean(p.has_video);
  return {
    state: done ? "completed" : "not_started",
    progress: done ? 100 : 0,
    stage: done ? "완료" : null,
    message: null,
    has_result: done,
    duration_seconds: done ? 96 : null,
    slide_count: done ? 6 : null,
  };
}

export const mockApi = {
  getOptionSchema: async () => OPTION_SCHEMA,

  createProject: async (title?: string) => {
    const store = load();
    const id = `preview${Math.random().toString(16).slice(2, 10)}`;
    const created = emptyProject(id, title?.trim() || null, now());
    store.projects[id] = created;
    save(store);
    return created;
  },

  getProject: async (id: string) => project(load(), id),

  listProjects: async () => Object.values(load().projects),

  uploadSource: async (id: string, file: File) => {
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({
      ...p,
      title: p.title || file.name.replace(/\.[^.]+$/, ""),
      presentation_status: "uploaded",
      source_file: {
        filename: file.name,
        extension: file.name.includes(".") ? `.${file.name.split(".").pop()}` : "",
        size_bytes: file.size,
        content_type: file.type || null,
        uploaded_at: now(),
      },
      input_mode: "source",
    });
    save(store);
    return store.projects[id];
  },

  importDeck: async (id: string, file: File) => {
    await wait(250);
    const store = load();
    const p = project(store, id);
    const stamp = now();
    store.projects[id] = touch({
      ...p,
      title: p.title || file.name.replace(/\.[^.]+$/, ""),
      presentation_status: "completed",
      source_file: {
        filename: file.name,
        extension: ".pptx",
        size_bytes: file.size,
        content_type: file.type || null,
        uploaded_at: stamp,
      },
      source_analysis: SAMPLE_ANALYSIS,
      has_analysis: true,
      lecture_profile: sampleProfile(id, stamp),
      has_plan: true,
      has_slides: true,
      has_presentation: true,
      input_mode: "deck",
    });
    save(store);
    return store.projects[id];
  },

  listIntros: async () => ({ folder: "intros", items: load().intros }),

  uploadIntro: async (file: File) => {
    const store = load();
    const item: VideoIntroItem = {
      filename: file.name,
      name: file.name.replace(/\.[^.]+$/, ""),
      size_bytes: file.size,
    };
    if (!store.intros.some((x) => x.filename === item.filename)) store.intros.push(item);
    save(store);
    return { filename: item.filename, folder: "intros", items: store.intros };
  },

  analyzeProject: async (id: string) => {
    await wait(350);
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({
      ...p,
      presentation_status: "analyzed",
      source_analysis: SAMPLE_ANALYSIS,
      has_analysis: true,
    });
    save(store);
    return store.projects[id];
  },

  saveProfile: async (id: string, options: LectureOptionsInput) => {
    const store = load();
    const p = project(store, id);
    const profile = inferProfile(id, options, p.lecture_profile);
    store.projects[id] = touch({
      ...p,
      lecture_profile: profile,
      has_plan: p.input_mode === "deck" ? true : false,
      has_slides: p.input_mode === "deck" ? true : false,
      has_presentation: p.input_mode === "deck" ? p.has_presentation : false,
      has_video: false,
      presentation_status: p.input_mode === "deck" ? "completed" : "analyzed",
    });
    save(store);
    return profile;
  },

  createPlan: async (id: string) => {
    await wait(300);
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({ ...p, has_plan: true, presentation_status: "planned" });
    save(store);
    return samplePlan(id, now());
  },

  getPlan: async (id: string) => samplePlan(id, now()),

  createSlides: async (id: string) => {
    await wait(250);
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({ ...p, has_slides: true });
    save(store);
    return sampleSlides(id, now());
  },

  getSlides: async (id: string) => sampleSlides(id, now()),

  getEnrichment: async (_id: string) => ({ slides: [] }),

  getScripts: async (id: string) => {
    await wait(200);
    return sampleScripts(id);
  },

  approvePlan: async (id: string) => {
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({ ...p, presentation_status: "ready_to_generate" });
    save(store);
    return store.projects[id];
  },

  buildPrompt: async () => ({ ok: true }),

  createPresentation: async (id: string) => {
    await wait(450);
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({
      ...p,
      has_presentation: true,
      has_prompt: true,
      presentation_status: "completed",
      presentation_provider: "ui-preview",
    });
    save(store);
    return presentationStatus(store.projects[id]);
  },

  presentationStatus: async (id: string) => presentationStatus(project(load(), id)),

  async downloadPresentation(_id: string) {
    downloadPlaceholder(
      "preview-lecture.txt",
      "UI 미리보기용 자리표시 파일입니다. 실제 PPT는 만들어지지 않습니다.\n"
    );
  },

  createVideo: async (id: string, _force = false) => {
    await wait(450);
    const store = load();
    const p = project(store, id);
    store.projects[id] = touch({ ...p, has_video: true });
    save(store);
    return videoStatus(store.projects[id]);
  },

  videoStatus: async (id: string) => videoStatus(project(load(), id)),

  async downloadVideo(_id: string) {
    downloadPlaceholder(
      "preview-lecture.txt",
      "UI 미리보기용 자리표시 파일입니다. 실제 영상은 만들어지지 않습니다.\n"
    );
  },
};

export type PreviewApi = typeof mockApi;
