import type { Dict, Lang } from "./types";
import { en } from "./en";
import { zhCN } from "./zh-CN";

export type { Dict, Lang } from "./types";

const dictionaries: Record<Lang, Dict> = { en, "zh-CN": zhCN };

export function normalizeLang(value: string | undefined | null): Lang {
  const text = (value ?? "").trim().toLowerCase();
  // Accept both the BCP-47 region code ("zh-CN") and the script code
  // ("zh-Hans"). The backend persists ``zh-Hans`` after normalization while
  // the selector still emits ``zh-CN``; if either side falls out of sync the
  // GUI would silently revert to English, so we keep the matcher permissive
  // on both variants.
  if (
    text === "zh-cn" ||
    text === "zh" ||
    text === "zh_cn" ||
    text === "zh-hans" ||
    text === "zh_hans" ||
    text === "chinese"
  ) {
    return "zh-CN";
  }
  return "en";
}

export function translate(lang: Lang, key: string): string {
  return dictionaries[lang]?.[key] ?? dictionaries.en[key] ?? key;
}

export const SUPPORTED_LANGS: Lang[] = ["en", "zh-CN"];
