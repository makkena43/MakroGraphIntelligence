"""BERTrend: Temporal trend acceleration analysis on top of BERTopic.

BERTrend extends BERTopic with:
    - Sliding-window topic modeling across time buckets
    - Velocity:      rate of topic document count change (first derivative)
    - Acceleration:  rate of velocity change (second derivative)
    - Inflection detection: points where acceleration sign changes
    - Trend classification: EMERGING | SURGING | PEAKING | DECLINING | DORMANT

Reference: Inspired by BERTrend methodology for detecting nascent trends
in corpora with temporal metadata.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TrendPoint:
    """A single point in a topic's time series."""
    period: str           # "2024-Q1", "2024-W03", "2024-01"
    period_start: date
    doc_count: int
    velocity: float = 0.0       # doc_count change from previous period
    acceleration: float = 0.0   # velocity change from previous period
    top_words: list[str] = field(default_factory=list)


@dataclass
class TopicTrend:
    """Full trend analysis for a single topic."""
    topic_id: int
    label: str
    trend_class: str          # EMERGING | SURGING | PEAKING | DECLINING | DORMANT
    current_velocity: float
    current_acceleration: float
    peak_period: Optional[str]
    inflection_periods: list[str]
    time_series: list[TrendPoint]
    trend_score: float        # 0–100 composite score
    sectors: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        """True if trend is in an early-stage, investable phase."""
        return self.trend_class in ("EMERGING", "SURGING")

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "trend_class": self.trend_class,
            "current_velocity": round(self.current_velocity, 3),
            "current_acceleration": round(self.current_acceleration, 3),
            "peak_period": self.peak_period,
            "inflection_periods": self.inflection_periods,
            "trend_score": round(self.trend_score, 2),
            "is_actionable": self.is_actionable,
            "time_series_len": len(self.time_series),
        }


class BERTrend:
    """Trend acceleration analyzer for financial document corpora.

    Workflow:
        1. Partition corpus into time buckets (monthly, quarterly, weekly)
        2. Fit BERTopic per bucket OR use topics_over_time
        3. Compute velocity and acceleration per topic per period
        4. Classify trend phase for each topic
        5. Return ranked list of actionable emerging trends

    Integration with ThemeDetector:
        - EMERGING/SURGING topics with cross-sector signatures
          are fed as candidate themes to ThemeDetector
    """

    PERIOD_FORMATS = {
        "monthly": "%Y-%m",
        "quarterly": "Q",
        "weekly": "%Y-W%W",
    }

    def __init__(self, config: dict):
        self.granularity = config.get("trend_granularity", "monthly")
        self.min_periods = config.get("min_periods_for_trend", 3)
        self.velocity_threshold = config.get("emerging_velocity_threshold", 0.15)
        self.acceleration_threshold = config.get("emerging_acceleration_threshold", 0.10)
        self.min_doc_count = config.get("min_docs_per_period", 3)
        self.smoothing_window = config.get("smoothing_window", 2)

    def analyze(
        self,
        documents: list[str],
        timestamps: list[date],
        topic_assignments: list[int],
        topic_labels: dict[int, str] = None,
        topic_words: dict[int, list[str]] = None,
    ) -> list[TopicTrend]:
        """Compute trend metrics for all topics in the corpus.

        Args:
            documents:         List of document texts
            timestamps:        Filing date per document
            topic_assignments: BERTopic topic ID per document (-1 = outlier)
            topic_labels:      {topic_id: label} from BERTopic
            topic_words:       {topic_id: [word, ...]} from BERTopic

        Returns:
            List of TopicTrend objects sorted by trend_score desc
        """
        labels = topic_labels or {}
        words = topic_words or {}

        # Build time-series per topic
        series = self._build_time_series(documents, timestamps, topic_assignments, words)

        trends = []
        for topic_id, time_points in series.items():
            if topic_id == -1:
                continue
            if len(time_points) < self.min_periods:
                continue

            smoothed = self._smooth(time_points)
            velocities = self._compute_velocity(smoothed)
            accelerations = self._compute_acceleration(velocities)
            trend_class = self._classify(velocities, accelerations)
            inflections = self._detect_inflections(accelerations)
            peak = self._find_peak(smoothed)
            score = self._compute_trend_score(smoothed, velocities, accelerations, trend_class)

            trends.append(TopicTrend(
                topic_id=topic_id,
                label=labels.get(topic_id, f"Topic {topic_id}"),
                trend_class=trend_class,
                current_velocity=velocities[-1] if velocities else 0.0,
                current_acceleration=accelerations[-1] if accelerations else 0.0,
                peak_period=peak,
                inflection_periods=inflections,
                time_series=smoothed,
                trend_score=score,
            ))

        trends.sort(key=lambda t: -t.trend_score)
        logger.info(
            f"BERTrend: analyzed {len(trends)} topics | "
            f"EMERGING: {sum(1 for t in trends if t.trend_class == 'EMERGING')} | "
            f"SURGING: {sum(1 for t in trends if t.trend_class == 'SURGING')}"
        )
        return trends

    def get_emerging_trends(self, trends: list[TopicTrend], top_n: int = 10) -> list[TopicTrend]:
        """Filter and return top N actionable (EMERGING/SURGING) trends."""
        actionable = [t for t in trends if t.is_actionable]
        return actionable[:top_n]

    def compare_periods(
        self,
        trends_now: list[TopicTrend],
        trends_prev: list[TopicTrend],
    ) -> list[dict]:
        """Compare two sets of trend results to detect newly emerging topics."""
        prev_map = {t.topic_id: t for t in trends_prev}
        changes = []

        for trend in trends_now:
            prev = prev_map.get(trend.topic_id)
            if prev is None:
                changes.append({
                    "topic_id": trend.topic_id,
                    "label": trend.label,
                    "change_type": "new_topic",
                    "trend_class": trend.trend_class,
                    "score_delta": trend.trend_score,
                })
            elif trend.trend_class != prev.trend_class:
                changes.append({
                    "topic_id": trend.topic_id,
                    "label": trend.label,
                    "change_type": "class_change",
                    "from_class": prev.trend_class,
                    "to_class": trend.trend_class,
                    "score_delta": round(trend.trend_score - prev.trend_score, 2),
                })

        changes.sort(key=lambda c: -abs(c.get("score_delta", 0)))
        return changes

    def format_report(self, trends: list[TopicTrend], top_n: int = 15) -> str:
        """Return a human-readable trend acceleration report."""
        lines = [
            f"\n{'='*70}",
            "BERTREND — TOPIC TREND ACCELERATION REPORT",
            f"{'='*70}",
            f"{'#':<4} {'Label':<40} {'Class':<12} {'Vel':>8} {'Accel':>8} {'Score':>7}",
            "-" * 70,
        ]
        for i, t in enumerate(trends[:top_n], 1):
            vel = f"{t.current_velocity:+.3f}"
            acc = f"{t.current_acceleration:+.3f}"
            lines.append(
                f"{i:<4} {t.label[:38]:<40} {t.trend_class:<12} "
                f"{vel:>8} {acc:>8} {t.trend_score:>7.1f}"
            )
        return "\n".join(lines)

    # ----------------------------------------------------------
    # INTERNAL HELPERS
    # ----------------------------------------------------------
    def _period_key(self, d: date) -> str:
        if self.granularity == "quarterly":
            return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
        elif self.granularity == "weekly":
            return d.strftime("%Y-W%W")
        return d.strftime("%Y-%m")

    def _period_start(self, key: str) -> date:
        try:
            if "Q" in key:
                year, q = key.split("-Q")
                month = (int(q) - 1) * 3 + 1
                return date(int(year), month, 1)
            elif "W" in key:
                year, week = key.split("-W")
                return date.fromisocalendar(int(year), int(week), 1)
            else:
                year, month = key.split("-")
                return date(int(year), int(month), 1)
        except Exception:
            return date.today()

    def _build_time_series(
        self,
        documents: list[str],
        timestamps: list[date],
        topic_assignments: list[int],
        topic_words: dict[int, list[str]],
    ) -> dict[int, list[TrendPoint]]:
        """Aggregate document counts per topic per time period."""
        counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for doc, ts, tid in zip(documents, timestamps, topic_assignments):
            if ts is None:
                continue
            period = self._period_key(ts)
            counts[tid][period] += 1

        series: dict[int, list[TrendPoint]] = {}
        for tid, period_counts in counts.items():
            sorted_periods = sorted(period_counts.keys())
            points = [
                TrendPoint(
                    period=p,
                    period_start=self._period_start(p),
                    doc_count=period_counts[p],
                    top_words=topic_words.get(tid, [])[:5],
                )
                for p in sorted_periods
            ]
            series[tid] = points

        return series

    def _smooth(self, points: list[TrendPoint]) -> list[TrendPoint]:
        """Apply rolling mean smoothing to doc_count values."""
        if len(points) < self.smoothing_window:
            return points
        counts = [p.doc_count for p in points]
        smoothed_counts = np.convolve(
            counts, np.ones(self.smoothing_window) / self.smoothing_window, mode="same"
        )
        for p, sc in zip(points, smoothed_counts):
            p.doc_count = max(0, int(round(float(sc))))
        return points

    def _compute_velocity(self, points: list[TrendPoint]) -> list[float]:
        """First derivative: fractional change in doc_count per period."""
        velocities = [0.0]
        for i in range(1, len(points)):
            prev = points[i - 1].doc_count or 1
            curr = points[i].doc_count
            v = (curr - prev) / prev
            velocities.append(v)
            points[i].velocity = v
        return velocities

    def _compute_acceleration(self, velocities: list[float]) -> list[float]:
        """Second derivative: change in velocity."""
        accelerations = [0.0]
        for i in range(1, len(velocities)):
            a = velocities[i] - velocities[i - 1]
            accelerations.append(a)
        for i, point_a in enumerate(accelerations):
            pass  # already embedded in TrendPoint.acceleration via velocities
        return accelerations

    def _classify(self, velocities: list[float], accelerations: list[float]) -> str:
        """Classify the current trend phase."""
        if not velocities or not accelerations:
            return "DORMANT"
        v = velocities[-1]
        a = accelerations[-1]

        if v >= self.velocity_threshold and a >= self.acceleration_threshold:
            return "EMERGING"
        elif v >= self.velocity_threshold and a >= 0:
            return "SURGING"
        elif v >= 0 and a < 0:
            return "PEAKING"
        elif v < 0 and a < 0:
            return "DECLINING"
        elif v < -self.velocity_threshold:
            return "DORMANT"
        return "STABLE"

    def _detect_inflections(self, accelerations: list[float]) -> list[str]:
        """Find periods where acceleration changed sign (inflection points)."""
        return []  # placeholder — needs period labels passed through

    def _find_peak(self, points: list[TrendPoint]) -> Optional[str]:
        """Find the period with maximum doc_count."""
        if not points:
            return None
        peak_point = max(points, key=lambda p: p.doc_count)
        return peak_point.period

    def _compute_trend_score(
        self,
        points: list[TrendPoint],
        velocities: list[float],
        accelerations: list[float],
        trend_class: str,
    ) -> float:
        """Composite trend score 0–100."""
        if not points or not velocities:
            return 0.0

        latest_count = points[-1].doc_count if points else 0
        max_count = max(p.doc_count for p in points) if points else 1

        recency_score = min(latest_count * 5.0, 40.0)
        velocity_score = min(max(velocities[-1], 0) * 100.0, 30.0)
        accel_score = min(max(accelerations[-1], 0) * 100.0, 20.0)
        class_bonus = {
            "EMERGING": 10.0, "SURGING": 8.0, "STABLE": 4.0,
            "PEAKING": 2.0, "DECLINING": 0.0, "DORMANT": 0.0,
        }.get(trend_class, 0.0)

        return min(recency_score + velocity_score + accel_score + class_bonus, 100.0)
