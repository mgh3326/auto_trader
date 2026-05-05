import ResearchSocialTab from "../../components/ResearchSocialTab";
import { useResearchSessionContext } from "./ResearchSessionContext";

export default function ResearchSocialPage() {
  const { stagesByType } = useResearchSessionContext();
  return <ResearchSocialTab stage={stagesByType.social ?? null} />;
}
