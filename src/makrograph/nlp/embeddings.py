"""Semantic embedding generation using Sentence Transformers.

Default model: all-MiniLM-L6-v2 (384-dim, fast, good quality)
Financial option: ProsusAI/finbert (768-dim, finance-tuned)

Supports:
    - Single text embedding
    - Batch embedding (efficient)
    - Document chunking for long texts
    - Similarity computation
"""

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "all-MiniLM-L6-v2"
CHUNK_SIZE = 512       # tokens approximate
CHUNK_OVERLAP = 64


class EmbeddingEngine:
    """Generates semantic embeddings for text using Sentence Transformers."""

    def __init__(self, config: dict):
        self.model_name = config.get("embedding_model", DEFAULT_MODEL)
        self.batch_size = config.get("embedding_batch_size", 32)
        self.max_seq_length = config.get("max_seq_length", 512)
        self.normalize = config.get("normalize_embeddings", True)
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            if self.max_seq_length:
                self._model.max_seq_length = self.max_seq_length
            logger.info(f"Loaded embedding model: {self.model_name} (dim={self.embedding_dim})")
        except ImportError:
            logger.warning("sentence-transformers not installed. Embeddings disabled.")
            self._model = None
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self._model = None

    @property
    def embedding_dim(self) -> int:
        dim_map = {
            "all-MiniLM-L6-v2": 384,
            "all-mpnet-base-v2": 768,
            "ProsusAI/finbert": 768,
            "sentence-transformers/all-distilroberta-v1": 768,
        }
        return dim_map.get(self.model_name, 384)

    @property
    def is_available(self) -> bool:
        self._load_model()
        return self._model is not None

    def embed(self, text: str) -> Optional[list[float]]:
        """Embed a single text string."""
        result = self.embed_batch([text])
        return result[0] if result else None

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed a batch of texts efficiently."""
        self._load_model()
        if self._model is None:
            return [None] * len(texts)

        try:
            import torch
            with torch.no_grad():
                embeddings = self._model.encode(
                    texts,
                    batch_size=self.batch_size,
                    normalize_embeddings=self.normalize,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
            return [e.tolist() for e in embeddings]
        except Exception as e:
            logger.error(f"Embedding batch failed: {e}")
            return [None] * len(texts)

    def embed_document(self, text: str, strategy: str = "mean_chunks") -> Optional[list[float]]:
        """Embed a full document, handling texts longer than max_seq_length.

        Strategies:
            mean_chunks  - embed chunks and average (default)
            first_chunk  - embed first chunk only (fast)
            summary      - embed first 512 tokens (executive summary)
        """
        if not text:
            return None

        if strategy == "first_chunk" or strategy == "summary":
            truncated = " ".join(text.split()[:400])
            return self.embed(truncated)

        chunks = self._chunk_text(text, chunk_size=400, overlap=50)
        if not chunks:
            return None

        embeddings = self.embed_batch(chunks)
        valid = [e for e in embeddings if e is not None]
        if not valid:
            return None

        # Mean pooling
        arr = np.mean(valid, axis=0)
        if self.normalize:
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
        return arr.tolist()

    def embed_documents_batch(
        self,
        doc_texts: list[tuple[int, str]],
        strategy: str = "mean_chunks",
    ) -> list[dict]:
        """Embed multiple documents. Returns list of {doc_id, embedding, chunk_count}."""
        results = []
        total = len(doc_texts)
        for i, (doc_id, text) in enumerate(doc_texts, 1):
            if i % 100 == 0:
                logger.info(f"Embedding [{i}/{total}]")
            chunks = self._chunk_text(text, chunk_size=400, overlap=50)
            if not chunks:
                continue
            embeddings = self.embed_batch(chunks)
            valid = [(j, e) for j, e in enumerate(embeddings) if e is not None]
            for chunk_idx, emb in valid:
                results.append({
                    "document_id": doc_id,
                    "embedding": emb,
                    "embedding_type": "document",
                    "text_chunk": chunks[chunk_idx][:500],
                    "chunk_index": chunk_idx,
                })
        logger.info(f"Generated {len(results)} embedding records for {total} docs")
        return results

    def _chunk_text(self, text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
        """Split text into overlapping word-level chunks."""
        words = text.split()
        if not words:
            return []
        chunks = []
        start = 0
        while start < len(words):
            end = min(start + chunk_size, len(words))
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            if end >= len(words):
                break
            start += chunk_size - overlap
        return chunks

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two embedding vectors."""
        arr_a = np.array(a)
        arr_b = np.array(b)
        norm_a = np.linalg.norm(arr_a)
        norm_b = np.linalg.norm(arr_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(arr_a, arr_b) / (norm_a * norm_b))

    def cluster_embeddings(
        self, embeddings: list[list[float]], n_clusters: int = 10
    ) -> list[int]:
        """K-means clustering of embedding vectors. Returns cluster labels."""
        try:
            from sklearn.cluster import KMeans
            arr = np.array(embeddings)
            km = KMeans(n_clusters=min(n_clusters, len(embeddings)), random_state=42, n_init=10)
            labels = km.fit_predict(arr)
            return labels.tolist()
        except ImportError:
            logger.warning("scikit-learn not installed. Clustering unavailable.")
            return [0] * len(embeddings)
        except Exception as e:
            logger.error(f"Clustering failed: {e}")
            return [0] * len(embeddings)
