// frontend/invest/src/components/BottomNav.tsx
import type { CSSProperties } from "react";
import { NavLink } from "react-router-dom";

const ROW_STYLE: CSSProperties = {
  display: "flex",
  justifyContent: "space-around",
  paddingTop: 8,
  borderTop: "1px solid var(--surface-2)",
  color: "var(--muted)",
  fontSize: 10,
  position: "sticky",
  bottom: 0,
  background: "var(--bg)",
};

const TAB_BASE: CSSProperties = {
  background: "none",
  border: "none",
  cursor: "pointer",
  padding: 8,
  fontSize: 10,
  textDecoration: "none",
};

const DISABLED_STYLE: CSSProperties = {
  ...TAB_BASE,
  color: "var(--muted)",
  opacity: 0.5,
  cursor: "not-allowed",
};

function activeStyle(isActive: boolean): CSSProperties {
  return {
    ...TAB_BASE,
    color: isActive ? "var(--text)" : "var(--muted)",
  };
}

export function BottomNav() {
  return (
    <div style={ROW_STYLE}>
      <NavLink to="/app" end style={({ isActive }) => activeStyle(isActive)}>
        증권
      </NavLink>
      <button
        type="button"
        aria-disabled="true"
        disabled
        style={DISABLED_STYLE}
        tabIndex={-1}
      >
        관심
      </button>
      <NavLink to="/app/discover" style={({ isActive }) => activeStyle(isActive)}>
        발견
      </NavLink>
      <button
        type="button"
        aria-disabled="true"
        disabled
        style={DISABLED_STYLE}
        tabIndex={-1}
      >
        피드
      </button>
    </div>
  );
}
