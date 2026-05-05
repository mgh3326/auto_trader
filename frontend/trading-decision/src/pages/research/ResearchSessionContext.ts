import { useOutletContext } from "react-router-dom";
import type {
  ResearchSessionFullResponse,
  StageAnalysis,
  StageType,
} from "../../api/types";

export interface ResearchSessionContext {
  data: ResearchSessionFullResponse;
  stagesByType: Partial<Record<StageType, StageAnalysis>>;
}

export function useResearchSessionContext(): ResearchSessionContext {
  return useOutletContext<ResearchSessionContext>();
}
