import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/", label: "홈", end: true },
  { to: "/feed/news", label: "뉴스" },
  { to: "/signals", label: "시그널" },
  { to: "/calendar", label: "캘린더" },
  { to: "/screener", label: "골라보기" },
];

export function DesktopHeader() {
  return (
    <header style={{ display: "flex", gap: 24, padding: "12px 32px", borderBottom: "1px solid var(--divider)", background: "var(--surface)" }}>
      <div style={{ fontWeight: 700, fontSize: 16 }}>auto_trader</div>
      <nav style={{ display: "flex", gap: 16 }}>
        {LINKS.map((l) => (
          <NavLink
            key={l.to} to={l.to} end={l.end}
            style={({ isActive }) => ({
              color: isActive ? "var(--fg)" : "var(--fg-2)",
              textDecoration: "none", fontSize: 14, padding: "4px 8px",
              fontWeight: isActive ? 700 : 600,
            })}
          >
            {l.label}
          </NavLink>
        ))}
      </nav>
    </header>
  );
}
