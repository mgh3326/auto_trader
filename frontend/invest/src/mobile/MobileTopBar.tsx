import { Icon } from "../ds";

export function MobileTopBar({ title }: { title: string }) {
  return (
    <div
      data-testid="mobile-top-bar"
      style={{
        height: 48,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 16px",
        borderBottom: "1px solid var(--divider)",
        background: "var(--surface)",
        flexShrink: 0,
        position: "sticky",
        top: 0,
        zIndex: 5,
      }}
    >
      <div style={{ fontSize: 16, fontWeight: 700, letterSpacing: "-0.02em" }}>{title}</div>
      <div aria-hidden style={{ display: "flex", gap: 6, color: "var(--fg-3)" }}>
        <Icon name="search" size={18} />
        <Icon name="bell" size={18} />
      </div>
    </div>
  );
}
