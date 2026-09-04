import { useEffect } from "react";
import { InvestRumReporter } from "./api/investRum";
import { router } from "./routes";

/** Route-level RUM without adding a browser telemetry SDK to the bundle. */
export function InvestRumObserver() {
  useEffect(() => {
    const reporter = new InvestRumReporter();
    reporter.begin(window.location.pathname);
    const unsubscribe = router.subscribe(() => reporter.begin(window.location.pathname));
    return () => {
      unsubscribe();
      reporter.stop();
    };
  }, []);
  return null;
}
