import { ActionCenterContent } from "../../components/action-center/ActionCenterContent";
import { MobileShell } from "../../mobile/MobileShell";

export function MobileActionCenterPage() {
  return (
    <MobileShell title="액션 센터">
      <ActionCenterContent compact />
    </MobileShell>
  );
}
