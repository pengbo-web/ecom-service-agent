export type SSEEvent = Record<string, any> & { type: string };

/** 把累积字符串按 \n\n 拆成事件；返回解析出的事件与未完成的余量。 */
export function splitSSEFrames(buffer: string): { events: SSEEvent[]; rest: string } {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const events: SSEEvent[] = [];
  for (const frame of parts) {
    const line = frame.trim();
    if (!line.startsWith("data:")) continue;
    try {
      events.push(JSON.parse(line.slice(line.indexOf(":") + 1).trim()));
    } catch {
      /* 忽略坏帧 */
    }
  }
  return { events, rest };
}
