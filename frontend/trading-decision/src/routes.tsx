import { createBrowserRouter } from "react-router-dom";
import SessionDetailPage from "./pages/SessionDetailPage";
import SessionListPage from "./pages/SessionListPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <SessionListPage /> },
    { path: "/sessions/:sessionUuid", element: <SessionDetailPage /> },
    { path: "*", element: <SessionListPage /> },
  ],
  { basename: "/trading/decisions" },
);
