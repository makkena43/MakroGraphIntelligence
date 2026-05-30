"""BERTopic-based topic modeling for financial documents.

Discovers latent topics across SEC filings and earnings calls,
tracks topic emergence over time, and feeds topic clusters
into the theme detection engine.
"""

import logging
import pickle
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TopicResult:
    """Output from topic modeling on a corpus of documents."""
    topic_id: int
    label: str
    top_words: list[str]
    top_ngrams: list[str]
    doc_count: int
    coherence_score: float = 0.0
    is_emerging: bool = False
    representative_docs: list[str] = field(default_factory=list)
    run_date: Optional[date] = None

    def __post_init__(self):
        if self.run_date is None:
            self.run_date = date.today()

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "top_words": self.top_words,
            "top_ngrams": self.top_ngrams,
            "doc_count": self.doc_count,
            "coherence_score": self.coherence_score,
            "is_emerging": self.is_emerging,
            "run_date": str(self.run_date),
        }


class TopicModeler:
    """BERTopic-based topic discovery on financial text corpus.

    Features:
        - Configurable number of topics (min_topic_size, nr_topics)
        - Custom financial stop words
        - Topic evolution via sliding window
        - Persistence: save/load fitted model
        - Emerging topic detection via document frequency delta
    """

    FINANCIAL_STOP_WORDS = {
        "quarter", "fiscal", "year", "company", "business", "operations",
        "results", "financial", "statements", "million", "billion",
        "revenue", "income", "loss", "earnings", "period", "ended",
        "respectively", "includes", "management", "pursuant", "certain",
        "however", "therefore", "including", "accordance", "related",
        "following", "noted", "additional", "approximately", "primarily",
    }

    def __init__(self, config: dict):
        self.min_topic_size = config.get("min_topic_size", 5)
        self.nr_topics = config.get("nr_topics", "auto")
        self.n_gram_range = tuple(config.get("n_gram_range", [1, 3]))
        self.model_save_path = config.get("model_save_path", "data/models/bertopic_model")
        self.embedding_model = config.get("embedding_model", "all-MiniLM-L6-v2")
        self.low_memory = config.get("low_memory", False)
        self._model = None
        self._last_run_date: Optional[date] = None

    def _build_model(self):
        """Build the BERTopic pipeline."""
        try:
            from bertopic import BERTopic
            from sklearn.feature_extraction.text import CountVectorizer

            vectorizer = CountVectorizer(
                stop_words=list(self.FINANCIAL_STOP_WORDS),
                ngram_range=self.n_gram_range,
                min_df=2,
            )

            self._model = BERTopic(
                embedding_model=self.embedding_model,
                vectorizer_model=vectorizer,
                min_topic_size=self.min_topic_size,
                nr_topics=self.nr_topics if self.nr_topics != "auto" else None,
                calculate_probabilities=False,
                low_memory=self.low_memory,
                verbose=False,
            )
            logger.info("BERTopic model initialized")
        except ImportError:
            logger.warning("bertopic not installed. Topic modeling unavailable.")
            self._model = None
        except Exception as e:
            logger.error(f"BERTopic initialization failed: {e}")
            self._model = None

    def fit(self, documents: list[str]) -> list[TopicResult]:
        """Fit the topic model on a corpus of documents."""
        if not documents:
            logger.warning("No documents provided for topic modeling")
            return []

        if self._model is None:
            self._build_model()
        if self._model is None:
            return []

        try:
            logger.info(f"Fitting BERTopic on {len(documents)} documents...")
            topics, _ = self._model.fit_transform(documents)
            self._last_run_date = date.today()
            results = self._extract_topics(topics, documents)
            logger.info(f"Topic modeling complete: {len(results)} topics discovered")
            return results
        except Exception as e:
            logger.error(f"Topic modeling fit failed: {e}")
            return []

    def transform(self, documents: list[str]) -> list[int]:
        """Assign topics to new documents using fitted model."""
        if self._model is None:
            logger.warning("Model not fitted. Call fit() first.")
            return [-1] * len(documents)
        try:
            topics, _ = self._model.transform(documents)
            return topics
        except Exception as e:
            logger.error(f"Topic transform failed: {e}")
            return [-1] * len(documents)

    def _extract_topics(self, topic_assignments: list[int], documents: list[str]) -> list[TopicResult]:
        """Extract TopicResult objects from fitted model."""
        results = []
        topic_info = self._model.get_topic_info()

        for _, row in topic_info.iterrows():
            tid = row["Topic"]
            if tid == -1:       # BERTopic outlier topic
                continue

            count = int(row.get("Count", 0))
            name = str(row.get("Name", f"topic_{tid}"))

            # Get top words and representations
            topic_repr = self._model.get_topic(tid)
            top_words = [w for w, _ in topic_repr[:10]] if topic_repr else []
            top_ngrams = [w for w, _ in topic_repr[:5] if " " in w] if topic_repr else []

            # Get representative documents
            try:
                rep_docs = self._model.get_representative_docs(tid)
                rep_docs = [d[:200] for d in (rep_docs or [])][:3]
            except Exception:
                rep_docs = []

            # Auto-label: use top 3 words
            label = " | ".join(top_words[:3]) if top_words else f"Topic {tid}"

            results.append(TopicResult(
                topic_id=tid,
                label=label,
                top_words=top_words,
                top_ngrams=top_ngrams,
                doc_count=count,
                representative_docs=rep_docs,
                run_date=self._last_run_date,
            ))

        return results

    def detect_emerging_topics(
        self,
        current_topics: list[TopicResult],
        previous_counts: dict[int, int],
        growth_threshold: float = 0.5,
    ) -> list[TopicResult]:
        """Flag topics with significant document frequency growth as emerging."""
        emerging = []
        for topic in current_topics:
            prev = previous_counts.get(topic.topic_id, 0)
            if prev == 0 and topic.doc_count >= self.min_topic_size:
                topic.is_emerging = True
                emerging.append(topic)
            elif prev > 0:
                growth = (topic.doc_count - prev) / prev
                if growth >= growth_threshold:
                    topic.is_emerging = True
                    emerging.append(topic)
        logger.info(f"Detected {len(emerging)} emerging topics")
        return emerging

    def topics_over_time(
        self,
        documents: list[str],
        timestamps: list[date],
        nr_bins: int = 10,
    ) -> dict:
        """Compute topic prevalence over time (temporal topic analysis)."""
        if self._model is None or not documents:
            return {}
        try:
            import pandas as pd
            ts_strs = [str(t) for t in timestamps]
            tot = self._model.topics_over_time(documents, ts_strs, nr_bins=nr_bins)
            return tot.to_dict(orient="records") if tot is not None else {}
        except Exception as e:
            logger.warning(f"Topics over time failed: {e}")
            return {}

    def save_model(self):
        """Persist the fitted model to disk."""
        if self._model is None:
            return
        path = Path(self.model_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._model.save(str(path), serialization="safetensors", save_ctfidf=True)
            logger.info(f"BERTopic model saved to {path}")
        except Exception as e:
            logger.warning(f"Model save failed (trying pickle): {e}")
            with open(str(path) + ".pkl", "wb") as f:
                pickle.dump(self._model, f)

    def load_model(self):
        """Load a previously saved model."""
        path = Path(self.model_save_path)
        pkl_path = Path(str(self.model_save_path) + ".pkl")

        try:
            from bertopic import BERTopic
            if path.exists():
                self._model = BERTopic.load(str(path))
                logger.info(f"BERTopic model loaded from {path}")
            elif pkl_path.exists():
                with open(pkl_path, "rb") as f:
                    self._model = pickle.load(f)
                logger.info(f"BERTopic model loaded from {pkl_path}")
        except Exception as e:
            logger.error(f"Failed to load BERTopic model: {e}")

    def save_topics_to_pg(self, topics: list[TopicResult], pg_store, doc_id_map: dict = None):
        """Persist topic results to PostgreSQL."""
        if not pg_store or not topics:
            return

        for topic in topics:
            try:
                with pg_store._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO mg_topic_clusters
                                (topic_id, run_date, top_words, top_ngrams,
                                 doc_count, coherence_score, label, is_emerging)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (topic_id, run_date) DO UPDATE SET
                                doc_count = EXCLUDED.doc_count,
                                is_emerging = EXCLUDED.is_emerging,
                                label = EXCLUDED.label
                        """, (
                            topic.topic_id,
                            topic.run_date,
                            topic.top_words,
                            topic.top_ngrams,
                            topic.doc_count,
                            topic.coherence_score,
                            topic.label[:200],
                            topic.is_emerging,
                        ))
            except Exception as e:
                logger.warning(f"Failed to save topic {topic.topic_id}: {e}")

        logger.info(f"Saved {len(topics)} topics to PostgreSQL")
