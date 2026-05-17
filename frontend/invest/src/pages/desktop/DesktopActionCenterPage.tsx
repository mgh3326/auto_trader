import { ActionCenterContent, ActionCenterRelatedLinks } from "../../components/action-center/ActionCenterContent";
import { DesktopShell } from "../../desktop/DesktopShell";
import { useViewport } from "../../hooks/useViewport";
import { MobileActionCenterPage } from "../mobile/MobileActionCenterPage";

export function DesktopActionCenterPage() {
  return (
    <DesktopShell
      center={
        <div style={{ display: "grid", gap: 12 }}>
          <ActionCenterContent />
          <ActionCenterRelatedLinks />
        </div>
      }
    />
  );
}

export function ActionCenterRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileActionCenterPage /> : <DesktopActionCenterPage />;
}
