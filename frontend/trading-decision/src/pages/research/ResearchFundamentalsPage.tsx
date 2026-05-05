import ResearchFundamentalsTab from "../../components/ResearchFundamentalsTab";
import { useResearchSessionContext } from "./ResearchSessionContext";

export default function ResearchFundamentalsPage() {
  const { stagesByType } = useResearchSessionContext();
  return (
    <ResearchFundamentalsTab stage={stagesByType.fundamentals ?? null} />
  );
}
