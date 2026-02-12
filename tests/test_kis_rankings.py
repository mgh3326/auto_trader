"""KIS ranking API 파라미터 테스트 (서비스 레이어 테스트)

이 모듈은 실제 KISClient의 ranking API 호출 파라미터를 검증합니다.
MCP 도구 테스트(test_mcp_top_stocks.py)와 분리하여 책임을 명확히 합니다.
"""

from unittest.mock import MagicMock

import pytest

from app.services.kis import (
    FLUCTUATION_RANK_TR,
    FLUCTUATION_RANK_URL,
    FOREIGN_BUYING_RANK_TR,
    FOREIGN_BUYING_RANK_URL,
    MARKET_CAP_RANK_TR,
    MARKET_CAP_RANK_URL,
    KISClient,
)


@pytest.mark.asyncio
class TestKISRankingAPIParams:
    async def test_market_cap_rank_api_params(self, monkeypatch):
        """market_cap_rank가 올바른 URL, tr_id, 파라미터로 API 호출하는지 검증"""
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().market_cap_rank(market="J", limit=5)

        assert len(captured_requests) == 1
        req = captured_requests[0]

        assert (
            req["url"]
            == f"https://openapi.koreainvestment.com:9443{MARKET_CAP_RANK_URL}"
        )
        assert req["headers"]["tr_id"] == MARKET_CAP_RANK_TR
        assert req["headers"]["authorization"] == "Bearer test_token"
        assert req["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
        assert req["params"]["FID_COND_SCR_DIV_CODE"] == "20174"
        assert req["params"]["FID_INPUT_ISCD"] == "0000"
        assert req["params"]["FID_DIV_CLS_CODE"] == "0"
        assert req["params"]["FID_TRGT_CLS_CODE"] == "0"
        assert req["params"]["FID_TRGT_EXLS_CLS_CODE"] == "0"
        assert req["params"]["FID_INPUT_PRICE_1"] == ""
        assert req["params"]["FID_INPUT_PRICE_2"] == ""
        assert req["params"]["FID_VOL_CNT"] == ""

        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005930"

    async def test_fluctuation_rank_api_params(self, monkeypatch):
        """fluctuation_rank가 올바른 URL, tr_id, 파라미터로 API 호출하는지 검증"""
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "5.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(market="J", direction="up", limit=5)

        assert len(captured_requests) == 1
        req = captured_requests[0]

        assert (
            req["url"]
            == f"https://openapi.koreainvestment.com:9443{FLUCTUATION_RANK_URL}"
        )
        assert req["headers"]["tr_id"] == FLUCTUATION_RANK_TR
        assert req["headers"]["authorization"] == "Bearer test_token"
        assert req["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
        assert req["params"]["FID_COND_SCR_DIV_CODE"] == "20170"
        assert req["params"]["FID_INPUT_ISCD"] == "0000"
        assert req["params"]["FID_DIV_CLS_CODE"] == "0"
        assert req["params"]["FID_RANK_SORT_CLS_CODE"] == "0"
        assert req["params"]["FID_INPUT_CNT_1"] == "0"
        assert req["params"]["FID_PRC_CLS_CODE"] == "0"
        assert req["params"]["FID_INPUT_PRICE_1"] == ""
        assert req["params"]["FID_INPUT_PRICE_2"] == ""
        assert req["params"]["FID_VOL_CNT"] == ""
        assert req["params"]["FID_TRGT_CLS_CODE"] == "0"
        assert req["params"]["FID_TRGT_EXLS_CLS_CODE"] == "0"
        assert req["params"]["FID_RSFL_RATE1"] == ""
        assert req["params"]["FID_RSFL_RATE2"] == ""

        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005930"

    async def test_fluctuation_rank_down_direction(self, monkeypatch):
        """fluctuation_rank direction='down'일 때의 동작 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "stck_prpr": "70000",
                        "prdy_ctrt": "-3.0",
                        "acml_vol": "5000000",
                        "hts_avls": "50000000000000",
                        "acml_tr_pbmn": "350000000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(
            market="J", direction="down", limit=5
        )

        assert result[0]["stck_shrn_iscd"] == "035420"

    async def test_foreign_buying_rank_api_params(self, monkeypatch):
        """foreign_buying_rank가 올바른 URL, tr_id, 파라미터로 API 호출하는지 검증"""
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().foreign_buying_rank(market="J", limit=5)

        assert len(captured_requests) == 1
        req = captured_requests[0]

        assert (
            req["url"]
            == f"https://openapi.koreainvestment.com:9443{FOREIGN_BUYING_RANK_URL}"
        )
        assert req["headers"]["tr_id"] == FOREIGN_BUYING_RANK_TR
        assert req["headers"]["authorization"] == "Bearer test_token"
        assert req["params"]["FID_COND_MRKT_DIV_CODE"] == "V"
        assert req["params"]["FID_COND_SCR_DIV_CODE"] == "16449"
        assert req["params"]["FID_INPUT_ISCD"] == "0000"
        assert req["params"]["FID_DIV_CLS_CODE"] == "0"
        assert req["params"]["FID_RANK_SORT_CLS_CODE"] == "0"
        assert req["params"]["FID_ETC_CLS_CODE"] == "1"

        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005930"

    async def test_market_cap_rank_limit_parameter(self, monkeypatch):
        """limit 파라미터가 정상적으로 적용되는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": f"{i:06d}",
                        "hts_kor_isnm": f"Stock{i}",
                        "stck_prpr": "80000",
                    }
                    for i in range(1, 11)
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().market_cap_rank(market="J", limit=3)

        assert len(result) == 3

    async def test_fluctuation_rank_multiple_items_sorted(self, monkeypatch):
        """fluctuation_rank가 여러 항목을 올바르게 정렬하는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "3.0",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "5.0",
                    },
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "stck_prpr": "70000",
                        "prdy_ctrt": "1.5",
                    },
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(
            market="J", direction="up", limit=10
        )

        assert len(result) == 3
        assert float(result[0]["prdy_ctrt"]) == 5.0
        assert float(result[1]["prdy_ctrt"]) == 3.0
        assert float(result[2]["prdy_ctrt"]) == 1.5

    async def test_api_error_handling(self, monkeypatch):
        """API 에러 시 RuntimeError가 발생하는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "1",
                "msg_cd": "ERROR",
                "msg1": "API Error occurred",
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        with pytest.raises(RuntimeError, match="API Error occurred"):
            await KISClient().market_cap_rank(market="J", limit=5)

    async def test_non_json_response_handling(self, monkeypatch):
        """Non-JSON 응답 시 RuntimeError가 발생하는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.side_effect = ValueError("Invalid JSON")
            mock_response.status_code = 500
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        with pytest.raises(RuntimeError, match="KIS API non-JSON response"):
            await KISClient().market_cap_rank(market="J", limit=5)

    async def test_api_error_without_msg1_fallback_to_msg_cd(self, monkeypatch):
        """API 에러 시 msg1이 없으면 msg_cd로 에러 메시지 생성하는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "1",
                "msg_cd": "E200",
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        with pytest.raises(RuntimeError, match="msg_cd=E200"):
            await KISClient().market_cap_rank(market="J", limit=5)

    async def test_token_expired_retry_egw00123(self, monkeypatch):
        """EGW00123(토큰 만료) 에러 발생 시 재시도 로직 검증"""
        call_count = {"get": 0, "clear_token": 0, "ensure_token": 0}
        token_value = "test_token"

        async def mock_get(_url, *args, **kwargs):
            call_count["get"] += 1
            mock_response = MagicMock()

            # 첫 번째 호출: 토큰 만료 에러
            if call_count["get"] == 1:
                mock_response.json.return_value = {
                    "rt_cd": "1",
                    "msg_cd": "EGW00123",
                    "msg1": "토큰이 만료되었습니다.",
                }
            # 두 번째 호출: 정상 응답
            else:
                mock_response.json.return_value = {
                    "rt_cd": "0",
                    "msg_cd": "",
                    "msg1": "",
                    "output": [
                        {
                            "stck_shrn_iscd": "005930",
                            "hts_kor_isnm": "삼성전자",
                            "stck_prpr": "80000",
                            "prdy_ctrt": "1.0",
                        }
                    ],
                }
            return mock_response

        async def mock_get_token():
            return token_value

        async def mock_clear_token():
            call_count["clear_token"] += 1

        async def mock_ensure_token():
            call_count["ensure_token"] += 1

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", token_value)
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        client = KISClient()
        monkeypatch.setattr(client._token_manager, "clear_token", mock_clear_token)
        monkeypatch.setattr(client, "_ensure_token", mock_ensure_token)

        result = await client.market_cap_rank(market="J", limit=5)

        # 재시도 확인: 총 2번 호출 (실패 1회 + 성공 1회)
        assert call_count["get"] == 2
        # 토큰 초기화 확인
        assert call_count["clear_token"] == 1
        # 새 토큰 확보 확인 (market_cap_rank는 3회 호출, 다른 메서드와 동작 차이)
        assert call_count["ensure_token"] == 3
        # 정상 결과 반환 확인
        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005930"

    async def test_token_invalid_retry_egw00121(self, monkeypatch):
        """EGW00121(유효하지 않은 토큰) 에러 발생 시 재시도 로직 검증"""
        call_count = {"get": 0, "clear_token": 0, "ensure_token": 0}
        token_value = "test_token"

        async def mock_get(_url, *args, **kwargs):
            call_count["get"] += 1
            mock_response = MagicMock()

            # 첫 번째 호출: 유효하지 않은 토큰 에러
            if call_count["get"] == 1:
                mock_response.json.return_value = {
                    "rt_cd": "1",
                    "msg_cd": "EGW00121",
                    "msg1": "유효하지 않은 토큰입니다.",
                }
            # 두 번째 호출: 정상 응답
            else:
                mock_response.json.return_value = {
                    "rt_cd": "0",
                    "msg_cd": "",
                    "msg1": "",
                    "output": [
                        {
                            "stck_shrn_iscd": "005380",
                            "hts_kor_isnm": "LG전자",
                            "stck_prpr": "120000",
                            "prdy_ctrt": "1.5",
                        }
                    ],
                }
            return mock_response

        async def mock_get_token():
            return token_value

        async def mock_clear_token():
            call_count["clear_token"] += 1

        async def mock_ensure_token():
            call_count["ensure_token"] += 1

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", token_value)
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        client = KISClient()
        monkeypatch.setattr(client._token_manager, "clear_token", mock_clear_token)
        monkeypatch.setattr(client, "_ensure_token", mock_ensure_token)

        result = await client.fluctuation_rank(market="J", direction="up", limit=5)

        # 재시도 확인: 총 2번 호출 (실패 1회 + 성공 1회)
        assert call_count["get"] == 2
        # 토큰 초기화 확인
        assert call_count["clear_token"] == 1
        # 새 토큰 확보 확인 (초기 호출 + 에러 후 재시도 + 재귀 호출 = 3회)
        assert call_count["ensure_token"] == 3
        # 정상 결과 반환 확인
        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005380"

    async def test_token_retry_multiple_methods(self, monkeypatch):
        """다른 ranking 메서드에서도 토큰 재발급 재시도가 동작하는지 검증"""
        call_count = {"volume": 0, "foreign": 0}
        token_value = "test_token"

        async def mock_get(self, _url, *args, **kwargs):
            mock_response = MagicMock()

            # volume_rank 첫 호출 실패 후 재시도
            if "volume-rank" in str(_url):
                call_count["volume"] += 1
                if call_count["volume"] == 1:
                    mock_response.json.return_value = {
                        "rt_cd": "1",
                        "msg_cd": "EGW00123",
                        "msg1": "토큰 만료",
                    }
                else:
                    mock_response.json.return_value = {
                        "rt_cd": "0",
                        "msg_cd": "",
                        "msg1": "",
                        "output": [{"stck_shrn_iscd": "005930"}],
                    }

            # foreign_buying_rank 첫 호출 실패 후 재시도
            elif "foreign-institution-total" in str(_url):
                call_count["foreign"] += 1
                if call_count["foreign"] == 1:
                    mock_response.json.return_value = {
                        "rt_cd": "1",
                        "msg_cd": "EGW00121",
                        "msg1": "유효하지 않은 토큰",
                    }
                else:
                    mock_response.json.return_value = {
                        "rt_cd": "0",
                        "msg_cd": "",
                        "msg1": "",
                        "output": [{"stck_shrn_iscd": "005930"}],
                    }

            return mock_response

        async def mock_get_token():
            return token_value

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", token_value)
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        client = KISClient()

        # volume_rank 테스트
        await client.volume_rank()
        assert call_count["volume"] == 2  # 실패 1회 + 성공 1회

        # foreign_buying_rank 테스트
        await client.foreign_buying_rank(market="J", limit=5)
        assert call_count["foreign"] == 2  # 실패 1회 + 성공 1회

    async def test_volume_rank_non_json_response_handling(self, monkeypatch):
        """volume_rank의 non-JSON 응답 처리를 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.side_effect = ValueError("Invalid JSON")
            mock_response.status_code = 500
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        with pytest.raises(RuntimeError, match="KIS API non-JSON response"):
            await KISClient().volume_rank(market="J", limit=5)

    async def test_volume_rank_api_error_without_msg1_fallback_to_msg_cd(
        self, monkeypatch
    ):
        """volume_rank의 API 에러 시 msg1이 없으면 msg_cd로 에러 메시지 생성하는지 검증"""

        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "1",
                "msg_cd": "E200",
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        with pytest.raises(RuntimeError, match="msg_cd=E200"):
            await KISClient().volume_rank(market="J", limit=5)

    async def test_volume_rank_with_market_and_limit(self, monkeypatch):
        """volume_rank의 market와 limit 파라미터가 적용되는지 검증"""
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                    for i in range(10)
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().volume_rank(market="K", limit=5)

        assert captured_requests[0]["params"]["FID_COND_MRKT_DIV_CODE"] == "K"
        assert len(result) == 5


@pytest.mark.asyncio
class TestKISRankingDirection:
    async def test_fluctuation_rank_up_direction(self, monkeypatch):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "5.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(market="J", direction="up", limit=5)

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["params"]["FID_RANK_SORT_CLS_CODE"] == "0"
        assert len(result) == 1

    async def test_fluctuation_rank_down_direction_sort_code(self, monkeypatch):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "stck_prpr": "70000",
                        "prdy_ctrt": "-3.0",
                        "acml_vol": "5000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(
            market="J", direction="down", limit=5
        )

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["params"]["FID_RANK_SORT_CLS_CODE"] == "3"
        assert req["params"]["FID_PRC_CLS_CODE"] == "0"
        assert len(result) == 1
        assert float(result[0]["prdy_ctrt"]) == -3.0

    async def test_fluctuation_rank_down_strict_negative_filtering(self, monkeypatch):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()

            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "stck_prpr": "70000",
                        "prdy_ctrt": "-3.0",
                        "acml_vol": "5000000",
                    },
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "60000",
                        "prdy_ctrt": "-1.5",
                        "acml_vol": "2000000",
                    },
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(
            market="J", direction="down", limit=5
        )

        assert len(captured_requests) == 1
        assert len(result) == 2
        assert all(float(r["prdy_ctrt"]) < 0 for r in result)
        assert float(result[0]["prdy_ctrt"]) == -3.0
        assert float(result[1]["prdy_ctrt"]) == -1.5

    async def test_fluctuation_rank_down_returns_empty_when_no_negatives(
        self, monkeypatch
    ):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                    },
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().fluctuation_rank(
            market="J", direction="down", limit=5
        )

        assert len(captured_requests) == 1
        assert len(result) == 0

    async def test_volume_rank_with_etf_excluded(self, monkeypatch):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "acml_vol": "10000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().volume_rank(market="J", limit=5)

        assert len(captured_requests) == 1
        assert len(result) == 1
        req = captured_requests[0]
        assert req["params"]["FID_TRGT_CLS_CODE"] == "11111111"
        assert req["params"]["FID_TRGT_EXLS_CLS_CODE"] == "0000001100"
