import type { ReactNode } from "react";
import { DesktopHeader } from "./DesktopHeader";

export function DesktopShell({
  left,
  center,
  right,
}: {
  left?: ReactNode;
  center: ReactNode;
  right: ReactNode;
}) {
  return (
    <div data-testid="desktop-shell" style={{ minHeight: "100vh", background: "var(--bg-alt)", color: "var(--fg)" }}>
      <DesktopHeader />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: left ? "220px minmax(0,1fr) 320px" : "minmax(0,1fr) 320px",
          gap: 24,
          padding: "24px 28px 64px",
          maxWidth: 1440,
          margin: "0 auto",
        }}
      >
        {left ? <aside style={{ minWidth: 0 }}>{left}</aside> : null}
        <main style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>{center}</main>
        <aside
          style={{
            position: "sticky",
            top: 80,
            alignSelf: "start",
            maxHeight: "calc(100vh - 96px)",
            overflowY: "auto",
          }}
        >
          {right}
        </aside>
      </div>
    </div>
  );
}
