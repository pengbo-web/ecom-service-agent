export function avatarColor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 360;
  return `hsl(${h} 55% 55%)`;
}

// 千牛风头像:同一 seed 稳定的双色斜向渐变,更有质感。
export function avatarGradient(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 360;
  const h2 = (h + 38) % 360;
  return `linear-gradient(135deg, hsl(${h} 62% 60%), hsl(${h2} 64% 46%))`;
}

export function initials(name: string): string {
  const s = (name || "?").trim();
  if (/^[\x00-\x7f]+$/.test(s)) return s.slice(0, 2).toUpperCase();  // 英文/数字取两位
  return s.slice(0, 2);                                             // 中文取两字
}

export function relativeTime(iso: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`;
  return iso.slice(5, 16).replace("T", " ");
}

export function statusMeta(c: { status: string; manual: boolean }): {
  label: string; tone: "ai" | "manual" | "closed";
} {
  if (c.status !== "open") return { label: "已结束", tone: "closed" };
  if (c.manual) return { label: "人工中", tone: "manual" };
  return { label: "AI 接待", tone: "ai" };
}

export const TONE_CLASS: Record<"ai" | "manual" | "closed", string> = {
  ai: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  manual: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  closed: "bg-muted text-muted-foreground",
};
