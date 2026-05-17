import type { ReactNode } from "react";
import { DesktopHeader } from "./DesktopHeader";
import { RightRemotePanel } from "./RightRemotePanel";
import { useRightRailCollapsed } from "./useRightRailCollapsed";

const RAIL_WIDTH_EXPANDED = "320px";
const RAIL_WIDTH_COLLAPSED = "56px";

export function DesktopShell({
  left,
  center,
  leftColumnWidth = 220,
}: {
  left?: ReactNode;
  center: ReactNode;
  leftColumnWidth?: number | string;
}) {
  const resolvedLeftColumnWidth =
    typeof leftColumnWidth === "number" ? `${leftColumnWidth}px` : leftColumnWidth;
  const { collapsed, setCollapsed } = useRightRailCollapsed();
  const railWidth = collapsed ? RAIL_WIDTH_COLLAPSED : RAIL_WIDTH_EXPANDED;

  return (
    <div
      data-testid="desktop-shell"
      data-rail-collapsed={collapsed ? "true" : "false"}
      style={{ minHeight: "100vh", background: "var(--bg-alt)", color: "var(--fg)" }}
    >
      <DesktopHeader />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: left
            ? `${resolvedLeftColumnWidth} minmax(0,1fr) ${railWidth}`
            : `minmax(0,1fr) ${railWidth}`,
          gap: 24,
          padding: "24px 28px 64px",
          maxWidth: 1440,
          margin: "0 auto",
          transition: "grid-template-columns var(--dur-slow, 240ms) var(--ease-emph, cubic-bezier(0.2, 0.8, 0.2, 1.0))",
        }}
      >
        {left ? <aside style={{ minWidth: 0 }}>{left}</aside> : null}
        <main style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>{center}</main>
        <aside
          data-testid="desktop-shell-rail"
          style={{
            position: "sticky",
            top: 80,
            alignSelf: "start",
            maxHeight: "calc(100vh - 96px)",
            overflowY: "auto",
            minWidth: 0,
          }}
        >
          <RightRemotePanel collapsed={collapsed} onCollapseChange={setCollapsed} />
        </aside>
      </div>
    </div>
  );
}
