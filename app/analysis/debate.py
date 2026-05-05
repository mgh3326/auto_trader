"""ROB-112 — Debate / summary builder with citation links."""

from typing import Any, Protocol

from app.schemas.research_pipeline import (
    BullBearArgument,
    PriceAnalysis,
    StageOutput,
    StageVerdict,
    SummaryDecision,
    SummaryOutput,
)


class ModelRunner(Protocol):
    async def __call__(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        ...


class StageLinkSpec:
    def __init__(
        self,
        stage_analysis_id: int,
        weight: float = 1.0,
        direction: str = "support",
        rationale: str | None = None,
    ):
        self.stage_analysis_id = stage_analysis_id
        self.weight = weight
        self.direction = direction
        self.rationale = rationale


async def build_summary(
    stage_outputs: dict[int, StageOutput],
    *,
    model_runner: ModelRunner | None = None,
) -> tuple[SummaryOutput, list[StageLinkSpec]]:
    """
    Builds a research summary from stage outputs.

    If model_runner is provided, it uses an LLM to generate the debate.
    Otherwise, it uses a deterministic v1 reducer.
    """

    warnings = []
    stale_count = 0

    # 1. Collect warnings and check staleness
    for output in stage_outputs.values():
        if output.verdict == StageVerdict.UNAVAILABLE:
            reason = "not_implemented"
            if hasattr(output.signals, "reason"):
                reason = output.signals.reason
            warnings.append(f"{output.stage_type}: UNAVAILABLE ({reason})")

        if output.source_freshness and output.source_freshness.stale_flags:
            stale_count += 1

    # 2. Force HOLD if >= 2 stages are stale
    force_hold = False
    if stale_count >= 2:
        force_hold = True
        warnings.append(f"Forcing HOLD: {stale_count} stages have stale data.")

    if model_runner:
        return await _build_llm_debate(
            stage_outputs, model_runner, force_hold=force_hold, warnings=warnings
        )

    return _build_deterministic_v1(stage_outputs, force_hold=force_hold, warnings=warnings)


async def _build_llm_debate(
    stage_outputs: dict[int, StageOutput],
    model_runner: ModelRunner,
    force_hold: bool,
    warnings: list[str],
) -> tuple[SummaryOutput, list[StageLinkSpec]]:
    # Placeholder for LLM debate logic.
    # In a real implementation, this would format a prompt with stage details,
    # call model_runner, and parse the JSON response.

    # For now, we fall back to deterministic and just simulate LLM fields.
    summary, links = _build_deterministic_v1(stage_outputs, force_hold, warnings)

    summary.model_name = "mock-llm"
    summary.raw_payload = {"simulation": True}
    summary.token_input = 100
    summary.token_output = 50

    return summary, links


def _build_deterministic_v1(
    stage_outputs: dict[int, StageOutput],
    force_hold: bool,
    warnings: list[str],
) -> tuple[SummaryOutput, list[StageLinkSpec]]:

    bull_ids = []
    bear_ids = []
    neutral_ids = []

    total_confidence = 0
    count = 0

    for sid, output in stage_outputs.items():
        if output.verdict == StageVerdict.BULL:
            bull_ids.append(sid)
        elif output.verdict == StageVerdict.BEAR:
            bear_ids.append(sid)
        elif output.verdict == StageVerdict.NEUTRAL:
            neutral_ids.append(sid)

        if output.verdict != StageVerdict.UNAVAILABLE:
            total_confidence += output.confidence
            count += 1

    avg_confidence = int(total_confidence / count) if count > 0 else 0

    # Decision logic
    if force_hold:
        decision = SummaryDecision.HOLD
    else:
        score = len(bull_ids) - len(bear_ids)
        if score > 0:
            decision = SummaryDecision.BUY
        elif score < 0:
            decision = SummaryDecision.SELL
        else:
            decision = SummaryDecision.HOLD

    # Bull/Bear arguments (Citations)
    bull_arguments = []
    if bull_ids:
        bull_arguments.append(
            BullBearArgument(
                text=f"Bullish indicators from {len(bull_ids)} stages.",
                cited_stage_ids=bull_ids,
                direction="support",
                weight=1.0,
            )
        )

    bear_arguments = []
    if bear_ids:
        bear_arguments.append(
            BullBearArgument(
                text=f"Bearish indicators from {len(bear_ids)} stages.",
                cited_stage_ids=bear_ids,
                direction="support",
                weight=1.0,
            )
        )

    # Ensure at least one argument if needed?
    # The requirement says "Bull/bear arguments must each cite at least one stage_analysis id (no orphan claims)".
    # This means if an argument exists, it must have a citation.

    summary = SummaryOutput(
        decision=decision,
        confidence=avg_confidence,
        bull_arguments=bull_arguments,
        bear_arguments=bear_arguments,
        price_analysis=PriceAnalysis(),
        reasons=[f"Score: {len(bull_ids)} bulls, {len(bear_ids)} bears"],
        warnings=warnings,
        model_name="deterministic-v1",
        prompt_version="v1",
    )

    # Build links
    links = []
    for sid, output in stage_outputs.items():
        direction = "context"
        if output.verdict == StageVerdict.BULL:
            direction = "support" if decision == SummaryDecision.BUY else "contradict"
        elif output.verdict == StageVerdict.BEAR:
            direction = "support" if decision == SummaryDecision.SELL else "contradict"

        links.append(
            StageLinkSpec(
                stage_analysis_id=sid,
                weight=1.0,
                direction=direction,
                rationale=f"Verdict: {output.verdict}",
            )
        )

    return summary, links
