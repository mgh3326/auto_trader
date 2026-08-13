// /invest/watches (desktop) — INVEST-WATCH-UI §57차 item ①.
import { WatchesPageBody } from "../../components/watches/WatchesPageBody";
import { DesktopShell } from "../../desktop/DesktopShell";

export function DesktopWatchesPage() {
  return <DesktopShell center={<WatchesPageBody />} />;
}
