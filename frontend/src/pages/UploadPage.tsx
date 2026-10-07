import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { isUiPreview } from "../preview/mode";

const SOURCE_ACCEPT = ".pdf,.ppt,.pptx,.docx,.txt,.md,.markdown";

export default function UploadPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"source" | "deck">("source");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<"upload" | "analyze">("upload");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    setStage("upload");
    try {
      const project = await api.createProject(title.trim() || undefined);
      if (mode === "deck") {
        await api.importDeck(project.id, file);
        navigate(`/projects/${project.id}/options`);
        return;
      }
      await api.uploadSource(project.id, file);
      setStage("analyze");
      await api.analyzeProject(project.id);
      navigate(`/projects/${project.id}/options`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "알 수 없는 오류가 발생했습니다.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>1. 시작하기</h2>
      <div className="mode-tabs" role="tablist" aria-label="업로드 방식">
        <button
          type="button"
          role="tab"
          className={mode === "source" ? "chip active" : "chip"}
          aria-selected={mode === "source"}
          onClick={() => {
            setMode("source");
            setFile(null);
            setError(null);
          }}
        >
          강의자료로 만들기
        </button>
        <button
          type="button"
          role="tab"
          className={mode === "deck" ? "chip active" : "chip"}
          aria-selected={mode === "deck"}
          onClick={() => {
            setMode("deck");
            setFile(null);
            setError(null);
          }}
        >
          만든 PPT로 대본·영상
        </button>
      </div>
      <p className="hint">
        {mode === "deck"
          ? "이미 만든 PPTX를 올리면 옵션을 고른 뒤, 그 슬라이드 순서대로 대본을 쓰고 강의 영상을 만듭니다. 슬라이드는 다시 그리지 않습니다."
          : "PDF, PPT, PPTX, DOCX, TXT, Markdown 파일을 업로드할 수 있습니다."}
      </p>
      <form onSubmit={submit}>
        <label className="field">
          <span>강의 제목 (선택)</span>
          <input
            type="text"
            value={title}
            placeholder="비워두면 파일명이 사용됩니다"
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label className="field">
          <span>{mode === "deck" ? "PPTX 파일" : "강의자료 파일"}</span>
          <input
            type="file"
            accept={mode === "deck" ? ".pptx" : SOURCE_ACCEPT}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        {file && (
          <p className="hint">
            선택됨: {file.name} ({(file.size / 1024).toFixed(1)} KB)
          </p>
        )}
        {isUiPreview && (
          <p className="hint">
            미리보기에서는 아무 파일이나 골라도 되고, 아래 버튼으로 샘플만 보고 넘어갈 수도 있습니다.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        {isUiPreview && (
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => {
              const sample = new File(["샘플 강의 자료"], mode === "deck" ? "sample.pptx" : "sample.md", {
                type: mode === "deck" ? "application/vnd.openxmlformats-officedocument.presentationml.presentation" : "text/markdown",
              });
              setFile(sample);
            }}
          >
            샘플 파일 넣기
          </button>
        )}
        <button type="submit" disabled={!file || busy}>
          {busy
            ? stage === "upload"
              ? "업로드 중..."
              : "자료를 읽는 중..."
            : mode === "deck"
              ? "PPT를 올리고 옵션 선택으로 이동"
              : "업로드·분석하고 옵션 선택으로 이동"}
        </button>
      </form>
    </section>
  );
}
