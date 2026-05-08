import { NavLink } from "react-router-dom";
import { Icon } from "../ds";
import type { IconName } from "../ds";

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
}

// 발견 currently routes to the legacy /app/discover path. Stage 4.2 introduces
// a canonical /discover route and this entry switches over.
const ITEMS: NavItem[] = [
  { to: "/", label: "홈", icon: "home", end: true },
  { to: "/feed/news", label: "뉴스", icon: "bell" },
  { to: "/app/discover", label: "발견", icon: "flash" },
  { to: "/signals", label: "시그널", icon: "chart" },
  { to: "/calendar", label: "캘린더", icon: "calendar" },
];

export function MobileBottomNav() {
  return (
    <nav
      data-testid="mobile-bottom-nav"
      className="mobile-bottom-nav"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(5, 1fr)",
        borderTop: "1px solid var(--divider)",
        background: "var(--surface)",
        flexShrink: 0,
      }}
    >
      {ITEMS.map((it) => (
        <NavLink
          key={it.to}
          to={it.to}
          end={it.end}
          style={({ isActive }) => ({
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 2,
            background: "none",
            border: "none",
            padding: 6,
            textDecoration: "none",
            color: isActive ? "var(--fg)" : "var(--fg-3)",
            fontWeight: isActive ? 700 : 600,
          })}
        >
          <Icon name={it.icon} size={20} />
          <span style={{ fontSize: 10 }}>{it.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
