import { NavLink } from "react-router-dom";
import { Icon } from "../ds";
import { ThemeToggle } from "../theme/ThemeToggle";

const LINKS: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "홈", end: true },
  { to: "/my", label: "MY" },
  { to: "/feed/news", label: "뉴스" },
  { to: "/discover", label: "발견" },
  { to: "/calendar", label: "캘린더" },
  { to: "/market", label: "시장" },
  { to: "/coverage", label: "커버리지" },
  { to: "/insights", label: "인사이트" },
  { to: "/screener", label: "골라보기" },
  { to: "/scalping", label: "스캘핑 일지" },
];

export function DesktopHeader() {
  return (
    <header
      style={{
        display: "flex",
        alignItems: "center",
        gap: 28,
        padding: "0 28px",
        height: 56,
        background: "var(--surface)",
        borderBottom: "1px solid var(--divider)",
        position: "sticky",
        top: 0,
        zIndex: 5,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 17, letterSpacing: "-0.02em" }}>
        <span
          aria-hidden
          style={{
            width: 22,
            height: 22,
            borderRadius: 6,
            background: "var(--accent)",
            color: "var(--fg-on-accent)",
            display: "grid",
            placeItems: "center",
            fontSize: 12,
            fontWeight: 800,
          }}
        >
          A
        </span>
        auto_trader
      </div>

      <nav style={{ display: "flex", gap: 2, marginLeft: 8 }}>
        {LINKS.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            end={l.end}
            style={({ isActive }) => ({
              padding: "8px 14px",
              borderRadius: 8,
              textDecoration: "none",
              color: isActive ? "var(--fg)" : "var(--fg-2)",
              fontWeight: isActive ? 700 : 600,
              fontSize: 14,
              whiteSpace: "nowrap",
              flexShrink: 0,
              position: "relative",
              transition: "color 120ms cubic-bezier(0.2,0,0,1)",
            })}
          >
            {({ isActive }) => (
              <>
                {l.label}
                {isActive && (
                  <span
                    aria-hidden
                    style={{
                      position: "absolute",
                      left: 14,
                      right: 14,
                      bottom: -17,
                      height: 2,
                      background: "var(--fg)",
                      borderRadius: 2,
                    }}
                  />
                )}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div style={{ flex: 1 }} />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          background: "var(--surface-2)",
          borderRadius: 10,
          width: 280,
          color: "var(--fg-3)",
        }}
      >
        <Icon name="search" size={16} />
        <span style={{ fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flex: 1 }}>종목, 뉴스 검색…</span>
        <span
          style={{
            marginLeft: "auto",
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            padding: "1px 5px",
            borderRadius: 4,
            background: "var(--surface)",
            border: "1px solid var(--border)",
          }}
        >
          ⌘K
        </span>
      </div>

      <ThemeToggle variant="compact" />

      <button
        type="button"
        aria-label="알림"
        style={{
          width: 36,
          height: 36,
          borderRadius: 10,
          border: "none",
          background: "var(--surface-2)",
          cursor: "pointer",
          color: "var(--fg-1)",
          display: "grid",
          placeItems: "center",
        }}
      >
        <Icon name="bell" size={18} />
      </button>

      <div
        aria-hidden
        style={{
          width: 32,
          height: 32,
          borderRadius: 999,
          background: "var(--surface-3)",
          color: "var(--fg-1)",
          display: "grid",
          placeItems: "center",
          fontWeight: 700,
          fontSize: 13,
        }}
      >
        MG
      </div>
    </header>
  );
}
