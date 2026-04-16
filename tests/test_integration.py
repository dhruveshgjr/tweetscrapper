#!/usr/bin/env python3
"""
tests/test_integration.py
Integration tests for GraphQL response parsing and data quality validation.
"""

import csv
import json
from pathlib import Path
from typing import Dict, List

import pytest
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
export_csv = import_module_from_file("export_csv", "export_csv.py")

deduplicate = config.deduplicate
convert_json_to_csv = export_csv.convert_json_to_csv
extract_tweet_row = export_csv.extract_tweet_row
extract_tweets_from_response = collector.extract_tweets_from_response
extract_cursor_from_response = collector.extract_cursor_from_response
get_author_screen_name = collector.get_author_screen_name

from tests.conftest import assert_tweet_quality, assert_csv_quality, MockResponseFactory


class TestResponseParsingPipeline:
    def test_parse_valid_response_full_pipeline(self, mock_factory: MockResponseFactory):
        response = mock_factory.valid_user_tweets(
            user_id="44196397", screen_name="elonmusk", tweet_count=25, cursor="next_page_cursor"
        )

        tweets = extract_tweets_from_response(response)
        assert len(tweets) >= 25

    def test_author_filtering_in_extraction(self, mock_factory: MockResponseFactory):
        response = mock_factory.valid_user_tweets(
            user_id="44196397", screen_name="elonmusk", tweet_count=10
        )
        tweets = extract_tweets_from_response(response)
        assert len(tweets) >= 10

    def test_retweet_text_preservation(self, mock_factory: MockResponseFactory):
        response = mock_factory.valid_user_tweets(tweet_count=5, include_retweets=True)
        tweets = extract_tweets_from_response(response)
        assert len(tweets) >= 5

    def test_cursor_pagination_chain(self, mock_factory: MockResponseFactory):
        all_tweets = []
        cursors = ["cursor_page_1", "cursor_page_2", "cursor_page_3", None]

        for i, cursor in enumerate(cursors):
            response = mock_factory.valid_user_tweets(tweet_count=20, cursor=cursor)
            page_tweets = extract_tweets_from_response(response)
            all_tweets.extend(page_tweets)
            if page_tweets and cursor is None:
                break

        assert len(all_tweets) >= 20


class TestCSVExportQuality:
    def test_csv_export_basic(self, tmp_output_dir: Path, mock_factory: MockResponseFactory):
        response = mock_factory.valid_user_tweets(tweet_count=50)
        tweets = extract_tweets_from_response(response)

        json_path = tmp_output_dir / "output" / "test_export.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tweets, f, ensure_ascii=False)

        csv_path = json_path.with_suffix(".csv")
        report_path = json_path.with_suffix(".report.json")

        stats = convert_json_to_csv(json_path, csv_path, report_path, dedup=True)

        assert stats["output_rows"] >= 1
        assert csv_path.exists()

    def test_csv_utf8_bom_excel_compatibility(self, tmp_output_dir: Path, sample_csv_valid: Path):
        with open(sample_csv_valid, "rb") as f:
            first_bytes = f.read(3)
        assert first_bytes == b"\xef\xbb\xbf"

    def test_csv_no_scientific_notation(
        self, tmp_output_dir: Path, mock_factory: MockResponseFactory
    ):
        response = mock_factory.valid_user_tweets(tweet_count=10)
        tweets = extract_tweets_from_response(response)

        json_path = tmp_output_dir / "output" / "test_ids.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tweets, f, ensure_ascii=False)

        csv_path = json_path.with_suffix(".csv")
        convert_json_to_csv(
            json_path, csv_path, tmp_output_dir / "output" / "test_ids.report.json", dedup=True
        )

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tweet_id = row["tweet_id"]
                assert "." not in tweet_id or not tweet_id.endswith(".0"), (
                    f"Scientific notation detected: {tweet_id}"
                )

    def test_csv_deduplication(self, tmp_output_dir: Path, sample_tweet_dict: Dict):
        tweets = [sample_tweet_dict] * 5

        json_path = tmp_output_dir / "output" / "test_dedup.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tweets, f, ensure_ascii=False)

        csv_path = json_path.with_suffix(".csv")
        stats = convert_json_to_csv(
            json_path, csv_path, tmp_output_dir / "output" / "test_dedup.report.json", dedup=True
        )

        assert stats["output_rows"] == 1
        assert stats["input_rows"] == 5
        assert stats["dedup_dropped"] == 4


class TestRateLimitHandling:
    def test_detect_rate_limit_response(self, mock_factory: MockResponseFactory):
        rate_limit_meta = mock_factory.rate_limited_response(retry_after=45)
        assert rate_limit_meta["error"] == "RATE_LIMIT"
        assert rate_limit_meta["retry_after"] == 45


class TestExtractTweetRow:
    def test_extract_basic_tweet(self, sample_tweet_dict: Dict):
        row = extract_tweet_row(sample_tweet_dict)
        assert row is not None
        assert row["tweet_id"] == "1234567890123456789"
        assert "full_text" in row

    def test_extract_missing_id(self):
        tweet = {"legacy": {"full_text": "test"}}
        row = extract_tweet_row(tweet)
        assert row is None

    def test_extract_retweet(self, mock_factory: MockResponseFactory):
        response = mock_factory.valid_user_tweets(tweet_count=5, include_retweets=True)
        tweets = extract_tweets_from_response(response)
        retweet = [t for t in tweets if "retweeted_status_result" in t.get("legacy", {})][0]
        row = extract_tweet_row(retweet)
        assert row is not None
        assert row["is_retweet"] == 1

    def test_extract_empty_legacy(self):
        tweet = {"rest_id": "123", "legacy": {}}
        row = extract_tweet_row(tweet)
        assert row is not None
