import { createBrowserRouter, Navigate } from "react-router-dom";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <HomePage /> },
    { path: "/paper", element: <PaperPlaceholderPage /> },
    { path: "/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest/app" }
);
