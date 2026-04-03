"""AI Markdown Generation Service

포트폴리오 및 종목 데이터를 AI 질문용 Markdown으로 변환하는 서비스
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.ai_markdown import InvestmentProfile


class AIMarkdownService:
    """AI Markdown 생성 서비스"""

    def __init__(self):
        self.investment_profile = InvestmentProfile()

    def generate_portfolio_stance_markdown(
        self,
        portfolio_data: dict[str, Any],
    ) -> dict[str, Any]:
        """포트폴리오 전체 스탠스용 Markdown 생성"""
        positions = portfolio_data.get("positions", [])
        summary = self._extract_portfolio_summary(portfolio_data)

        title = f"포트폴리오 전체 스탠스 분석 - {datetime.now().strftime('%Y-%m-%d')}"
        filename = f"portfolio-stance-{datetime.now().strftime('%Y%m%d')}.md"

        content = f"""# {title}

## 역할 및 응답 방식
당신은 투자 추천 봇이 아닙니다. 현재 포트폴리오를 보고 이후 스탠스를 정리해주는 분석 보조 역할을 수행합니다.

## 투자 성향
{self.investment_profile.to_markdown()}

## 현재 포트폴리오 요약
- 총 평가금액: {summary["total_evaluation"]:,}원
- 총 손익: {summary["total_profit_loss"]:+,.0f}원 ({summary["total_profit_rate"]:+.2f}%)
- 보유 종목 수: {summary["total_positions"]}개
- 자산군 분포:
{self._format_asset_allocation(summary["allocation"])}

## 상위 보유 종목
{self._format_top_holdings(positions, limit=10)}

## 질문
현재 포트폴리오를 감안했을 때, 지금 시점의 적절한 스탠스를 분석해주세요:
1. 현재 포트폴리오의 핵심 특징 3가지
2. 적절한 스탠스 (공격적 확대 / 선별적 추가매수 / 유지 / 일부 축소 / 방어적 현금확보 중 선택)
3. 근거 3개
4. 틀릴 수 있는 시나리오 2개
5. 체크할 트리거 3개
6. 한 줄 결론

## 원하는 답변 형식
- 명확하고 실행 가능한 스탠스 제시
- 각 근거는 데이터 기반으로 작성
- 시나리오는 반대 의견도 고려한 균형 잡힌 관점
- 트리거는 구체적인 수치나 이벤트 기준으로 작성
"""

        return {
            "title": title,
            "content": content,
            "filename": filename,
            "metadata": {
                "position_count": len(positions),
                "generated_at": datetime.now().isoformat(),
            },
        }

    def generate_stock_stance_markdown(
        self,
        stock_data: dict[str, Any],
    ) -> dict[str, Any]:
        """종목 현재 스탠스용 Markdown 생성"""
        summary = stock_data.get("summary", {})
        weights = stock_data.get("weights", {})

        symbol = summary.get("symbol", "UNKNOWN")
        name = summary.get("name", symbol)
        market_type = summary.get("market_type", "UNKNOWN")

        title = f"[{symbol}] {name} - 현재 스탠스 분석"
        filename = f"stock-{symbol}-stance.md"

        content = f"""# {title}

## 역할 및 응답 방식
특정 종목에 대해 "당장 사라/팔아라"가 아닌, 보유자 관점에서 스탠스를 정리하는 역할을 수행합니다.

## 투자 성향
{self.investment_profile.to_markdown()}

## 현재 포지션 정보
- 종목명: {name} ({symbol})
- 시장: {market_type}
- 현재가: {self._format_price(summary.get("current_price"), market_type)}
- 평균단가: {self._format_price(summary.get("avg_price"), market_type)}
- 보유수량: {summary.get("quantity", 0):,.4f}
- 수익률: {summary.get("profit_rate", 0):+.2f}%
- 평가금액: {self._format_price(summary.get("evaluation"), market_type)}
- 포트폴리오 비중: {weights.get("portfolio_weight_pct", "N/A")}%
- 동일 시장 내 비중: {weights.get("market_weight_pct", "N/A")}%

## 질문
현재 보유 중인 {name}({symbol})에 대해 분석해주세요:
1. 현재 포지션 해석 (수익률, 비중 등을 종합적으로)
2. 이후 스탠스 (추가매수 / 유지 / 일부축소 / 관망 중 선택)
3. 근거 3개
4. 스탠스가 바뀌는 조건 (역전 시나리오)
5. 반대 시나리오 (내가 틀렸을 경우)
6. 한 줄 결론

## 원하는 답변 형식
- 보유자 관점의 현실적인 조언
- 구체적인 추가매수/축소 조건 제시
- 리스크 관리 관점 포함
"""

        return {
            "title": title,
            "content": content,
            "filename": filename,
            "metadata": {
                "symbol": symbol,
                "market_type": market_type,
                "generated_at": datetime.now().isoformat(),
            },
        }

    def generate_stock_add_or_hold_markdown(
        self,
        stock_data: dict[str, Any],
    ) -> dict[str, Any]:
        """종목 추가매수 vs 유지용 Markdown 생성"""
        summary = stock_data.get("summary", {})
        weights = stock_data.get("weights", {})
        journal = stock_data.get("journal", {})

        symbol = summary.get("symbol", "UNKNOWN")
        name = summary.get("name", symbol)
        market_type = summary.get("market_type", "UNKNOWN")
        current_price = summary.get("current_price", 0)
        avg_price = summary.get("avg_price", 0)

        # 추가매수 가능 여부 판단을 위한 간단한 로직
        price_ratio = current_price / avg_price if avg_price and avg_price > 0 else 1.0

        title = f"[{symbol}] {name} - 추가매수 vs 유지 판단"
        filename = f"stock-{symbol}-add-or-hold.md"

        content = f"""# {title}

## 역할 및 응답 방식
현재 가격/평균단가/비중을 감안하여, 지금은 추가매수가 가능한지 아니면 기존 물량 유지가 적절한지 판단하는 역할을 수행합니다.

## 투자 성향
{self.investment_profile.to_markdown()}

## 현재 포지션 정보
- 종목명: {name} ({symbol})
- 시장: {market_type}
- 현재가: {self._format_price(current_price, market_type)}
- 평균단가: {self._format_price(avg_price, market_type)}
- 현재가/평단가 비율: {price_ratio:.2%}
- 보유수량: {summary.get("quantity", 0):,.4f}
- 수익률: {summary.get("profit_rate", 0):+.2f}%
- 포트폴리오 비중: {weights.get("portfolio_weight_pct", "N/A")}%

## 매매 계획 정보
{self._format_journal_info(journal)}

## 질문
현재 {name}({symbol})의 가격 수준을 감안했을 때:
1. 지금은 추가매수가 가능한가요, 아니면 기존 물량 유지가 적절한가요?
2. 추가매수가 가능하다면 유효한 조건 (가격/비중/시점 기준)
3. 추가매수를 피해야 할 조건 (리스크/시그널 기준)
4. 체크할 신호 3개
5. 한 줄 결론

## 원하는 답변 형식
- Yes/No 명확한 판단 우선 제시
- 추가매수 시 구체적인 가격/수량 전략
- 리스크 관리 관점에서의 최대 허용 비중 제시
"""

        return {
            "title": title,
            "content": content,
            "filename": filename,
            "metadata": {
                "symbol": symbol,
                "market_type": market_type,
                "price_ratio": price_ratio,
                "generated_at": datetime.now().isoformat(),
            },
        }

    def _extract_portfolio_summary(
        self, portfolio_data: dict[str, Any]
    ) -> dict[str, Any]:
        """포트폴리오 요약 데이터 추출"""
        positions = portfolio_data.get("positions", [])

        total_evaluation = sum(p.get("evaluation", 0) or 0 for p in positions)
        total_profit_loss = sum(p.get("profit_loss", 0) or 0 for p in positions)

        # 자산군별 비중 계산
        allocation = {"KR": 0, "US": 0, "CRYPTO": 0}
        for pos in positions:
            market = pos.get("market_type", "KR")
            eval_val = pos.get("evaluation", 0) or 0
            if market in allocation:
                allocation[market] += eval_val

        # 비율로 변환
        if total_evaluation > 0:
            allocation = {
                k: (v / total_evaluation * 100) for k, v in allocation.items() if v > 0
            }

        # 총 수익률 계산
        total_cost = total_evaluation - total_profit_loss
        total_profit_rate = (
            (total_profit_loss / total_cost * 100) if total_cost > 0 else 0
        )

        return {
            "total_evaluation": total_evaluation,
            "total_profit_loss": total_profit_loss,
            "total_profit_rate": total_profit_rate,
            "total_positions": len(positions),
            "allocation": allocation,
        }

    def _format_asset_allocation(self, allocation: dict[str, float]) -> str:
        """자산군 비중을 Markdown 리스트로 포맷팅"""
        market_names = {"KR": "국내주식", "US": "해외주식", "CRYPTO": "암호화폐"}
        lines = []
        for market, pct in sorted(allocation.items(), key=lambda x: -x[1]):
            name = market_names.get(market, market)
            lines.append(f"  - {name}: {pct:.1f}%")
        return "\n".join(lines) if lines else "  - 데이터 없음"

    def _format_top_holdings(self, positions: list[dict], limit: int = 10) -> str:
        """상위 보유 종목 포맷팅"""
        sorted_positions = sorted(
            positions, key=lambda x: x.get("evaluation", 0) or 0, reverse=True
        )[:limit]

        if not sorted_positions:
            return "- 보유 종목 없음"

        lines = []
        for pos in sorted_positions:
            symbol = pos.get("symbol", "N/A")
            name = pos.get("name", symbol)
            profit_rate = pos.get("profit_rate", 0) or 0
            eval_val = pos.get("evaluation", 0) or 0
            lines.append(
                f"- {name} ({symbol}): {eval_val:,.0f}원 ({profit_rate:+.2f}%)"
            )

        return "\n".join(lines)

    def _format_price(self, price: float | None, market_type: str) -> str:
        """가격 포맷팅"""
        if price is None:
            return "N/A"
        currency = "$" if market_type == "US" else "₩"
        return f"{currency}{price:,.2f}"

    def _format_journal_info(self, journal: dict[str, Any]) -> str:
        """매매 계획 정보 포맷팅"""
        if not journal:
            return "- 등록된 매매 계획 없음"

        target = journal.get("target_price")
        stop = journal.get("stop_loss_price")
        target_dist = journal.get("target_distance_pct")
        stop_dist = journal.get("stop_distance_pct")

        lines = []
        if target:
            lines.append(f"- 목표가: {target:,.2f} ({target_dist:+.1f}%)")
        if stop:
            lines.append(f"- 손절가: {stop:,.2f} ({stop_dist:+.1f}%)")

        return "\n".join(lines) if lines else "- 등록된 매매 계획 없음"
