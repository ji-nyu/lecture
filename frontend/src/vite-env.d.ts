/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_UI_PREVIEW?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
