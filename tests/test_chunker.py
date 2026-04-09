"""Tests for content chunking utilities."""

import json

from omnix.resolver.chunker import chunk_text, chunk_json_array


class TestChunkText:
    def test_small_text_single_chunk(self):
        text = "Hello world. This is short."
        chunks = chunk_text(text, max_chars=3000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_large_text_splits(self):
        sentences = [f"Sentence number {i} is here." for i in range(100)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=200)
        assert len(chunks) > 1
        # All content should be represented
        combined = " ".join(chunks)
        for s in sentences:
            assert s in combined

    def test_splits_into_multiple_chunks(self):
        sentences = [f"This is sentence {i}." for i in range(50)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=100, overlap=0)
        assert len(chunks) > 1

    def test_empty_text(self):
        chunks = chunk_text("")
        assert chunks == [""]

    def test_overlap_provides_context(self):
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        chunks = chunk_text(text, max_chars=40, overlap=20)
        if len(chunks) > 1:
            # Second chunk should contain overlap from first
            assert len(chunks[1]) > 0


class TestChunkJsonArray:
    def test_small_array_single_chunk(self):
        data = [{"id": i} for i in range(10)]
        content = json.dumps(data)
        chunks = chunk_json_array(content, batch_size=50)
        assert len(chunks) == 1

    def test_large_array_splits(self):
        data = [{"id": i, "name": f"item_{i}"} for i in range(120)]
        content = json.dumps(data)
        chunks = chunk_json_array(content, batch_size=50)
        assert len(chunks) == 3  # 50 + 50 + 20

        # Verify all items present
        all_items = []
        for chunk in chunks:
            all_items.extend(json.loads(chunk))
        assert len(all_items) == 120

    def test_non_array_single_chunk(self):
        content = json.dumps({"key": "value"})
        chunks = chunk_json_array(content)
        assert len(chunks) == 1
        assert chunks[0] == content

    def test_invalid_json(self):
        chunks = chunk_json_array("not json at all")
        assert len(chunks) == 1
        assert chunks[0] == "not json at all"

    def test_empty_array(self):
        chunks = chunk_json_array("[]")
        assert len(chunks) == 1
