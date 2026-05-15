import type { FeedTopic } from "../../types/feedNews";

const TOPIC_LABEL: Record<FeedTopic, string> = {
  fx: "환율",
  rates: "금리",
};

interface NewsTopicChipsProps {
  value: FeedTopic | null;
  onChange: (topic: FeedTopic | null) => void;
}

export function NewsTopicChips({ value, onChange }: NewsTopicChipsProps) {
  const topics: FeedTopic[] = ["fx", "rates"];
  return (
    <div aria-label="뉴스 주제 필터" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {topics.map((topic) => {
        const selected = value === topic;
        return (
          <button
            key={topic}
            type="button"
            onClick={() => onChange(selected ? null : topic)}
            data-testid={`news-topic-${topic}`}
            style={{
              border: selected ? "1px solid var(--accent)" : "1px solid var(--line)",
              background: selected ? "rgba(59, 130, 246, 0.14)" : "var(--surface)",
              color: selected ? "var(--accent)" : "var(--fg-2)",
              borderRadius: 999,
              padding: "6px 10px",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
            }}
          >
            {TOPIC_LABEL[topic]}
          </button>
        );
      })}
    </div>
  );
}
