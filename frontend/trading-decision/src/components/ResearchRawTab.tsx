import type { ResearchSessionFullResponse } from "../api/types";

interface Props {
  data: ResearchSessionFullResponse;
}

export default function ResearchRawTab({ data }: Props) {
  return (
    <div>
      <section aria-label="세션 원본">
        <h4>session</h4>
        <pre>{JSON.stringify(data.session, null, 2)}</pre>
      </section>
      <section aria-label="단계 원본">
        <h4>stages</h4>
        <pre>{JSON.stringify(data.stages, null, 2)}</pre>
      </section>
      <section aria-label="요약 원본">
        <h4>summary</h4>
        <pre>{JSON.stringify(data.summary, null, 2)}</pre>
      </section>
    </div>
  );
}
