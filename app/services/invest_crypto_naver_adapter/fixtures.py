"""Fixture/reference metadata for the read-only Naver crypto adapter.

Naver crypto coverage is intentionally treated as reference-only until a stable,
allowed upstream endpoint is separately verified. Keep this file deterministic and
side-effect free.
"""

from __future__ import annotations

NAVER_CRYPTO_REFERENCES: dict[str, dict[str, str | list[str]]] = {
    "KRW-BTC": {
        "baseSymbol": "BTC",
        "koreanName": "비트코인",
        "englishName": "Bitcoin",
        "displayName": "비트코인",
        "naverUrl": "https://m.stock.naver.com/crypto/UPBIT/KRW-BTC",
        "referenceNotes": [
            "Naver crypto metadata is fixture/reference-only in ROB-234.",
            "Use Upbit official/public read-model prices as the executable source of truth.",
        ],
    },
    "KRW-ETH": {
        "baseSymbol": "ETH",
        "koreanName": "이더리움",
        "englishName": "Ethereum",
        "displayName": "이더리움",
        "naverUrl": "https://m.stock.naver.com/crypto/UPBIT/KRW-ETH",
        "referenceNotes": [
            "Naver crypto metadata is fixture/reference-only in ROB-234.",
            "Use Upbit official/public read-model prices as the executable source of truth.",
        ],
    },
    "KRW-XRP": {
        "baseSymbol": "XRP",
        "koreanName": "엑스알피",
        "englishName": "XRP",
        "displayName": "엑스알피",
        "naverUrl": "https://m.stock.naver.com/crypto/UPBIT/KRW-XRP",
        "referenceNotes": [
            "Naver crypto metadata is fixture/reference-only in ROB-234.",
            "Use Upbit official/public read-model prices as the executable source of truth.",
        ],
    },
    "KRW-SOL": {
        "baseSymbol": "SOL",
        "koreanName": "솔라나",
        "englishName": "Solana",
        "displayName": "솔라나",
        "naverUrl": "https://m.stock.naver.com/crypto/UPBIT/KRW-SOL",
        "referenceNotes": [
            "Naver crypto metadata is fixture/reference-only in ROB-234.",
            "Use Upbit official/public read-model prices as the executable source of truth.",
        ],
    },
}
