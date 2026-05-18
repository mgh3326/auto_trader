import { Link } from "react-router-dom";
import { ActionCenterContent } from "../../components/action-center/ActionCenterContent";
import { PageSafetyNote } from "../../components/PageSafetyNote";
import { DesktopShell } from "../../desktop/DesktopShell";
import { useViewport } from "../../hooks/useViewport";
import { MobileActionCenterPage } from "../mobile/MobileActionCenterPage";

export function DesktopActionCenterPage() {
  return (
    <DesktopShell
      center={
        <div style={{ display: "grid", gap: 12 }}>
          <PageSafetyNote
            routeId="action-center"
            heading="관련 화면"
            tag="액션센터"
            items={[
              <Link key="home" to="/" style={{ color: "inherit" }}>홈</Link>,
              <Link key="insights" to="/insights" style={{ color: "inherit" }}>인사이트</Link>,
              <Link key="signals" to="/my?tab=signals" style={{ color: "inherit" }}>시그널</Link>,
            ]}
          />
          <ActionCenterContent />
        </div>
      }
    />
  );
}

export function ActionCenterRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileActionCenterPage /> : <DesktopActionCenterPage />;
}
