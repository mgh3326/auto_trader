import { useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabSpec {
  id: string;
  label: string;
}

interface Props {
  tabs: TabSpec[];
  activeId: string;
  onChange: (id: string) => void;
  renderPanel: (id: string) => ReactNode;
  ariaLabel?: string;
}

export default function ResearchTabs({
  tabs,
  activeId,
  onChange,
  renderPanel,
  ariaLabel = "Research detail tabs",
}: Props) {
  const tablistRef = useRef<HTMLDivElement>(null);

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const idx = tabs.findIndex((t) => t.id === activeId);
    if (idx < 0) return;
    let nextIdx: number | null = null;
    if (event.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
    if (event.key === "ArrowLeft")
      nextIdx = (idx - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIdx = 0;
    if (event.key === "End") nextIdx = tabs.length - 1;
    if (nextIdx === null) return;
    event.preventDefault();
    const next = tabs[nextIdx];
    if (!next) return;
    onChange(next.id);
    const button = tablistRef.current?.querySelector<HTMLButtonElement>(
      `[data-tab-id="${next.id}"]`,
    );
    button?.focus();
  }

  return (
    <div>
      <div role="tablist" aria-label={ariaLabel} ref={tablistRef}>
        {tabs.map((tab) => {
          const selected = tab.id === activeId;
          return (
            <button
              key={tab.id}
              role="tab"
              type="button"
              data-tab-id={tab.id}
              aria-selected={selected}
              aria-controls={`tabpanel-${tab.id}`}
              id={`tab-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(tab.id)}
              onKeyDown={onKeyDown}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={`tabpanel-${tab.id}`}
          aria-labelledby={`tab-${tab.id}`}
          hidden={tab.id !== activeId}
        >
          {tab.id === activeId ? renderPanel(tab.id) : null}
        </div>
      ))}
    </div>
  );
}
