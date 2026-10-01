export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtNum(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  return n.toLocaleString("ko-KR", { maximumFractionDigits: 8 });
}

export function fmtPct(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return `${value}%`;
}

/**
 * Client-clock deadline for a server-relative expiry, anchored to the moment
 * its request was SENT. The server sampled no earlier than that, so the result
 * is never later than the true deadline, without any client/server clock sync.
 * A missing expiry means the server has no valid_until: already expired.
 */
export function deadlineFrom(sentAt: number, expiresInSeconds: number | null): number {
  return expiresInSeconds === null ? sentAt : sentAt + expiresInSeconds * 1000;
}

export function secondsUntil(deadline: number | undefined, now: number): number | null {
  if (deadline === undefined) return null;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

export function formatCountdown(remaining: number | null): string {
  if (remaining === null) return "—";
  if (remaining <= 0) return "만료";
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const seconds = remaining % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`;
}
