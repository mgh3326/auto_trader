import type { ReactNode } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { MobileBottomNav } from "./MobileBottomNav";

export function MobileShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div data-testid="mobile-shell" className="mobile-shell">
      <MobileTopBar title={title} />
      <div data-testid="mobile-shell-scroll" className="mobile-shell__scroll">
        {children}
      </div>
      <MobileBottomNav />
    </div>
  );
}
