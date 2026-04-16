#!/usr/bin/env python3
"""
tests/test_extractor.py
Unit tests for GraphQL response extraction functions.
"""

import pytest
from typing import Any, Dict

import sys
import importlib.util
from pathlib import Path

TESTS_DIR = Path(__file__).parent
PROJECT_DIR = TESTS_DIR.parent


def import_module_from_file(name, rel_path):
    path = PROJECT_DIR / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


config = import_module_from_file("config", "config.py")
collector = import_module_from_file("collector", "2_graphql_collector.py")

extract_screen_name = config.extract_screen_name
is_valid_tweet = config.is_valid_tweet
clean_tweet = config.clean_tweet
deduplicate = config.deduplicate
extract_tweets_from_response = collector.extract_tweets_from_response
extract_cursor_from_response = collector.extract_cursor_from_response
get_author_screen_name = collector.get_author_screen_name
is_tweet_endpoint = collector.is_tweet_endpoint


class TestExtractTweetsFromResponse:
    def test_extract_single_tweet(self, sample_tweet_dict: Dict[str, Any]):
        result = extract_tweets_from_response(sample_tweet_dict)
        assert len(result) == 1
        assert result[0]["rest_id"] == "1234567890123456789"

    def test_extract_nested_tweets(self, mock_factory):
        response = mock_factory.valid_user_tweets(tweet_count=5)
        result = extract_tweets_from_response(response)
        assert len(result) >= 5

    def test_extract_with_retweets(self, mock_factory):
        response = mock_factory.valid_user_tweets(tweet_count=10, include_retweets=True)
        result = extract_tweets_from_response(response)
        retweet_count = sum(1 for t in result if "retweeted_status_result" in t.get("legacy", {}))
        assert retweet_count >= 2

    def test_extract_empty_response(self, mock_factory):
        response = mock_factory.empty_response()
        result = extract_tweets_from_response(response)
        assert result == []

    def test_extract_malformed_gracefully(self):
        assert extract_tweets_from_response(None) == []
        assert extract_tweets_from_response("not a dict") == []
        assert extract_tweets_from_response(123) == []
        partial = {"data": {"user": {}}}
        result = extract_tweets_from_response(partial)
        assert isinstance(result, list)


class TestExtractCursorFromResponse:
    def test_extract_valid_cursor(self, mock_factory):
        response = mock_factory.valid_user_tweets(cursor="test_cursor_value_123")
        cursor = extract_cursor_from_response(response)
        assert cursor is None or "test_cursor_value_123" in str(cursor)

    def test_extract_no_cursor(self, mock_factory):
        response = mock_factory.valid_user_tweets(cursor=None)
        cursor = extract_cursor_from_response(response)
        assert cursor is None

    def test_extract_cursor_wrong_type(self, mock_factory):
        response = mock_factory.valid_user_tweets(cursor="bottom_cursor")
        cursor = extract_cursor_from_response(response)
        assert cursor is None or cursor is not None

    def test_extract_malformed_cursor_structure(self):
        malformed = {"data": {"user": {"result": {}}}}
        cursor = extract_cursor_from_response(malformed)
        assert cursor is None


class TestGetAuthorScreenName:
    def test_extract_standard_path(self, sample_tweet_dict: Dict[str, Any]):
        author = get_author_screen_name(sample_tweet_dict)
        assert author == "elonmusk"

    def test_extract_core_screen_name_direct(self):
        tweet = {
            "core": {
                "user_results": {
                    "result": {
                        "__typename": "User",
                        "core": {"screen_name": "direct_core_user"},
                        "legacy": {"screen_name": "legacy_user"},
                    }
                }
            }
        }
        author = get_author_screen_name(tweet)
        assert author == "direct_core_user"

    def test_extract_fallback_to_legacy(self):
        tweet = {
            "core": {
                "user_results": {
                    "result": {"__typename": "User", "legacy": {"screen_name": "fallback_user"}}
                }
            }
        }
        author = get_author_screen_name(tweet)
        assert author == "fallback_user"

    def test_extract_missing_author(self):
        assert get_author_screen_name({}) == ""
        assert get_author_screen_name({"core": {}}) == ""


class TestIsTweetEndpoint:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://x.com/i/api/graphql/abc123/UserTweets", True),
            ("https://x.com/i/api/graphql/def456/UserByScreenName", True),
            ("https://x.com/i/api/graphql/ghi789/UserMedia", True),
            ("https://x.com/i/api/graphql/jkl012/TweetDetail", True),
            ("https://x.com/i/api/graphql/mno345/SearchTimeline", False),
            ("https://x.com/i/api/graphql/pqr678/UserResult", True),
            ("https://x.com/i/api/graphql/stu901/UserLikes", True),
        ],
    )
    def test_endpoint_patterns(self, url: str, expected: bool):
        assert is_tweet_endpoint(url) == expected

    def test_case_insensitive_match(self):
        assert is_tweet_endpoint("https://x.com/i/api/graphql/abc/UserTweets") == True


class TestConfigExtractScreenName:
    def test_standard_path(self, sample_tweet_dict: Dict[str, Any]):
        author = extract_screen_name(sample_tweet_dict)
        assert author == "elonmusk"

    def test_fallback_path(self):
        tweet = {"core": {"user_results": {"result": {"legacy": {"screen_name": "fallback"}}}}}
        assert extract_screen_name(tweet) == "fallback"

    def test_empty(self):
        assert extract_screen_name({}) == ""


class TestConfigIsValidTweet:
    def test_valid_tweet(self, sample_tweet_dict: Dict[str, Any]):
        assert is_valid_tweet(sample_tweet_dict) == True

    def test_invalid_typename(self):
        tweet = {"__typename": "NotTweet", "legacy": {"full_text": "test"}}
        assert is_valid_tweet(tweet) == False

    def test_missing_text(self):
        tweet = {"__typename": "Tweet", "legacy": {}}
        assert is_valid_tweet(tweet) == False

    def test_missing_id(self):
        tweet = {"__typename": "Tweet", "legacy": {"full_text": "test"}}
        assert is_valid_tweet(tweet) == False


class TestConfigCleanTweet:
    def test_basic_clean(self, sample_tweet_dict: Dict[str, Any]):
        cleaned = clean_tweet(sample_tweet_dict)
        assert cleaned["id"] == "1234567890123456789"
        assert cleaned["screen_name"] == "elonmusk"
        assert "text" in cleaned

    def test_newline_replacement(self):
        tweet = {
            "rest_id": "123",
            "legacy": {
                "full_text": "Line1\nLine2\nLine3",
                "created_at": "Wed Apr 16 12:00:00 +0000 2024",
                "favorite_count": 0,
                "retweet_count": 0,
                "reply_count": 0,
                "quote_count": 0,
                "lang": "en",
            },
            "core": {"user_results": {"result": {"legacy": {"screen_name": "test"}}}},
        }
        cleaned = clean_tweet(tweet)
        assert "\n" not in cleaned["text"]


class TestConfigDeduplicate:
    def test_dedupe_by_id(self):
        tweets = [
            {"rest_id": "1", "text": "a"},
            {"rest_id": "2", "text": "b"},
            {"rest_id": "1", "text": "c"},
        ]
        result = deduplicate(tweets)
        assert len(result) == 2
        assert result[0]["rest_id"] == "1"
        assert result[1]["rest_id"] == "2"

    def test_dedupe_empty(self):
        assert deduplicate([]) == []
