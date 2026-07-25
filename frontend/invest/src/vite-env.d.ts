/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_LINEAR_WORKSPACE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
