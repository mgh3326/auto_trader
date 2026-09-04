import { RouterProvider } from "react-router-dom";
import { InvestRumObserver } from "./InvestRumObserver";
import { router } from "./routes";
import { AccountPanelProvider } from "./desktop/AccountPanelProvider";
import "./styles/tokens.css";
import "./styles.css";

export default function App() {
  return (
    <AccountPanelProvider>
      <InvestRumObserver />
      <RouterProvider router={router} />
    </AccountPanelProvider>
  );
}
