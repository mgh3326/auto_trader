import { useAccountPanelContext } from "./AccountPanelProvider";

export function useAccountPanel() {
  return useAccountPanelContext();
}
