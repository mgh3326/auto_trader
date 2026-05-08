import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";

export function DesktopHomePage() {
  const panel = useAccountPanel();
  return (
    <DesktopShell
      center={
        <section style={{ padding: 24, borderRadius: 12, background: "var(--surface, #15181f)" }}>
          <h1 style={{ fontSize: 18, marginTop: 0 }}>/invest 데스크톱 (read-only)</h1>
          <p style={{ color: "#9ba0ab", fontSize: 13 }}>
            상단 네비게이션에서 뉴스, 시그널, 캘린더로 이동하세요.
          </p>
        </section>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
