import { ActionCenterContent, ActionCenterRelatedLinks } from "../../components/action-center/ActionCenterContent";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { useViewport } from "../../hooks/useViewport";
import { MobileActionCenterPage } from "../mobile/MobileActionCenterPage";

export function DesktopActionCenterPage() {
  return (
    <DesktopShell
      center={<ActionCenterContent />}
      right={<div style={{ display: "grid", gap: 12 }}><ActionCenterRelatedLinks /><RightRemotePanel /></div>}
    />
  );
}

export function ActionCenterRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileActionCenterPage /> : <DesktopActionCenterPage />;
}
