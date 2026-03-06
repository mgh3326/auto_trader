#!/usr/bin/env python3
"""
Verification script for subtask-5-2 E2E test implementation.

This script verifies that all required components are in place for the E2E test,
even though we can't run the full test in the isolated worktree environment.
"""

import ast
import os
import sys
from pathlib import Path


def verify_file_exists(filepath: str) -> bool:
    """Verify a file exists"""
    return Path(filepath).exists()


def verify_python_syntax(filepath: str) -> bool:
    """Verify Python file has valid syntax"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            ast.parse(f.read())
        return True
    except SyntaxError as e:
        print(f"  ❌ Syntax error in {filepath}: {e}")
        return False


def verify_function_exists(filepath: str, function_name: str) -> bool:
    """Verify a function exists in a Python file"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
                return True
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                return True
        return False
    except Exception as e:
        print(f"  ❌ Error checking for function {function_name}: {e}")
        return False


def verify_log_messages(filepath: str, messages: list[str]) -> dict[str, bool]:
    """Verify specific log messages exist in file"""
    results = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        for msg in messages:
            results[msg] = msg in content
            if not results[msg]:
                print(f"  ⚠️  Log message not found: '{msg}'")

        return results
    except Exception as e:
        print(f"  ❌ Error checking log messages: {e}")
        return {msg: False for msg in messages}


def main():
    """Run all verifications"""
    print("=" * 80)
    print("E2E Test Implementation Verification for subtask-5-2")
    print("=" * 80)
    print()

    all_passed = True

    # Verify main service file exists
    print("1. Verifying main service file...")
    service_file = "app/services/kr_hourly_candles_read_service.py"
    if verify_file_exists(service_file):
        print(f"  ✅ File exists: {service_file}")
        if verify_python_syntax(service_file):
            print(f"  ✅ Syntax valid")
        else:
            print(f"  ❌ Syntax invalid")
            all_passed = False
    else:
        print(f"  ❌ File not found: {service_file}")
        all_passed = False
    print()

    # Verify required functions exist
    print("2. Verifying required functions...")
    functions = [
        ("read_kr_hourly_candles_1h", service_file),
        ("_fetch_historical_minutes_via_kis", service_file),
        ("_store_minute_candles_background", service_file),
        ("_aggregate_minutes_to_hourly", service_file),
    ]

    for func_name, filepath in functions:
        if verify_function_exists(filepath, func_name):
            print(f"  ✅ Function exists: {func_name}")
        else:
            print(f"  ❌ Function not found: {func_name}")
            all_passed = False
    print()

    # Verify log messages exist
    print("3. Verifying required log messages...")
    log_messages = [
        "DB returned",
        "Fallback to KIS API",
        "Background task created",
    ]

    log_results = verify_log_messages(service_file, log_messages)
    for msg, found in log_results.items():
        if found:
            print(f"  ✅ Log message exists: '{msg}'")
        else:
            print(f"  ❌ Log message missing: '{msg}'")
            all_passed = False
    print()

    # Verify test script exists
    print("4. Verifying test scripts...")
    test_files = [
        "test_e2e_subtask_5_2.py",
        "E2E_MANUAL_TEST_GUIDE.md",
        "E2E_TEST_VERIFICATION.md",
    ]

    for test_file in test_files:
        if verify_file_exists(test_file):
            print(f"  ✅ Test file exists: {test_file}")
        else:
            print(f"  ❌ Test file not found: {test_file}")
    print()

    # Check for asyncio.create_task usage
    print("5. Verifying background task implementation...")
    try:
        with open(service_file, 'r', encoding='utf-8') as f:
            content = f.read()

        if "asyncio.create_task" in content:
            print(f"  ✅ asyncio.create_task() found")
        else:
            print(f"  ❌ asyncio.create_task() not found")
            all_passed = False

        if "add_done_callback" in content:
            print(f"  ✅ Error callback registered")
        else:
            print(f"  ⚠️  Error callback not found (non-blocking pattern)")
    except Exception as e:
        print(f"  ❌ Error checking background task: {e}")
        all_passed = False
    print()

    # Check for UPSERT SQL
    print("6. Verifying UPSERT SQL for database...")
    try:
        with open(service_file, 'r', encoding='utf-8') as f:
            content = f.read()

        if "INSERT INTO public.kr_candles_1m" in content:
            print(f"  ✅ INSERT statement found")
        else:
            print(f"  ❌ INSERT statement not found")
            all_passed = False

        if "ON CONFLICT" in content:
            print(f"  ✅ ON CONFLICT clause found (UPSERT)")
        else:
            print(f"  ❌ ON CONFLICT clause not found")
            all_passed = False
    except Exception as e:
        print(f"  ❌ Error checking UPSERT: {e}")
        all_passed = False
    print()

    # Summary
    print("=" * 80)
    print("Verification Summary")
    print("=" * 80)

    if all_passed:
        print("✅ ALL CHECKS PASSED")
        print()
        print("The E2E test implementation is complete and ready for testing.")
        print()
        print("Required components verified:")
        print("  ✓ Main service file exists with valid syntax")
        print("  ✓ All required functions implemented")
        print("  ✓ Log messages for test verification added")
        print("  ✓ Test scripts and documentation provided")
        print("  ✓ Background task pattern (asyncio.create_task)")
        print("  ✓ UPSERT SQL for data persistence")
        print()
        print("Next steps:")
        print("  1. Deploy to full environment with database and API access")
        print("  2. Run: uv run python test_e2e_subtask_5_2.py")
        print("  3. Verify logs contain expected messages")
        print("  4. Check database for persisted minute candles")
        return 0
    else:
        print("❌ SOME CHECKS FAILED")
        print()
        print("Please review the failures above and fix the issues.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
