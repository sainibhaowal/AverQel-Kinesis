"""Tests for output_retention utilities."""

from __future__ import annotations

import pytest

from app.deepspace.utils.output_retention import (
    # Classes
    ItemRetainer,
    ItemRetentionStrategy,
    OmittedExact,
    # Types
    OmittedNone,
    OmittedUnknown,
    PushDecision,
    RetainedItems,
    RetainedText,
    RetentionNotice,
    TextRetainer,
    TextRetentionStrategyHead,
    TextRetentionStrategyHeadTail,
    TextRetentionStrategyTail,
    # Factories
    create_item_retainer,
    create_text_retainer_head,
    create_text_retainer_head_tail,
    create_text_retainer_tail,
    # Formatters
    describe_omitted,
    format_retention_notice,
    truncate_without_splitting_surrogate_pair,
)


class TestOmittedTypes:
    """Tests for omission metadata types."""

    def test_omitted_none(self) -> None:
        o = OmittedNone()
        assert o.kind == "none"

    def test_omitted_exact(self) -> None:
        o = OmittedExact(count=5)
        assert o.kind == "exact"
        assert o.count == 5

    def test_omitted_unknown(self) -> None:
        o = OmittedUnknown()
        assert o.kind == "unknown"


class TestItemRetainer:
    """Tests for ItemRetainer (logical units)."""

    def test_zero_capacity(self) -> None:
        retainer = ItemRetainer(ItemRetentionStrategy(max_items=0))
        decision = retainer.push("item")
        assert decision == PushDecision(kept=False, truncated=True)
        result = retainer.finish()
        assert result.items == []
        assert result.truncated is True
        assert result.seen == 1
        assert result.kept == 0
        assert isinstance(result.omitted, OmittedExact)
        assert result.omitted.count == 1

    def test_within_capacity(self) -> None:
        retainer = ItemRetainer(ItemRetentionStrategy(max_items=3))
        for item in ["a", "b", "c"]:
            decision = retainer.push(item)
            assert decision == PushDecision(kept=True, truncated=False)
        result = retainer.finish()
        assert result.items == ["a", "b", "c"]
        assert result.truncated is False
        assert result.seen == 3
        assert result.kept == 3
        assert isinstance(result.omitted, OmittedNone)

    def test_exceeds_capacity(self) -> None:
        retainer = ItemRetainer(ItemRetentionStrategy(max_items=2))
        assert retainer.push("a") == PushDecision(kept=True, truncated=False)
        assert retainer.push("b") == PushDecision(kept=True, truncated=False)
        assert retainer.push("c") == PushDecision(kept=False, truncated=True)
        assert retainer.push("d") == PushDecision(kept=False, truncated=True)
        result = retainer.finish()
        assert result.items == ["a", "b"]
        assert result.truncated is True
        assert result.seen == 4
        assert result.kept == 2
        assert isinstance(result.omitted, OmittedExact)
        assert result.omitted.count == 2

    def test_continue_after_truncated(self) -> None:
        """Truncated flag stays true once omitted."""
        retainer = ItemRetainer(ItemRetentionStrategy(max_items=1))
        retainer.push("a")  # kept
        retainer.push("b")  # omitted, truncated=True
        result = retainer.finish()
        assert result.truncated is True

    def test_different_types(self) -> None:
        """Works with any type."""
        retainer = ItemRetainer[int](ItemRetentionStrategy(max_items=2))
        retainer.push(1)
        retainer.push(2)
        result = retainer.finish()
        assert result.items == [1, 2]


class TestTextRetainer:
    """Tests for TextRetainer (byte streams)."""

    # ---- Head Strategy ----

    def test_head_within_capacity(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=100))
        retainer.push(b"hello")
        retainer.push(b" world")
        result = retainer.finish()
        assert result.text == "hello world"
        assert result.truncated is False
        assert isinstance(result.omitted_bytes, OmittedNone)

    def test_head_exceeds_capacity(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=5))
        retainer.push(b"hello")
        retainer.push(b" world")
        result = retainer.finish()
        assert result.text == "hello"
        assert result.truncated is True
        assert isinstance(result.omitted_bytes, OmittedExact)
        assert result.omitted_bytes.count == 6  # " world" = 6 bytes

    def test_head_exact_capacity(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=5))
        retainer.push(b"hello")
        result = retainer.finish()
        assert result.text == "hello"
        assert result.truncated is False

    def test_head_string_input(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=10))
        retainer.push("hello")
        result = retainer.finish()
        assert result.text == "hello"

    def test_head_utf8_boundary(self) -> None:
        """Test UTF-8 boundary preservation at cut."""
        # "héllo" = h + é (2 bytes) + llo = 6 bytes
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=3))
        retainer.push("héllo")
        result = retainer.finish()
        # Should not produce replacement char at boundary
        assert "�" not in result.text
        # "hé" = 3 bytes (h + é)
        assert result.text == "hé"

    # ---- Tail Strategy ----

    def test_tail_within_capacity(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyTail(max_bytes=100))
        retainer.push(b"hello")
        retainer.push(b" world")
        result = retainer.finish()
        assert result.text == "hello world"
        assert result.truncated is False

    def test_tail_exceeds_capacity(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyTail(max_bytes=5))
        retainer.push(b"hello")
        retainer.push(b" world")
        result = retainer.finish()
        assert result.text == "world"
        assert result.truncated is True
        assert isinstance(result.omitted_bytes, OmittedExact)
        assert result.omitted_bytes.count == 6  # "hello " = 6 bytes

    def test_tail_utf8_boundary(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyTail(max_bytes=3))
        retainer.push("héllo")
        result = retainer.finish()
        assert "�" not in result.text
        # "llo" = 3 bytes, but "é" is 2 bytes so "lo" = 2 bytes or "llo" = 3
        # Actually "llo" = 3 bytes, so should get "llo"
        assert result.text == "llo"

    # ---- HeadTail Strategy ----

    def test_head_tail_both_within(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHeadTail(head_bytes=10, tail_bytes=10))
        retainer.push(b"hello world")
        result = retainer.finish()
        assert result.text == "hello world"
        assert result.truncated is False

    def test_head_tail_middle_omitted(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHeadTail(head_bytes=5, tail_bytes=5))
        retainer.push(b"0123456789abcdef")
        result = retainer.finish()
        # Should keep first 5 and last 5: "01234" + "bcdef"
        assert result.text == "01234bcdef"
        assert result.truncated is True
        assert isinstance(result.omitted_bytes, OmittedExact)
        assert result.omitted_bytes.count == 6  # "56789a" = 6 bytes

    def test_head_tail_utf8_boundary(self) -> None:
        """Test UTF-8 boundary at both cuts."""
        # "héllo world" = h + é(2) + llo world = 12 bytes
        retainer = TextRetainer(TextRetentionStrategyHeadTail(head_bytes=3, tail_bytes=3))
        retainer.push("héllo world")
        result = retainer.finish()
        assert "�" not in result.text
        # head: "hé" (3 bytes), tail: "ld" (2 bytes) or "rld" (3 bytes)
        # Actually depends on exact bytes

    def test_head_tail_no_gap_decoding(self) -> None:
        """When no gap, decode as contiguous."""
        retainer = TextRetainer(TextRetentionStrategyHeadTail(head_bytes=10, tail_bytes=10))
        retainer.push("hello")
        result = retainer.finish()
        assert result.text == "hello"
        assert result.truncated is False

    # ---- Multi-chunk ----

    def test_multiple_chunks(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=20))
        retainer.push(b"chunk1")
        retainer.push(b"chunk2")
        retainer.push(b"chunk3")
        result = retainer.finish()
        assert result.text == "chunk1chunk2chunk3"
        assert result.truncated is False

    def test_multiple_chunks_exceed(self) -> None:
        retainer = TextRetainer(TextRetentionStrategyHead(max_bytes=10))
        retainer.push(b"12345")
        retainer.push(b"67890")
        retainer.push(b"abcde")
        result = retainer.finish()
        assert result.text == "1234567890"
        assert result.truncated is True

    def test_suffix_sliding_window(self) -> None:
        """Test suffix chunks slide correctly."""
        retainer = TextRetainer(TextRetentionStrategyTail(max_bytes=10))
        # Push chunks that exceed suffix cap
        retainer.push(b"1234567890")  # 10 bytes
        retainer.push(b"abcdefghij")  # 10 bytes - should keep only last 10
        result = retainer.finish()
        assert result.text == "abcdefghij"
        assert result.truncated is True
        assert isinstance(result.omitted_bytes, OmittedExact)
        assert result.omitted_bytes.count == 10


class TestRetentionNoticeFormatting:
    """Tests for describe_omitted and format_retention_notice."""

    def test_describe_omitted_none(self) -> None:
        assert describe_omitted(OmittedNone(), "items") == ""

    def test_describe_omitted_exact(self) -> None:
        assert describe_omitted(OmittedExact(count=3), "items") == "Omitted 3 items."
        assert describe_omitted(OmittedExact(count=1), "bytes") == "Omitted 1 bytes."

    def test_describe_omitted_unknown(self) -> None:
        assert describe_omitted(OmittedUnknown(), "lines") == "More lines were omitted."

    def test_format_retention_notice_both(self) -> None:
        notice = RetentionNotice(
            scope="grep",
            strategy="head",
            unit="items",
            limit=10,
            kept=10,
            omitted=OmittedExact(count=5),
        )

        def recovery(n: RetentionNotice) -> str:
            return "Narrow the pattern to see more."

        result = format_retention_notice(notice, recovery)
        assert "Omitted 5 items." in result
        assert "Narrow the pattern" in result

    def test_format_retention_notice_only_recovery(self) -> None:
        notice = RetentionNotice(
            scope="bash",
            strategy="head",
            unit="bytes",
            limit=1000,
            kept=1000,
            omitted=OmittedNone(),
        )

        def recovery(n: RetentionNotice) -> str:
            return "Increase buffer size."

        result = format_retention_notice(notice, recovery)
        assert result == "Increase buffer size."

    def test_format_retention_notice_only_omission(self) -> None:
        notice = RetentionNotice(
            scope="web",
            strategy="head",
            unit="chars",
            limit=500,
            kept=500,
            omitted=OmittedExact(count=200),
        )

        def recovery(n: RetentionNotice) -> str:
            return ""

        result = format_retention_notice(notice, recovery)
        assert result == "Omitted 200 chars."


class TestTruncateSurrogatePair:
    """Tests for truncate_without_splitting_surrogate_pair."""

    def test_no_truncation_needed(self) -> None:
        text = "hello"
        assert truncate_without_splitting_surrogate_pair(text, 10) == "hello"

    def test_truncate_ascii(self) -> None:
        text = "hello world"
        assert truncate_without_splitting_surrogate_pair(text, 5) == "hello"

    def test_truncate_surrogate_pair_high(self) -> None:
        # 🌍 = U+1F30D = surrogate pair \uD83C\uDF0D
        text = "Hello 🌍 World"
        # Cap at 7 chars: "Hello " (6) + high surrogate of 🌍 (1) = would split pair
        result = truncate_without_splitting_surrogate_pair(text, 7)
        # Should drop the high surrogate
        assert result == "Hello "
        assert not (0xD800 <= ord(result[-1]) <= 0xDBFF)

    def test_truncate_surrogate_pair_low(self) -> None:
        text = "A🌍B"
        # Cap at 2: "A" + high surrogate
        result = truncate_without_splitting_surrogate_pair(text, 2)
        assert result == "A"

    def test_exact_at_pair_boundary(self) -> None:
        text = "A🌍B"
        # Cap at 3: "A" + high + low = complete pair
        result = truncate_without_splitting_surrogate_pair(text, 3)
        assert result == "A🌍"


class TestFactoryFunctions:
    """Tests for convenience factory functions."""

    def test_create_item_retainer(self) -> None:
        r = create_item_retainer(5)
        assert isinstance(r, ItemRetainer)
        r.push("a")
        result = r.finish()
        assert result.kept == 1

    def test_create_text_retainer_head(self) -> None:
        r = create_text_retainer_head(100)
        assert isinstance(r, TextRetainer)
        r.push(b"test")
        result = r.finish()
        assert result.text == "test"

    def test_create_text_retainer_tail(self) -> None:
        r = create_text_retainer_tail(100)
        assert isinstance(r, TextRetainer)
        r.push(b"test")
        result = r.finish()
        assert result.text == "test"

    def test_create_text_retainer_head_tail(self) -> None:
        r = create_text_retainer_head_tail(50, 50)
        assert isinstance(r, TextRetainer)
        r.push(b"hello")
        result = r.finish()
        assert result.text == "hello"


class TestPushDecision:
    """Tests for PushDecision."""

    def test_kept_not_truncated(self) -> None:
        d = PushDecision(kept=True, truncated=False)
        assert d.kept is True
        assert d.truncated is False

    def test_dropped_truncated(self) -> None:
        d = PushDecision(kept=False, truncated=True)
        assert d.kept is False
        assert d.truncated is True


class TestRetainedItems:
    """Tests for RetainedItems dataclass."""

    def test_fields(self) -> None:
        ri = RetainedItems(
            items=["a", "b"],
            truncated=True,
            seen=5,
            kept=2,
            omitted=OmittedExact(count=3),
        )
        assert ri.items == ["a", "b"]
        assert ri.truncated is True
        assert ri.seen == 5
        assert ri.kept == 2
        assert ri.omitted.count == 3


class TestRetainedText:
    """Tests for RetainedText dataclass."""

    def test_fields(self) -> None:
        rt = RetainedText(
            text="hello",
            truncated=True,
            omitted_bytes=OmittedExact(count=10),
        )
        assert rt.text == "hello"
        assert rt.truncated is True
        assert rt.omitted_bytes.count == 10


class TestRetentionNotice:
    """Tests for RetentionNotice dataclass."""

    def test_fields(self) -> None:
        rn = RetentionNotice(
            scope="grep",
            strategy="head",
            unit="items",
            limit=10,
            kept=10,
            omitted=OmittedExact(count=5),
        )
        assert rn.scope == "grep"
        assert rn.strategy == "head"
        assert rn.unit == "items"
        assert rn.limit == 10
        assert rn.kept == 10
        assert rn.omitted.count == 5


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_text_retainer(self) -> None:
        r = TextRetainer(TextRetentionStrategyHead(max_bytes=10))
        result = r.finish()
        assert result.text == ""
        assert result.truncated is False
        assert isinstance(result.omitted_bytes, OmittedNone)

    def test_zero_byte_capacity(self) -> None:
        r = TextRetainer(TextRetentionStrategyHead(max_bytes=0))
        r.push(b"hello")
        result = r.finish()
        assert result.text == ""
        assert result.truncated is True
        assert isinstance(result.omitted_bytes, OmittedExact)
        assert result.omitted_bytes.count == 5

    def test_item_retainer_zero_capacity(self) -> None:
        r = ItemRetainer(ItemRetentionStrategy(max_items=0))
        r.push("a")
        result = r.finish()
        assert result.items == []
        assert result.truncated is True
        assert result.omitted.count == 1

    def test_large_text_stream(self) -> None:
        """Test with larger stream."""
        r = TextRetainer(TextRetentionStrategyHead(max_bytes=100))
        for i in range(100):
            r.push(f"line{i}\n".encode())
        result = r.finish()
        assert len(result.text.encode("utf-8")) <= 100
        assert result.truncated is True

    def test_unicode_mixed(self) -> None:
        r = TextRetainer(TextRetentionStrategyHead(max_bytes=20))
        r.push("Hello 🌍 世界 Привет")
        result = r.finish()
        assert "�" not in result.text  # No replacement chars from boundary cuts
        assert len(result.text.encode()) <= 20


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
