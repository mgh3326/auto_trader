#!/usr/bin/env python3
"""
JSON 분석 대시보드 테스트 스크립트
"""

import asyncio
import requests
import json
from datetime import datetime


def test_api_endpoints():
    """API 엔드포인트들을 테스트합니다."""
    base_url = "http://localhost:8000"
    
    print("=== JSON 분석 대시보드 API 테스트 ===\n")
    
    # 1. 필터 옵션 조회
    print("1. 필터 옵션 조회:")
    try:
        response = requests.get(f"{base_url}/analysis-json/api/filters")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ 상품 타입: {len(data.get('instrument_types', []))}개")
            print(f"   ✅ 종목 코드: {len(data.get('symbols', []))}개")
            print(f"   ✅ 모델명: {len(data.get('model_names', []))}개")
        else:
            print(f"   ❌ 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")
    
    print()
    
    # 2. 통계 정보 조회
    print("2. 통계 정보 조회:")
    try:
        response = requests.get(f"{base_url}/analysis-json/api/statistics")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ 전체 분석: {data.get('total_count', 0):,}건")
            print(f"   ✅ 평균 신뢰도: {data.get('average_confidence', 0)}%")
            print(f"   ✅ 투자 결정별 통계: {data.get('decision_counts', {})}")
        else:
            print(f"   ❌ 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")
    
    print()
    
            # 3. 분석 결과 조회
        print("3. 분석 결과 조회:")
        try:
            response = requests.get(f"{base_url}/analysis-json/api/results?page=1&page_size=5")
            if response.status_code == 200:
                data = response.json()
                print(f"   ✅ 전체 결과: {data.get('total_count', 0):,}건")
                print(f"   ✅ 현재 페이지: {data.get('page', 0)}/{data.get('total_pages', 0)}")
                print(f"   ✅ 페이지당 결과: {len(data.get('results', []))}건")
                
                # 첫 번째 결과 상세 정보
                if data.get('results'):
                    first_result = data['results'][0]
                    print(f"   ✅ 첫 번째 결과: {first_result.get('name', 'N/A')} ({first_result.get('symbol', 'N/A')})")
                    print(f"      - 결정: {first_result.get('decision', 'N/A')}")
                    print(f"      - 신뢰도: {first_result.get('confidence', 'N/A')}%")
        else:
            print(f"   ❌ 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")
    
    print()
    
    # 3-1. 투자 결정별 필터링 테스트
    print("3-1. 투자 결정별 필터링 테스트:")
    try:
        # 매수 추천만 조회
        response = requests.get(f"{base_url}/analysis-json/api/results?decision=buy&page=1&page_size=3")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ 매수 추천 필터: {data.get('total_count', 0):,}건")
        else:
            print(f"   ❌ 매수 추천 필터 실패: {response.status_code}")
            
        # 관망만 조회
        response = requests.get(f"{base_url}/analysis-json/api/results?decision=hold&page=1&page_size=3")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ 관망 필터: {data.get('total_count', 0):,}건")
        else:
            print(f"   ❌ 관망 필터 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")
    
    print()
    
    # 4. 상세 정보 조회 (첫 번째 결과가 있는 경우)
    print("4. 상세 정보 조회:")
    try:
        response = requests.get(f"{base_url}/analysis-json/api/results?page=1&page_size=1")
        if response.status_code == 200:
            data = response.json()
            if data.get('results'):
                first_result = data['results'][0]
                result_id = first_result.get('id')
                
                detail_response = requests.get(f"{base_url}/analysis-json/api/detail/{result_id}")
                if detail_response.status_code == 200:
                    detail_data = detail_response.json()
                    print(f"   ✅ 상세 정보 조회 성공: {detail_data.get('name', 'N/A')}")
                    print(f"      - 가격 분석: {len(detail_data.get('reasons', []))}개 근거")
                    print(f"      - 상세 텍스트 길이: {len(detail_data.get('detailed_text', ''))}자")
                else:
                    print(f"   ❌ 상세 정보 조회 실패: {detail_response.status_code}")
            else:
                print("   ⚠️ 조회할 결과가 없습니다.")
        else:
            print(f"   ❌ 결과 조회 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")
    
    print()
    
    # 5. 웹 페이지 접근 테스트
    print("5. 웹 페이지 접근:")
    try:
        response = requests.get(f"{base_url}/analysis-json/")
        if response.status_code == 200:
            print("   ✅ 대시보드 페이지 접근 성공")
            print(f"   ✅ HTML 길이: {len(response.text):,}자")
        else:
            print(f"   ❌ 실패: {response.status_code}")
    except Exception as e:
        print(f"   ❌ 오류: {e}")


def main():
    """메인 함수"""
    print("JSON 분석 대시보드 테스트를 시작합니다...")
    print("서버가 실행 중인지 확인하세요: http://localhost:8000")
    print()
    
    try:
        test_api_endpoints()
        print("\n=== 테스트 완료 ===")
        print("웹 브라우저에서 http://localhost:8000/analysis-json/ 에 접속하여 대시보드를 확인하세요.")
    except KeyboardInterrupt:
        print("\n테스트가 중단되었습니다.")
    except Exception as e:
        print(f"\n테스트 중 오류 발생: {e}")


if __name__ == "__main__":
    main()
