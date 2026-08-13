// /invest/watches (mobile) — INVEST-WATCH-UI §57차 item ①.
import { WatchesPageBody } from "../../components/watches/WatchesPageBody";
import { MobileShell } from "../../mobile/MobileShell";

export function MobileWatchesPage() {
  return (
    <MobileShell title="감시">
      <div style={{ padding: "14px 16px 24px" }}>
        <WatchesPageBody />
      </div>
    </MobileShell>
  );
}
