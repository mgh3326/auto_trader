import "./screener.css";

interface Props {
  open: boolean;
  onClose: () => void;
  appliedChipCount: number;
}

const TABS = ["기본", "재무", "시세", "기술", "필터 검색"];
const BASIC_CATEGORIES = ["국가", "시장", "카테고리", "시가총액", "제외 종목 관리"];

export function ScreenerFilterModal({ open, onClose, appliedChipCount }: Props) {
  if (!open) return null;
  return (
    <div className="screener-modal-backdrop" role="dialog" aria-modal="true" data-testid="screener-modal">
      <div className="screener-modal">
        <header className="screener-modal-header">
          <h3>필터</h3>
          <button type="button" className="screener-modal-close" onClick={onClose} aria-label="닫기">
            ×
          </button>
        </header>
        <nav className="screener-modal-tabs">
          {TABS.map((t, i) => (
            <button
              key={t}
              type="button"
              className={i === 0 ? "screener-modal-tab-active" : "screener-modal-tab"}
              disabled
              aria-disabled="true"
            >
              {t}
            </button>
          ))}
        </nav>
        <section className="screener-modal-body">
          <p className="screener-modal-notice">
            세부 필터 편집은 준비중입니다. 좌측 프리셋에서 선택해 주세요.
          </p>
          <ul className="screener-modal-category-list">
            {BASIC_CATEGORIES.map((c) => (
              <li key={c} className="screener-modal-category-item">
                {c}
              </li>
            ))}
          </ul>
        </section>
        <footer className="screener-modal-footer">
          <button type="button" disabled aria-disabled="true">초기화</button>
          <button type="button" disabled aria-disabled="true">
            {appliedChipCount}개 필터 적용 (준비중)
          </button>
        </footer>
      </div>
    </div>
  );
}
