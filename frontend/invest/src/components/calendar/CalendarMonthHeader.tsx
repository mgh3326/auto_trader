import { Icon } from "../../ds";

export interface CalendarMonthHeaderProps {
  title: string;
  onPrev: () => void;
  onNext: () => void;
}

export function CalendarMonthHeader({ title, onPrev, onNext }: CalendarMonthHeaderProps) {
  return (
    <div className="calendar-month-header">
      <button
        type="button"
        className="calendar-nav-btn"
        aria-label="이전 달"
        data-testid="calendar-prev-month"
        onClick={onPrev}
      >
        <Icon name="chev" size={14} />
      </button>
      <div className="calendar-month-header__title">{title}</div>
      <button
        type="button"
        className="calendar-nav-btn calendar-nav-btn--flip"
        aria-label="다음 달"
        data-testid="calendar-next-month"
        onClick={onNext}
      >
        <Icon name="chev" size={14} />
      </button>
    </div>
  );
}
