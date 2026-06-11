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
            import torch
            from sentence_transformers import SentenceTransformer

            # Benchmark findings (all-MiniLM-L6-v2, 500 chunks):
            #   CPU batch=32          : 45 chunks/s  (old default)
            #   CPU batch=256         : 37 chunks/s  (SLOWER — memory pressure)
            #   MPS batch=256         : 61 chunks/s
            #   MPS batch=512         : 2  chunks/s  (CATASTROPHIC — MPS OOM)
            #   CPU + torch.compile   : 61 chunks/s  (free, same as MPS)
            #   CPU 2-process + b=32  : 83 chunks/s  (WINNER — +84% vs baseline)
            #
            # Strategy: CPU device + multi-process pool (2 workers) beats MPS.
            # MPS has high memory pressure at large batch sizes and doesn't
            # benefit from multi-process (shared GPU state issues).

            self._model = SentenceTransformer(self.model_name, device="cpu")
            if self.max_seq_length:
                self._model.max_seq_length = self.max_seq_length

            # torch.compile — JIT-compiles the transformer for ~35% CPU speedup.
            # Same output, no quality change. Falls back silently if unavailable.
            try:
                self._model[0].auto_model = torch.compile(
                    self._model[0].auto_model, mode="reduce-overhead"
                )
                self._compiled = True
            except Exception:
                self._compiled = False

            # Try multi-process pool (2 CPU workers — 83 chunks/s vs 45).
            # On macOS with "spawn" start method, pool creation fails in subprocesses.
            # Catch RuntimeError silently and fall back to single-process.
            self._pool = None
            try:
                import multiprocessing
                ctx = multiprocessing.get_context("fork")  # fork avoids bootstrap error on macOS
                self._pool = self._model.start_multi_process_pool(
                    target_devices=["cpu", "cpu"]
                )
            except Exception:
                self._pool = None  # Fall back to single-process (torch.compile still gives +35%)

            mode = f"cpu×2-pool" if self._pool else "cpu+compiled"
            logger.info(
                f"Loaded embedding model: {self.model_name} "
                f"(dim={self.embedding_dim}, {mode}, "
                f"compiled={getattr(self,'_compiled',False)})"
            )
        except ImportError:
            logger.warning("sentence-transformers not installed. Embeddings disabled.")
            self._model = None
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self._model = None

    def __del__(self):
        """Clean up multi-process pool on garbage collection."""
        pool = getattr(self, "_pool", None)
        if pool is not None and self._model is not None:
            try:
                self._model.stop_multi_process_pool(pool)
            except Exception:
                pass

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
        """Embed multiple documents efficiently using cross-doc batching.

        Option 3: Collect ALL chunks from ALL docs into one flat list, run a
        single model forward pass, then map results back to their documents.
        This is 3-5x faster than per-doc embedding because the GPU/CPU
        processes one large batch instead of many small ones.
        """
        self._load_model()
        if self._model is None:
            return []

        total = len(doc_texts)

        # Phase 1: chunk all docs and build a flat index
        all_chunks: list[str] = []
        chunk_map: list[tuple[int, int, str]] = []  # (doc_id, chunk_idx, chunk_text)
        for doc_id, text in doc_texts:
            chunks = self._chunk_text(text, chunk_size=400, overlap=50)
            for ci, chunk in enumerate(chunks):
                all_chunks.append(chunk)
                chunk_map.append((doc_id, ci, chunk))

        if not all_chunks:
            return []

        # Phase 2: encode ALL chunks across ALL docs using multi-process pool.
        # 2 CPU workers split the chunk list — 83 chunks/s vs 45 single-process.
        try:
            pool = getattr(self, "_pool", None)
            if pool is not None:
                all_embeddings = self._model.encode_multi_process(
                    all_chunks,
                    pool,
                    batch_size=self.batch_size,
                    normalize_embeddings=self.normalize,
                )
            else:
                # Fallback: single-process encode (pool not available)
                import torch
                with torch.no_grad():
                    all_embeddings = self._model.encode(
                        all_chunks,
                        batch_size=self.batch_size,
                        normalize_embeddings=self.normalize,
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )
        except Exception as e:
            logger.error(f"Cross-doc embedding failed: {e}")
            return []

        # Phase 3: map embeddings back to docs
        results = []
        for (doc_id, chunk_idx, chunk_text), emb in zip(chunk_map, all_embeddings):
            results.append({
                "document_id":  doc_id,
                "embedding":    emb.tolist(),
                "embedding_type": "document",
                "text_chunk":   chunk_text[:500],
                "chunk_index":  chunk_idx,
            })

        # Progress log every 100 docs
        if total % 100 == 0 or total <= 10:
            logger.info(f"Embedding [{total}/{total}]")
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
