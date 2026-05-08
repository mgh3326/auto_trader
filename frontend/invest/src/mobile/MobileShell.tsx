import type { ReactNode } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { MobileBottomNav } from "./MobileBottomNav";

export function MobileShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div
      data-testid="mobile-shell"
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
        color: "var(--fg)",
      }}
    >
      <MobileTopBar title={title} />
      <div style={{ flex: 1, overflow: "auto" }}>{children}</div>
      <MobileBottomNav />
    </div>
  );
}
