import ResearchMarketTab from "../../components/ResearchMarketTab";
import { useResearchSessionContext } from "./ResearchSessionContext";

export default function ResearchMarketPage() {
  const { stagesByType } = useResearchSessionContext();
  return <ResearchMarketTab stage={stagesByType.market ?? null} />;
}
