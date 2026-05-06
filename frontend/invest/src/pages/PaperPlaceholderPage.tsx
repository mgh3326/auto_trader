import { useParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";

export function PaperPlaceholderPage() {
  const { variant } = useParams();
  return (
    <AppShell>
      <div style={{ padding: "4rem 2rem", textAlign: "center" }}>
        <h1 style={{ fontSize: 24 }}>준비 중</h1>
        <p className="subtle">
          {variant ? `${variant} ` : ""}모의투자 기능은 현재 개발 중입니다.
        </p>
      </div>
    </AppShell>
  );
}
