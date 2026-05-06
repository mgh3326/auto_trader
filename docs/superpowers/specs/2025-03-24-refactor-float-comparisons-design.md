# Design Spec: Refactor Float Comparisons in `tests/test_trading_integration.py`

## Goal
Improve the readability and idiomatic quality of the test suite by replacing manual absolute difference checks with `pytest.approx`.

## Scope
- File: `tests/test_trading_integration.py`
- Target: All occurrences of `assert abs(a - b) < eps`.

## Architecture & Implementation
The refactoring is a direct string replacement of manual assertion logic with the `pytest.approx` helper.

### Refactoring Mappings
- `assert abs(result - expected) < 0.01` -> `assert result == pytest.approx(expected, abs=0.01)`
- `assert abs(data["combined_avg"] - 73666.67) < 0.01` -> `assert data["combined_avg"] == pytest.approx(73666.67, abs=0.01)`
- `assert abs(result.price - 73666.67) < 0.01` -> `assert result.price == pytest.approx(73666.67, abs=0.01)`
- `assert abs(result.price - expected) < 0.01` -> `assert result.price == pytest.approx(expected, abs=0.01)`
- `assert abs(result.price - expected) < 1` -> `assert result.price == pytest.approx(expected, abs=1)`
- `assert abs(result["based_on_kis_avg"].percent - expected_percent) < 0.01` -> `assert result["based_on_kis_avg"].percent == pytest.approx(expected_percent, abs=0.01)`
- `assert abs(ref.combined_avg - expected_combined) < 0.01` -> `assert ref.combined_avg == pytest.approx(expected_combined, abs=0.01)`
- `assert abs(r3.price - 73666.67) < 0.01` -> `assert r3.price == pytest.approx(73666.67, abs=0.01)`
- `assert abs(r1.price - 74000 * 1.05) < 0.01` -> `assert r1.price == pytest.approx(74000 * 1.05, abs=0.01)`
- `assert abs(r2.price - 73000 * 1.10) < 0.01` -> `assert r2.price == pytest.approx(73000 * 1.10, abs=0.01)`
- `assert abs(r3.price - 73666.67 * 1.03) < 1` -> `assert r3.price == pytest.approx(73666.67 * 1.03, abs=1)`

## Testing & Validation
1.  **Run Tests:** Execute `uv run pytest tests/test_trading_integration.py` to confirm behavioral parity.
2.  **Linting:** Run `make lint` and `make format` to ensure style consistency.

## Success Criteria
- No manual `abs()` calls remain for float comparisons in the target file.
- All integration tests pass.
- Code matches project style standards.
