#!/usr/bin/env python3
"""
tests/run_tests_healing.py
Self-healing test runner for overnight AI agent execution.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
OUTPUT_DIR = PROJECT_ROOT / "output"
REPORTS_DIR = OUTPUT_DIR / "test_reports"


class FailureType(Enum):
    AUTH_EXPIRED = auto()
    RATE_LIMITED = auto()
    SCHEMA_CHANGED = auto()
    DATA_QUALITY = auto()
    NETWORK_ERROR = auto()
    UNKNOWN = auto()


class HealingStrategy:
    STRATEGIES = {
        FailureType.AUTH_EXPIRED: {
            "action": "re_authenticate",
            "description": "Re-run 1_authenticator.py to refresh session",
            "commands": [["python3", "1_authenticator.py"]],
            "wait_seconds": 30,
            "requires_user": True,
        },
        FailureType.RATE_LIMITED: {
            "action": "increase_backoff",
            "description": "Increase rate limit backoff parameters",
            "config_updates": {
                "REQUEST_DELAY_SECONDS": 3.0,
            },
            "wait_seconds": 60,
            "requires_user": False,
        },
        FailureType.SCHEMA_CHANGED: {
            "action": "update_extractors",
            "description": "Update GraphQL extraction paths (requires code change)",
            "note": "This requires manual intervention; log for review",
            "requires_user": True,
        },
        FailureType.DATA_QUALITY: {
            "action": "adjust_filters",
            "description": "Relax or tighten data quality filters",
            "requires_user": False,
        },
        FailureType.NETWORK_ERROR: {
            "action": "retry_with_backoff",
            "description": "Wait and retry with exponential backoff",
            "wait_seconds": 120,
            "max_retries": 3,
            "requires_user": False,
        },
    }

    @classmethod
    def get_strategy(cls, failure_type: FailureType) -> Optional[Dict]:
        return cls.STRATEGIES.get(failure_type)


def classify_failure(test_output: str, test_name: str) -> FailureType:
    output_lower = test_output.lower()

    if any(
        p in output_lower
        for p in [
            "authentication failed",
            "invalid token",
            "ct0 not found",
            "auth_token missing",
            "session expired",
            "401 unauthorized",
        ]
    ):
        return FailureType.AUTH_EXPIRED
    if any(p in output_lower for p in ["429", "rate limit", "too many requests", "retry-after"]):
        return FailureType.RATE_LIMITED
    if any(
        p in output_lower
        for p in [
            "keyerror",
            "attributeerror",
            "noneType",
            "expected dict got none",
            "timeline_v2",
            "instructions",
        ]
    ):
        return FailureType.SCHEMA_CHANGED
    if any(
        p in output_lower
        for p in [
            "assertionerror",
            "author mismatch",
            "truncat",
            "scientific notation",
            "missing field",
        ]
    ):
        return FailureType.DATA_QUALITY
    if any(
        p in output_lower
        for p in ["connection refused", "timeout", "ssl error", "dns", "network unreachable"]
    ):
        return FailureType.NETWORK_ERROR

    return FailureType.UNKNOWN


def run_pytest_suite(
    target_user: str,
    markers: Optional[List[str]] = None,
    verbose: bool = False,
) -> Tuple[bool, str, Dict]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(TESTS_DIR),
        "-v" if verbose else "-q",
        "--tb=short",
    ]

    if markers:
        for marker in markers:
            cmd.extend(["-m", marker])

    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        success = result.returncode == 0
        return success, result.stderr, {}
    except subprocess.TimeoutExpired:
        return False, "Test suite timed out after 300 seconds", {}
    except FileNotFoundError:
        return False, "pytest not found; install with: pip install pytest", {}
    except Exception as e:
        return False, f"Failed to run tests: {type(e).__name__}: {e}", {}


def apply_healing_strategy(
    failure_type: FailureType, strategy: Dict, dry_run: bool = False
) -> bool:
    action = strategy["action"]
    log_prefix = f"[HEAL:{action}]"
    print(f"{log_prefix} {strategy['description']}")

    if dry_run:
        print(f"{log_prefix} (dry-run) Would execute:")
        if "commands" in strategy:
            for cmd in strategy["commands"]:
                print(f"  $ {' '.join(cmd)}")
        return True

    if "commands" in strategy:
        for cmd in strategy["commands"]:
            print(f"{log_prefix} Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=strategy.get("command_timeout", 120),
            )
            if result.returncode != 0:
                print(f"{log_prefix} Command failed: {result.stderr[:200]}")
                return False

    if "config_updates" in strategy:
        config_path = PROJECT_ROOT / "config.py"
        if config_path.exists():
            print(f"{log_prefix} Updating config.py...")
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, value in strategy["config_updates"].items():
                if f"{key} = " in content:
                    content = content.replace(
                        f"{key} = {content.split(f'{key} = ')[1].split('\n')[0]}",
                        f"{key} = {value}",
                    )
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)

    wait_seconds = strategy.get("wait_seconds", 0)
    if wait_seconds > 0:
        print(f"{log_prefix} Waiting {wait_seconds}s...")
        if not strategy.get("requires_user"):
            time.sleep(wait_seconds)
        else:
            print(f"{log_prefix} Manual action required; press Enter to continue")
            input()

    return True


def generate_healing_report(
    attempt: int,
    failure_type: Optional[FailureType],
    strategy_applied: bool,
    test_result: bool,
    error_output: str,
    report_path: Path,
) -> Dict:
    report = {
        "timestamp": datetime.now().isoformat(),
        "healing_attempt": attempt,
        "failure_type": failure_type.name if failure_type else None,
        "strategy_applied": strategy_applied,
        "test_passed_after_heal": test_result,
        "error_snippet": error_output[:500] if error_output else None,
        "next_action": "PROCEED" if test_result else "RETRY_OR_ESCALATE",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report


def main():
    parser = argparse.ArgumentParser(description="Self-healing test runner for TweetScrape")
    parser.add_argument("--target-user", default="elonmusk")
    parser.add_argument(
        "--max-tweets", type=int, default=3000, help="Target tweet count for collection"
    )
    parser.add_argument("--max-heal-attempts", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--skip-slow", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.report_only:
        latest_report = REPORTS_DIR / "latest_heal_report.json"
        if latest_report.exists():
            with open(latest_report, "r", encoding="utf-8") as f:
                report = json.load(f)
            print(json.dumps(report, indent=2))
        else:
            print("No healing report found")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"TweetScrape Self-Healing Test Runner")
    print(f"   Target: @{args.target_user}")
    print(f"   Max tweets: {args.max_tweets}")
    print(f"   Max healing attempts: {args.max_heal_attempts}")
    print(f"   Dry run: {args.dry_run}")
    print("-" * 60)

    markers = ["not slow"] if args.skip_slow else None
    success = False

    for attempt in range(1, args.max_heal_attempts + 1):
        print(f"\n[CYCLE {attempt}/{args.max_heal_attempts}] Running test suite...")
        success, error_output, json_report = run_pytest_suite(
            target_user=args.target_user,
            markers=markers,
            verbose=args.verbose,
        )

        if success:
            print(f"All tests passed on attempt {attempt}")
            report = generate_healing_report(
                attempt=attempt,
                failure_type=None,
                strategy_applied=False,
                test_result=True,
                error_output="",
                report_path=REPORTS_DIR / "latest_heal_report.json",
            )
            return 0

        print(f"Tests failed. Analyzing failures...")
        failure_type = classify_failure(error_output, "unknown_test")
        print(f"Classified failure: {failure_type.name}")

        strategy = HealingStrategy.get_strategy(failure_type)
        if not strategy:
            print(f"No healing strategy for {failure_type.name}; manual review required")
            break

        print(f"Applying healing strategy: {strategy['action']}")
        strategy_success = apply_healing_strategy(failure_type, strategy, dry_run=args.dry_run)

        if not strategy_success:
            print(f"Healing strategy failed; escalating")
            break

        report_path = REPORTS_DIR / f"heal_attempt_{attempt}.json"
        generate_healing_report(
            attempt=attempt,
            failure_type=failure_type,
            strategy_applied=strategy_success,
            test_result=False,
            error_output=error_output,
            report_path=report_path,
        )

        if not args.dry_run and strategy.get("requires_user"):
            print(f"Manual intervention required for {strategy['action']}")
            print(f"After completing manual steps, press Enter to re-run tests...")
            input()

    print(f"\n{'=' * 60}")
    print(f"Healing cycles complete")
    print(f"   Final status: {'READY' if success else 'NEEDS REVIEW'}")
    print(f"   Reports: {REPORTS_DIR}")
    print(f"{'=' * 60}")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
