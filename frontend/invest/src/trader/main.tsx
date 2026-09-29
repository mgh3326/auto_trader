import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { TraderPage } from "./TraderPage";
import { applyInitialTheme } from "../theme/useTheme";

applyInitialTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TraderPage />
  </StrictMode>,
);
