/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEMO_USER_NAME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
