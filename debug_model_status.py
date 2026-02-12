import asyncio

from app.core.model_rate_limiter import ModelRateLimiter


async def main():
    """모델 상태 확인 및 관리"""
    rate_limiter = ModelRateLimiter()

    try:
        # 모든 모델의 상태 확인
        models = ["gemini-2.5-pro", "gemini-2.5-flash"]

        print("=== 모델 상태 확인 ===")
        for model in models:
            status = await rate_limiter.get_model_status(model)
            print(f"\n{model}:")
            for key, value in status.items():
                if key == 'api_keys' and isinstance(value, list):
                    print(f"  {key}:")
                    for api_key_status in value:
                        print(f"    - {api_key_status}")
                else:
                    print(f"  {key}: {value}")

        print("\n" + "="*50)

        # 전체 제한 상태 확인
        print("\n=== 전체 제한 상태 ===")
        all_status = await rate_limiter.get_all_rate_limits()
        if 'error' not in all_status:
            print(f"총 제한된 API 키 수: {all_status['total_limited']}")
            for model_name, model_info in all_status['models'].items():
                print(f"\n{model_name}:")
                for api_key_status in model_info['api_keys']:
                    print(f"  - {api_key_status}")
        else:
            print(f"전체 상태 조회 오류: {all_status['error']}")

        print("\n" + "="*50)

        # 사용자 입력으로 모델 제한 해제
        while True:
            print("\n=== 모델 제한 관리 ===")
            print("1. 특정 모델의 특정 API 키 제한 해제")
            print("2. 특정 모델의 모든 API 키 제한 해제")
            print("3. 모든 모델의 모든 API 키 제한 해제")
            print("4. 모델 상태 다시 확인")
            print("5. 전체 제한 상태 확인")
            print("6. 종료")

            choice = input("\n선택하세요 (1-6): ").strip()

            if choice == "1":
                model_name = input("제한을 해제할 모델명을 입력하세요: ").strip()
                api_key = input("제한을 해제할 API 키를 입력하세요 (마스킹된 형태): ").strip()
                if model_name and api_key:
                    success = await rate_limiter.clear_model_rate_limit(model_name, api_key)
                    if success:
                        print(f"✅ {model_name} 모델 (API: {api_key}) 제한 해제 완료")
                    else:
                        print(f"❌ {model_name} 모델 (API: {api_key}) 제한 해제 실패")

            elif choice == "2":
                model_name = input("제한을 해제할 모델명을 입력하세요: ").strip()
                if model_name:
                    success = await rate_limiter.clear_model_rate_limit(model_name)
                    if success:
                        print(f"✅ {model_name} 모델의 모든 API 키 제한 해제 완료")
                    else:
                        print(f"❌ {model_name} 모델 제한 해제 실패")

            elif choice == "3":
                print("모든 모델의 모든 API 키 제한을 해제합니다...")
                for model in models:
                    await rate_limiter.clear_model_rate_limit(model)
                print("✅ 모든 모델의 모든 API 키 제한 해제 완료")

            elif choice == "4":
                print("\n=== 모델 상태 재확인 ===")
                for model in models:
                    status = await rate_limiter.get_model_status(model)
                    print(f"\n{model}:")
                    for key, value in status.items():
                        if key == 'api_keys' and isinstance(value, list):
                            print(f"  {key}:")
                            for api_key_status in value:
                                print(f"    - {api_key_status}")
                        else:
                            print(f"  {key}: {value}")

            elif choice == "5":
                print("\n=== 전체 제한 상태 재확인 ===")
                all_status = await rate_limiter.get_all_rate_limits()
                if 'error' not in all_status:
                    print(f"총 제한된 API 키 수: {all_status['total_limited']}")
                    for model_name, model_info in all_status['models'].items():
                        print(f"\n{model_name}:")
                        for api_key_status in model_info['api_keys']:
                            print(f"  - {api_key_status}")
                else:
                    print(f"전체 상태 조회 오류: {all_status['error']}")

            elif choice == "6":
                print("프로그램을 종료합니다.")
                break

            else:
                print("잘못된 선택입니다. 1-6 중에서 선택하세요.")

    finally:
        await rate_limiter.close()


if __name__ == "__main__":
    asyncio.run(main())
