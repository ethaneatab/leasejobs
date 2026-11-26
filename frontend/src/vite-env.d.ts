/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_REPO_URL: string;
  readonly VITE_GIT_SHA: string;
  readonly VITE_API_KEY?: string;
}
declare module "*.md?raw" {
  const s: string;
  export default s;
}
