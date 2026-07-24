import { open } from "@tauri-apps/plugin-dialog";

function isDesktop(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

export async function chooseRepository(currentPath: string): Promise<string | null | undefined> {
  if (!isDesktop()) return undefined;
  const selected = await open({
    directory: true,
    multiple: false,
    defaultPath: currentPath || undefined,
    title: "选择需要分析的代码仓库"
  });
  return typeof selected === "string" ? selected : null;
}
