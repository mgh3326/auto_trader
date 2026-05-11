import { NavLink } from "react-router-dom";
import { Icon } from "../ds";
import type { IconName } from "../ds";

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
}

const ITEMS: NavItem[] = [
  { to: "/", label: "홈", icon: "home", end: true },
  { to: "/my", label: "MY", icon: "person" },
  { to: "/feed/news", label: "뉴스", icon: "bell" },
  { to: "/discover", label: "발견", icon: "flash" },
  { to: "/calendar", label: "캘린더", icon: "calendar" },
];

export function MobileBottomNav() {
  return (
    <nav
      data-testid="mobile-bottom-nav"
      className="mobile-bottom-nav"
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${ITEMS.length}, 1fr)`,
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
