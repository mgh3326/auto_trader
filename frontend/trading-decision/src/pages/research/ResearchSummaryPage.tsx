import { useNavigate } from "react-router-dom";
import ResearchSummaryTab from "../../components/ResearchSummaryTab";
import { useResearchSessionContext } from "./ResearchSessionContext";

export default function ResearchSummaryPage() {
  const { data } = useResearchSessionContext();
  const navigate = useNavigate();
  return (
    <ResearchSummaryTab
      data={data}
      onJumpToStage={(stageType) => navigate(`../${stageType}`)}
    />
  );
}
