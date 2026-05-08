import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/", label: "홈", end: true },
  { to: "/feed/news", label: "뉴스" },
  { to: "/signals", label: "시그널" },
  { to: "/calendar", label: "캘린더" },
];

export function DesktopHeader() {
  return (
    <header style={{ display: "flex", gap: 24, padding: "12px 32px", borderBottom: "1px solid var(--surface-2, #1c1e24)" }}>
      <div style={{ fontWeight: 700, fontSize: 16 }}>auto_trader</div>
      <nav style={{ display: "flex", gap: 16 }}>
        {LINKS.map((l) => (
          <NavLink
            key={l.to} to={l.to} end={l.end}
            style={({ isActive }) => ({
              color: isActive ? "#fff" : "#9ba0ab",
              textDecoration: "none", fontSize: 14, padding: "4px 8px",
            })}
          >
            {l.label}
          </NavLink>
        ))}
      </nav>
    </header>
  );
}
