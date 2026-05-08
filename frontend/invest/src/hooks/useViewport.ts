import { useEffect, useState } from "react";

export type Viewport = "mobile" | "compact" | "desktop";

const MOBILE_MAX = 900;
const COMPACT_MAX = 1200;

function detect(): Viewport {
  if (typeof window === "undefined") return "desktop";
  const w = window.innerWidth;
  if (w < MOBILE_MAX) return "mobile";
  if (w < COMPACT_MAX) return "compact";
  return "desktop";
}

export function useViewport(): Viewport {
  const [vp, setVp] = useState<Viewport>(() => detect());
  useEffect(() => {
    function onResize() {
      setVp(detect());
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return vp;
}
