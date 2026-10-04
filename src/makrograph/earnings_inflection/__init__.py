"""Earnings Inflection Detector (research-only, opt-in).

Detects material, potentially sustainable changes in a company's earnings
economics from dated filings.  Produces evidence assessments only - never
investment actions, position sizes or portfolio instructions - and is not
wired into any existing pipeline, schedule or notification path.

Entry point: ``scripts/earnings_inflection.py`` or
``makrograph.earnings_inflection.pipeline.EarningsInflectionPipeline``.
"""

from .contracts import RESEARCH_ONLY_NOTICE  # noqa: F401
