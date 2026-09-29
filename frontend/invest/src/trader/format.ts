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
