#!/usr/bin/env python3
"""
tests/conftest.py
Pytest fixtures and configuration for TweetScrape test suite.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import pytest


# ============================================================================
# GLOBAL TEST CONFIGURATION
# ============================================================================


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow (requires network/browser)")
    config.addinivalue_line("markers", "integration: mark test as integration-level")


# ============================================================================
# TEMPORARY DIRECTORY FIXTURE
# ============================================================================


@pytest.fixture(scope="function")
def tmp_output_dir() -> Generator[Path, None, None]:
    tmpdir = tempfile.mkdtemp(prefix="tweetscrape_test_")
    path = Path(tmpdir)
    (path / "output").mkdir(exist_ok=True)
    try:
        yield path
    finally:
        try:
            shutil.rmtree(tmpdir)
        except PermissionError:
            pytest.skip(f"Could not cleanup {tmpdir}")


# ============================================================================
# MOCK GRAPHQL RESPONSE FACTORY
# ============================================================================


class MockResponseFactory:
    @staticmethod
    def valid_user_tweets(
        user_id: str = "44196397",
        screen_name: str = "elonmusk",
        tweet_count: int = 20,
        cursor: Optional[str] = "DAABCgABGNvF-5__-ggAAgAAAAIAAQAA",
        include_retweets: bool = True,
    ) -> Dict[str, Any]:
        entries = []
        for i in range(tweet_count):
            tweet_id = f"1{(10**17) + i}"
            is_retweet = include_retweets and (i % 5 == 0)

            tweet_data = {
                "__typename": "Tweet",
                "rest_id": tweet_id,
                "core": {
                    "user_results": {
                        "result": {
                            "__typename": "User",
                            "rest_id": user_id,
                            "legacy": {
                                "screen_name": screen_name,
                                "name": f"{screen_name.title()} Test",
                                "verified": True,
                            },
                        }
                    }
                },
                "legacy": {
                    "created_at": f"Wed Apr 16 12:{i:02d}:00 +0000 2024",
                    "conversation_id_str": tweet_id,
                    "display_text_range": [0, 280],
                    "entities": {"hashtags": [], "urls": [], "user_mentions": []},
                    "favorite_count": 1000 + i * 10,
                    "favorited": False,
                    "full_text": f"Test tweet #{i} with sufficient length to avoid truncation concerns.",
                    "is_quote_status": False,
                    "lang": "en",
                    "quote_count": 10 + i,
                    "reply_count": 5 + i,
                    "retweet_count": 50 + i * 2,
                    "retweeted": False,
                    "user_id_str": user_id,
                    "id_str": tweet_id,
                },
                "views": {"count": str(10000 + i * 100), "state": "EnabledWithCount"},
            }

            if is_retweet:
                tweet_data["legacy"]["retweeted_status_result"] = {
                    "result": {
                        "__typename": "Tweet",
                        "rest_id": f"9{(10**17) + i}",
                        "core": {
                            "user_results": {
                                "result": {
                                    "__typename": "User",
                                    "legacy": {"screen_name": "original_user"},
                                }
                            }
                        },
                        "legacy": {
                            "full_text": f"Original retweet text #{i}",
                            "favorite_count": 100,
                            "retweet_count": 50,
                        },
                    }
                }

            entries.append({"tweet_results": {"result": tweet_data}})

        if cursor:
            entries.append(
                {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": cursor}
            )

        return {
            "data": {
                "user": {
                    "result": {
                        "timeline_v2": {
                            "timeline": {
                                "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                            }
                        }
                    }
                }
            }
        }

    @staticmethod
    def empty_response() -> Dict[str, Any]:
        return {
            "data": {
                "user": {
                    "result": {
                        "timeline_v2": {
                            "timeline": {
                                "instructions": [{"type": "TimelineAddEntries", "entries": []}]
                            }
                        }
                    }
                }
            }
        }

    @staticmethod
    def rate_limited_response(retry_after: int = 30) -> Dict[str, Any]:
        return {"error": "RATE_LIMIT", "retry_after": retry_after, "message": "Rate limit exceeded"}


@pytest.fixture(scope="function")
def mock_factory() -> MockResponseFactory:
    return MockResponseFactory()


# ============================================================================
# TEST DATA HELPERS
# ============================================================================


@pytest.fixture
def sample_tweet_dict() -> Dict[str, Any]:
    return {
        "__typename": "Tweet",
        "rest_id": "1234567890123456789",
        "core": {
            "user_results": {
                "result": {
                    "__typename": "User",
                    "rest_id": "44196397",
                    "legacy": {
                        "screen_name": "elonmusk",
                        "name": "Elon Musk",
                    },
                }
            }
        },
        "legacy": {
            "created_at": "Wed Apr 16 12:00:00 +0000 2024",
            "full_text": "Test tweet content that is sufficiently long",
            "favorite_count": 1000,
            "retweet_count": 500,
            "reply_count": 100,
            "quote_count": 50,
            "lang": "en",
            "user_id_str": "44196397",
            "id_str": "1234567890123456789",
        },
        "views": {"count": "10000", "state": "EnabledWithCount"},
    }


@pytest.fixture
def sample_csv_valid(tmp_output_dir: Path) -> Path:
    import csv

    csv_path = tmp_output_dir / "output" / "test_valid.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "text", "created_at", "likes"])
        writer.writeheader()
        writer.writerow(
            {
                "id": "1234567890123456789",
                "text": "Valid tweet text",
                "created_at": "2024-04-16 12:00:00",
                "likes": "1000",
            }
        )
    return csv_path


# ============================================================================
# ASSERTION HELPERS (Quality Gates)
# ============================================================================


def assert_tweet_quality(tweet: Dict[str, Any], target_user: str):
    tweet_id = tweet.get("rest_id") or tweet.get("legacy", {}).get("id_str")
    assert isinstance(tweet_id, str), f"tweet_id must be string, got {type(tweet_id).__name__}"

    from config import extract_screen_name

    author = extract_screen_name(tweet)
    assert author.lower() == target_user.lower(), (
        f"Author mismatch: expected '{target_user}', got '{author}'"
    )

    legacy = tweet.get("legacy", {})
    full_text = legacy.get("full_text", "")
    has_note_tweet = "note_tweet" in tweet

    if not has_note_tweet:
        assert len(full_text) <= 280, (
            f"Unexpected long text without note_tweet: {len(full_text)} chars"
        )
    else:
        assert full_text or tweet.get("note_tweet", {}).get("text"), "Tweet has no text content"

    for field in ["favorite_count", "retweet_count", "reply_count", "quote_count"]:
        value = legacy.get(field)
        assert isinstance(value, (int, type(None))), (
            f"{field} must be int or None, got {type(value).__name__}: {value}"
        )

    created_at = legacy.get("created_at")
    assert created_at and isinstance(created_at, str), "created_at must be non-empty string"


def assert_csv_quality(csv_path: Path, expected_rows: int, target_user: str):
    import csv

    assert csv_path.exists(), f"CSV file not found: {csv_path}"

    with open(csv_path, "rb") as f:
        bom = f.read(3)
        assert bom == b"\xef\xbb\xbf", "CSV must start with UTF-8 BOM for Excel compatibility"

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == expected_rows, f"Expected {expected_rows} rows, got {len(rows)}"

    for i, row in enumerate(rows, start=2):
        tweet_id = row.get("tweet_id", "")
        assert "." not in tweet_id or not tweet_id.endswith(".0"), (
            f"Row {i}: ID has scientific notation: {tweet_id}"
        )

        screen_name = row.get("screen_name", "")
        assert screen_name.lower() == target_user.lower(), (
            f"Row {i}: Author mismatch: expected '{target_user}', got '{screen_name}'"
        )

        text = row.get("full_text", "")
        assert not text.endswith("…"), f"Row {i}: Text appears truncated: {text[:50]}..."
