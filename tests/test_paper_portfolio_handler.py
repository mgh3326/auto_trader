"""Unit tests for paper portfolio handler."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.paper_portfolio_handler import (
    PaperAccountSelector,
    is_paper_account_token,
    parse_paper_account_token,
)


class TestIsPaperAccountToken:
    def test_exact_paper(self):
        assert is_paper_account_token("paper") is True

    def test_paper_with_name(self):
        assert is_paper_account_token("paper:데이트레이딩") is True

    def test_case_insensitive(self):
        assert is_paper_account_token("PAPER") is True
        assert is_paper_account_token("Paper:swing") is True

    def test_paper_with_whitespace(self):
        assert is_paper_account_token("  paper  ") is True

    def test_non_paper(self):
        assert is_paper_account_token("kis") is False
        assert is_paper_account_token("upbit") is False
        assert is_paper_account_token("paperless") is False  # prefix-only match forbidden
        assert is_paper_account_token(None) is False
        assert is_paper_account_token("") is False


class TestParsePaperAccountToken:
    def test_bare_paper_returns_all_selector(self):
        sel = parse_paper_account_token("paper")
        assert sel == PaperAccountSelector(account_name=None)

    def test_paper_with_name(self):
        sel = parse_paper_account_token("paper:데이트레이딩")
        assert sel == PaperAccountSelector(account_name="데이트레이딩")

    def test_trims_whitespace(self):
        sel = parse_paper_account_token("  paper :   swing  ")
        assert sel == PaperAccountSelector(account_name="swing")

    def test_empty_name_after_colon(self):
        sel = parse_paper_account_token("paper:")
        assert sel == PaperAccountSelector(account_name=None)

    def test_non_paper_raises(self):
        with pytest.raises(ValueError, match="not a paper account token"):
            parse_paper_account_token("kis")
