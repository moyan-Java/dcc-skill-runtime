"""本地知识索引、关键词检索和可选向量检索。"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional, Protocol, Sequence
from urllib import request


@dataclass(frozen=True)
class KnowledgeHit:
    """返回给编排层的知识片段。"""

    document_id: str
    title: str
    content: str
    source: str
    score: float


class EmbeddingProvider(Protocol):
    """Embedding 服务的最小接口。"""

    def embed(self, text: str) -> Sequence[float]:
        ...


class OpenAICompatibleEmbedding:
    """调用 OpenAI-compatible 的 Embedding HTTP 接口。"""

    def __init__(self, endpoint: str, model: str, api_key: str, timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/") + "/embeddings"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def embed(self, text: str) -> Sequence[float]:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(http_request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        vector = data.get("data", [{}])[0].get("embedding")
        if not isinstance(vector, list) or not all(isinstance(item, (int, float)) for item in vector):
            raise RuntimeError("Embedding 服务返回了无效向量")
        return tuple(float(item) for item in vector)


class KnowledgeIndex:
    """使用 SQLite 保存文档、FTS 索引和向量。"""

    def __init__(self, database_path: Path, provider: Optional[EmbeddingProvider] = None) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self._create_tables()

    def add_document(self, document_id: str, title: str, content: str, source: str) -> None:
        chunks = _split_content(content)
        with self._connection() as connection:
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM knowledge_fts WHERE document_id = ?", (document_id,))
            for index, chunk in enumerate(chunks):
                chunk_id = "%s:%s" % (document_id, index)
                vector_blob = b""
                if self.provider is not None:
                    try:
                        vector_blob = _pack_vector(self.provider.embed(chunk))
                    except Exception:
                        # 向量服务不可用时保留文档，搜索阶段使用关键词兜底。
                        vector_blob = b""
                connection.execute(
                    "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?)",
                    (chunk_id, document_id, title, chunk, source, vector_blob),
                )
                connection.execute(
                    "INSERT INTO knowledge_fts(chunk_id, document_id, title, content) "
                    "VALUES (?, ?, ?, ?)",
                    (chunk_id, document_id, title, chunk),
                )

    def search(self, query: str, limit: int = 5) -> tuple[KnowledgeHit, ...]:
        if not query.strip() or limit <= 0:
            return ()
        keyword_hits = self._keyword_hits(query, limit)
        vector_hits = self._vector_hits(query, limit) if self.provider is not None else ()
        merged: dict[str, KnowledgeHit] = {}
        for hit in keyword_hits + vector_hits:
            previous = merged.get(hit.document_id)
            if previous is None or hit.score > previous.score:
                merged[hit.document_id] = hit
        return tuple(sorted(merged.values(), key=lambda item: item.score, reverse=True)[:limit])

    def _keyword_hits(self, query: str, limit: int) -> tuple[KnowledgeHit, ...]:
        phrase = " OR ".join(part for part in query.split() if part)
        if not phrase:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.document_id, c.title, c.content, c.source,
                       bm25(knowledge_fts) AS rank
                FROM knowledge_fts f
                JOIN knowledge_chunks c ON c.chunk_id = f.chunk_id
                WHERE knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (phrase, limit),
            ).fetchall()
        return tuple(
            KnowledgeHit(row[0], row[1], row[2], row[3], 1.0 / (1.0 + abs(float(row[4]))))
            for row in rows
        )

    def _vector_hits(self, query: str, limit: int) -> tuple[KnowledgeHit, ...]:
        try:
            query_vector = tuple(float(item) for item in self.provider.embed(query))
        except Exception:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT document_id, title, content, source, vector FROM knowledge_chunks WHERE vector != ''"
            ).fetchall()
        scored = []
        for row in rows:
            vector = _unpack_vector(row[4])
            score = _cosine(query_vector, vector)
            scored.append(KnowledgeHit(row[0], row[1], row[2], row[3], score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return tuple(scored[:limit])

    def _create_tables(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    vector BLOB NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_id UNINDEXED,
                    title,
                    content
                );
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _split_content(content: str, size: int = 1200) -> tuple[str, ...]:
    normalized = "\n".join(line.rstrip() for line in content.splitlines()).strip()
    if not normalized:
        return ()
    return tuple(normalized[index : index + size] for index in range(0, len(normalized), size))


def _pack_vector(vector: Sequence[float]) -> bytes:
    values = tuple(float(item) for item in vector)
    return struct.pack("!I%sf" % len(values), len(values), *values)


def _unpack_vector(data: bytes) -> tuple[float, ...]:
    if not data:
        return ()
    length = struct.unpack("!I", data[:4])[0]
    return tuple(struct.unpack("!%sf" % length, data[4:]))


def _cosine(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or not first:
        return 0.0
    numerator = sum(left * right for left, right in zip(first, second))
    denominator = math.sqrt(sum(item * item for item in first)) * math.sqrt(
        sum(item * item for item in second)
    )
    return numerator / denominator if denominator else 0.0
