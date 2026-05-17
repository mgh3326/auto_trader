import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Icon } from "../ds";

const DISMISS_KEY_PREFIX = "invest:safety-note-dismissed:";

function readDismissed(routeId: string): boolean {
  try {
    return window.localStorage.getItem(`${DISMISS_KEY_PREFIX}${routeId}`) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(routeId: string): void {
  try {
    window.localStorage.setItem(`${DISMISS_KEY_PREFIX}${routeId}`, "1");
  } catch {
    /* ignore */
  }
}

export function PageSafetyNote({
  routeId,
  heading,
  tag,
  items,
  children,
  dismissible = true,
}: Readonly<{
  routeId: string;
  heading: string;
  tag?: string;
  items?: ReactNode[];
  children?: ReactNode;
  dismissible?: boolean;
}>) {
  const [dismissed, setDismissed] = useState<boolean>(() =>
    dismissible ? readDismissed(routeId) : false,
  );

  useEffect(() => {
    if (!dismissible) {
      setDismissed(false);
      return;
    }
    setDismissed(readDismissed(routeId));
  }, [routeId, dismissible]);

  const handleDismiss = useCallback(() => {
    writeDismissed(routeId);
    setDismissed(true);
  }, [routeId]);

  if (dismissed) return null;

  return (
    <div
      data-testid="page-safety-note"
      data-route-id={routeId}
      role="note"
      style={{
        display: "grid",
        gridTemplateColumns: dismissible ? "18px 1fr auto" : "18px 1fr",
        gap: 12,
        alignItems: "start",
        background: "var(--accent-soft)",
        border: "1px solid color-mix(in srgb, var(--accent) 15%, transparent)",
        borderRadius: 12,
        padding: "12px 14px",
        fontSize: 12,
        color: "var(--fg-1)",
      }}
    >
      <span style={{ color: "var(--accent)", marginTop: 1 }}>
        <Icon name="info" size={16} />
      </span>
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontWeight: 700,
            color: "var(--fg)",
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {heading}
          {tag ? (
            <span
              style={{
                fontSize: 10,
                padding: "1px 6px",
                borderRadius: 999,
                background: "var(--surface)",
                color: "var(--accent)",
                fontWeight: 700,
                border: "1px solid color-mix(in srgb, var(--accent) 20%, transparent)",
              }}
            >
              {tag}
            </span>
          ) : null}
        </div>
        {items && items.length > 0 ? (
          <ul
            style={{
              margin: "6px 0 0",
              padding: 0,
              listStyle: "none",
              display: "grid",
              gap: 4,
            }}
          >
            {items.map((item, idx) => (
              <li
                key={idx}
                style={{
                  color: "var(--fg-2)",
                  lineHeight: 1.55,
                }}
              >
                <span style={{ color: "var(--fg-4)", marginRight: 6 }}>·</span>
                {item}
              </li>
            ))}
          </ul>
        ) : null}
        {children ? <div style={{ marginTop: 6, color: "var(--fg-2)", lineHeight: 1.55 }}>{children}</div> : null}
      </div>
      {dismissible ? (
        <button
          type="button"
          onClick={handleDismiss}
          aria-label="안내 닫기"
          data-testid="page-safety-note-dismiss"
          style={{
            background: "transparent",
            border: "none",
            cursor: "pointer",
            padding: 2,
            color: "var(--fg-3)",
            fontFamily: "inherit",
            fontSize: 13,
            lineHeight: 1,
          }}
        >
          ✕
        </button>
      ) : null}
    </div>
  );
}
