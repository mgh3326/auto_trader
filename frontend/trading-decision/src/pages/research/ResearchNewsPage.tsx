import ResearchNewsTab from "../../components/ResearchNewsTab";
import { useResearchSessionContext } from "./ResearchSessionContext";

export default function ResearchNewsPage() {
  const { stagesByType } = useResearchSessionContext();
  return <ResearchNewsTab stage={stagesByType.news ?? null} />;
}
