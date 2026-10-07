import { ApiError } from "./apiError";
import { isUiPreview } from "./preview/mode";
import { mockApi } from "./preview/mockApi";
import type {
  EnrichedSlideSpecification,
  LectureScript,
  LectureOptionsInput,
  LecturePlan,
  LectureProfile,
  OptionSchema,
  PresentationStatus,
  Project,
  SlideSpecification,
  VideoIntroItem,
  VideoStatus,
} from "./types";

export { ApiError } from "./apiError";
export type { PresentationStatus, VideoIntroItem, VideoStatus } from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    throw new ApiError(
      "서버에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.",
      "NetworkError"
    );
  }
  if (!res.ok) {
    let message = "요청을 처리하지 못했습니다.";
    let code = "UnknownError";
    let details: string[] = [];
    try {
      const body = await res.json();
      if (body?.error) {
        message = body.error.message ?? message;
        code = body.error.code ?? code;
        details = body.error.details ?? [];
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message, code, details);
  }
  return (await res.json()) as T;
}

const liveApi = {
  getOptionSchema: () => request<OptionSchema>("/lecture-options"),
  createProject: (title?: string) =>
    request<Project>("/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(title ? { title } : {}),
    }),
  getProject: (id: string) => request<Project>(`/projects/${id}`),
  listProjects: () => request<Project[]>("/projects"),
  uploadSource: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Project>(`/projects/${id}/upload`, {
      method: "POST",
      body: form,
    });
  },
  importDeck: (id: string, file: File, durationMinutes = 20) => {
    const form = new FormData();
    form.append("file", file);
    form.append("duration_minutes", String(durationMinutes));
    return request<Project>(`/projects/${id}/deck`, {
      method: "POST",
      body: form,
    });
  },
  listIntros: () => request<{ folder: string; items: VideoIntroItem[] }>("/video-intros"),
  uploadIntro: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ filename: string; folder: string; items: VideoIntroItem[] }>("/video-intros", {
      method: "POST",
      body: form,
    });
  },
  analyzeProject: (id: string) =>
    request<Project>(`/projects/${id}/analyze`, { method: "POST" }),
  saveProfile: (id: string, options: LectureOptionsInput) =>
    request<LectureProfile>(`/projects/${id}/profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    }),
  createPlan: (id: string) => request<LecturePlan>(`/projects/${id}/plan`, { method: "POST" }),
  getPlan: (id: string) => request<LecturePlan>(`/projects/${id}/plan`),
  createSlides: (id: string) =>
    request<SlideSpecification>(`/projects/${id}/slides`, { method: "POST" }),
  getSlides: (id: string) => request<SlideSpecification>(`/projects/${id}/slides`),
  getEnrichment: (id: string) =>
    request<EnrichedSlideSpecification>(`/projects/${id}/enrichment`),
  getScripts: (id: string) => request<LectureScript>(`/projects/${id}/scripts`),
  approvePlan: (id: string) =>
    request<Project>(`/projects/${id}/plan/approve`, { method: "POST" }),
  buildPrompt: (id: string) =>
    request<unknown>(`/projects/${id}/prompt`, { method: "POST" }),
  createPresentation: (id: string) =>
    request<PresentationStatus>(`/projects/${id}/presentation`, { method: "POST" }),
  presentationStatus: (id: string) =>
    request<PresentationStatus>(`/projects/${id}/presentation/status`),
  async downloadPresentation(id: string) {
    let res: Response;
    try {
      res = await fetch(`${BASE}/projects/${id}/presentation/download`);
    } catch {
      throw new ApiError("서버에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.", "NetworkError");
    }
    if (!res.ok) {
      let message = "프레젠테이션을 받지 못했습니다.";
      let code = "UnknownError";
      try {
        const body = await res.json();
        if (body?.error) {
          message = body.error.message ?? message;
          code = body.error.code ?? code;
        }
      } catch {
        /* not JSON */
      }
      throw new ApiError(message, code);
    }
    const blob = await res.blob();
    let name = "presentation.pptx";
    const cd = res.headers.get("content-disposition") ?? "";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = cd.match(/filename="?([^";]+)"?/i);
    if (star) name = decodeURIComponent(star[1]);
    else if (plain) name = plain[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
  createVideo: (id: string, force = false) =>
    request<VideoStatus>(`/projects/${id}/video${force ? "?force=true" : ""}`, { method: "POST" }),
  videoStatus: (id: string) => request<VideoStatus>(`/projects/${id}/video/status`),
  async downloadVideo(id: string) {
    let res: Response;
    try {
      res = await fetch(`${BASE}/projects/${id}/video/download`);
    } catch {
      throw new ApiError("서버에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.", "NetworkError");
    }
    if (!res.ok) {
      let message = "영상을 받지 못했습니다.";
      try {
        const body = await res.json();
        if (body?.error) message = body.error.message ?? message;
      } catch {
        /* not JSON */
      }
      throw new ApiError(message, "UnknownError");
    }
    const blob = await res.blob();
    let name = "lecture.mp4";
    const cd = res.headers.get("content-disposition") ?? "";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = cd.match(/filename="?([^";]+)"?/i);
    if (star) name = decodeURIComponent(star[1]);
    else if (plain) name = plain[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

export const api = isUiPreview ? mockApi : liveApi;
