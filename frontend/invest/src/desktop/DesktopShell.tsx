import type { ReactNode } from "react";
import { DesktopHeader } from "./DesktopHeader";

export function DesktopShell({
  left, center, right,
}: { left?: ReactNode; center: ReactNode; right: ReactNode }) {
  return (
    <div data-testid="desktop-shell" style={{ minHeight: "100vh", background: "var(--bg, #0e1014)", color: "var(--text, #e8eaf0)" }}>
      <DesktopHeader />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: left ? "240px minmax(0,1fr) 320px" : "minmax(0,1fr) 320px",
          gap: 24,
          padding: "24px 32px",
          maxWidth: 1440,
          margin: "0 auto",
        }}
      >
        {left ? <aside style={{ minWidth: 0 }}>{left}</aside> : null}
        <main style={{ minWidth: 0 }}>{center}</main>
        <aside style={{ position: "sticky", top: 24, alignSelf: "start", maxHeight: "calc(100vh - 48px)", overflowY: "auto" }}>
          {right}
        </aside>
      </div>
    </div>
  );
}
