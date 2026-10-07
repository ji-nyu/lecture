import { Link, Navigate, Route, Routes } from "react-router-dom";
import UploadPage from "./pages/UploadPage";
import OptionsPage from "./pages/OptionsPage";
import LecturePreviewPage from "./pages/LecturePreviewPage";
import { isUiPreview } from "./preview/mode";

export default function App() {
  return (
    <div className="app">
      {isUiPreview && (
        <div className="preview-banner" role="status">
          UI 미리보기 모드입니다. 서버에 연결하지 않으며, 강의 내용과 파일은 모두 샘플입니다.
          <Link to="/projects/preview-demo/preview">샘플 미리보기 바로 열기</Link>
        </div>
      )}
      <header className="app-header">
        <Link to="/" className="brand">
          AILectureGen
        </Link>
        <span className="tagline">
          {isUiPreview ? "UI 미리보기 · 디자인 확인용" : "교수자 주도형 AI 강의 생성 시스템"}
        </span>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/projects/:projectId/options" element={<OptionsPage />} />
          <Route path="/projects/:projectId/preview" element={<LecturePreviewPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
