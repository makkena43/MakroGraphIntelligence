"""Selective LLM reasoning for high-value tasks only.

LLMs are called SPARINGLY and only for:
    1. Investment hypothesis generation for confirmed themes
    2. Entity disambiguation (e.g., "Apple" -> tech company vs fruit)
    3. Signal context validation (confirm ambiguous signals)
    4. Theme description enrichment
    5. GraphRAG multi-hop reasoning (graph_rag.py)

Supported providers:
    anthropic  — Claude 3 Haiku / Sonnet / Opus
    openai     — GPT-4o / GPT-4o-mini
    deepseek   — DeepSeek-V3 / DeepSeek-R1 (OpenAI-compatible API)
    gemini     — Google Gemini Flash / Pro (REST API, no SDK needed)

Budget protection:
    - Token tracking per task type
    - Daily cost cap enforcement
    - Batching and caching to avoid redundant calls

DeepSeek notes:
    - Uses OpenAI-compatible SDK with base_url='https://api.deepseek.com'
    - Set DEEPSEEK_API_KEY env var
    - DeepSeek-V3 default model: 'deepseek-chat'
    - DeepSeek-R1 (reasoning): 'deepseek-reasoner' (higher cost, chain-of-thought)
"""

import hashlib
import json
import logging
import time
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


class LLMReasoner:
    """Selective LLM interface supporting Claude (Anthropic) and OpenAI.

    Call budget is strictly enforced. Every LLM call is logged to PostgreSQL.
    """

    TASK_PROMPTS = {
        "theme_hypothesis": (
            "You are an expert equity analyst. Given the following investment theme data from SEC filings "
            "and earnings calls, write a concise 3-sentence investment hypothesis explaining:\n"
            "1. What is driving this theme\n"
            "2. Why it represents a multi-year opportunity\n"
            "3. The key risks to monitor\n\n"
            "Theme: {theme_name}\n"
            "Description: {description}\n"
            "Key signals: {signal_summary}\n"
            "Sectors affected: {sectors}\n"
            "Companies showing evidence: {companies}\n\n"
            "Investment Hypothesis:"
        ),
        "entity_resolution": (
            "You are a financial data expert. Given the following entity mention from an SEC filing, "
            "determine the correct canonical company name, ticker symbol (if public), and entity type.\n\n"
            "Entity text: '{entity_text}'\n"
            "Context: '{context}'\n\n"
            "Respond in JSON: {{\"canonical_name\": \"...\", \"ticker\": \"...\", \"entity_type\": \"...\", \"confidence\": 0.0}}"
        ),
        "signal_validation": (
            "You are a financial analyst reviewing signal extraction from SEC filings. "
            "Determine if the following text contains a genuine investment signal.\n\n"
            "Signal type identified: '{signal_type}'\n"
            "Context: '{context}'\n\n"
            "Does this context genuinely indicate '{signal_type}'? "
            "Respond in JSON: {{\"is_valid\": true/false, \"confidence\": 0.0, \"corrected_type\": \"...\"}}"
        ),
        "canonical_naming": (
            "You are a senior investment analyst. The following auto-detected theme names all describe "
            "the same macro investment opportunity detected from SEC filings and earnings calls.\n\n"
            "Theme names:\n{theme_names}\n\n"
            "Strongest theme description:\n{description}\n\n"
            "Generate ONE canonical name for this investment theme cluster. Rules:\n"
            "- Max 7 words\n"
            "- Investor-grade: clear, specific, actionable\n"
            "- Focus on the structural driver + the asset class impacted\n"
            "- Good examples: 'AI Infrastructure Power Constraint', "
            "'HBM Supply Constraint', 'EV Battery Materials Shortage'\n"
            "- Bad examples: 'Technology', 'Supply Chain', 'AI Growth'\n\n"
            "Respond with ONLY the canonical name — no explanation, no punctuation at the end."
        ),
        "gemini_themes_analysis": (
            "You are an expert macro investment analyst. The following investment themes were "
            "auto-detected from {market} market company filings and earnings data over multiple quarters.\n\n"
            "SHORTLISTED THEMES ({n_themes} themes):\n{themes_text}\n\n"
            "Provide a concise investment analysis covering:\n"
            "1. Top 3 themes with the strongest multi-year investment case (2-3 sentences each)\n"
            "2. Cross-theme connections and amplifying factors\n"
            "3. Key macro risks to monitor across these themes\n"
            "4. Sector rotation implications\n\n"
            "Be specific, data-driven, and actionable. Write for a professional equity investor."
        ),
        "gemini_stocks_analysis": (
            "You are an expert thematic portfolio manager. The following stocks were ranked using "
            "multi-factor thematic analysis of {market} market company filings.\n\n"
            "ACTIVE THEMES:\n{themes_text}\n\n"
            "TOP RANKED STOCKS:\n{stocks_text}\n\n"
            "Provide:\n"
            "1. Top 5 high-conviction positions with brief rationale (1-2 sentences each)\n"
            "2. Portfolio construction guidance (supply chain vs end beneficiary vs direct plays)\n"
            "3. Key theme concentration risks to hedge\n"
            "4. One contrarian view worth considering\n\n"
            "Be concise and actionable. Write for a professional portfolio manager."
        ),
    }

    def __init__(self, config: dict):
        self.provider = config.get("llm_provider", "anthropic")  # anthropic | openai | deepseek
        self.model = config.get("llm_model", "claude-3-haiku-20240307")
        self.max_tokens = config.get("llm_max_tokens", 400)
        self.temperature = config.get("llm_temperature", 0.3)
        self.daily_cost_cap_usd = config.get("llm_daily_cost_cap_usd", 2.0)
        self.enabled = config.get("llm_enabled", False)  # OFF by default

        # Cost per 1K tokens (approximate)
        self._cost_per_1k = config.get("llm_cost_per_1k_tokens", 0.00025)

        self._gemini_api_key: str = config.get("gemini_api_key", "")
        self._cache: dict[str, str] = {}
        self._daily_cost: float = 0.0
        self._daily_calls: int = 0
        self._last_reset = str(date.today())
        self._client = None

    def _reset_daily_if_needed(self):
        today = str(date.today())
        if self._last_reset != today:
            self._daily_cost = 0.0
            self._daily_calls = 0
            self._last_reset = today

    def _load_client(self):
        """Lazy-load the LLM client."""
        if self._client is not None:
            return

        if self.provider == "anthropic":
            try:
                import anthropic
                self._client = anthropic.Anthropic()
                logger.info(f"Anthropic client loaded (model: {self.model})")
            except ImportError:
                logger.warning("anthropic package not installed.")
                self._client = None
        elif self.provider == "openai":
            try:
                from openai import OpenAI
                self._client = OpenAI()
                logger.info(f"OpenAI client loaded (model: {self.model})")
            except ImportError:
                logger.warning("openai package not installed.")
                self._client = None
        elif self.provider == "deepseek":
            try:
                import os
                from openai import OpenAI
                api_key = os.environ.get("DEEPSEEK_API_KEY", "")
                if not api_key:
                    logger.warning("DEEPSEEK_API_KEY env var not set.")
                self._client = OpenAI(
                    api_key=api_key,
                    base_url="https://api.deepseek.com",
                )
                logger.info(f"DeepSeek client loaded (model: {self.model})")
            except ImportError:
                logger.warning("openai package not installed (required for DeepSeek).")
                self._client = None
            except Exception as e:
                logger.warning(f"DeepSeek client init failed: {e}")
                self._client = None
        elif self.provider == "gemini":
            import os
            api_key = self._gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                logger.warning("Gemini API key not configured. Set gemini_api_key in config or GEMINI_API_KEY env var.")
            self._client = {"api_key": api_key}
            logger.info(f"Gemini client configured (model: {self.model})")

    def _cache_key(self, task: str, payload: dict) -> str:
        raw = task + json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _call_llm(self, prompt: str, task_type: str) -> Optional[str]:
        """Execute a single LLM call with budget enforcement."""
        self._reset_daily_if_needed()

        if not self.enabled:
            logger.debug(f"LLM disabled. Skipping {task_type} call.")
            return None

        if self._daily_cost >= self.daily_cost_cap_usd:
            logger.warning(f"LLM daily cost cap (${self.daily_cost_cap_usd}) reached. Skipping.")
            return None

        self._load_client()
        if self._client is None:
            return None

        start_ms = int(time.time() * 1000)
        try:
            if self.provider == "anthropic":
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.content[0].text
                in_tokens = response.usage.input_tokens
                out_tokens = response.usage.output_tokens

            elif self.provider in ("openai", "deepseek"):
                kwargs = dict(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                # deepseek-reasoner does not accept temperature
                if not (self.provider == "deepseek" and "reasoner" in self.model):
                    kwargs["temperature"] = self.temperature
                response = self._client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content
                in_tokens = response.usage.prompt_tokens
                out_tokens = response.usage.completion_tokens

            elif self.provider == "gemini":
                import requests as _requests
                api_key = self._client.get("api_key", "")
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self.model}:generateContent"
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": self.temperature,
                        "maxOutputTokens": self.max_tokens,
                    },
                }
                resp = _requests.post(
                    url,
                    headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
                    json=payload,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                usage = data.get("usageMetadata", {})
                in_tokens = usage.get("promptTokenCount", len(prompt.split()))
                out_tokens = usage.get("candidatesTokenCount", len(text.split()))

            else:
                return None

            total_tokens = in_tokens + out_tokens
            cost = (total_tokens / 1000.0) * self._cost_per_1k
            self._daily_cost += cost
            self._daily_calls += 1
            latency = int(time.time() * 1000) - start_ms

            logger.debug(
                f"LLM call [{task_type}]: {total_tokens} tokens, "
                f"${cost:.5f}, {latency}ms"
            )
            return text

        except Exception as e:
            logger.error(f"LLM call failed [{task_type}]: {e}")
            return None

    def generate_theme_hypothesis(
        self,
        theme_name: str,
        description: str,
        signal_summary: dict,
        sectors: list[str],
        companies: list[str],
        pg_store=None,
        theme_id: int = None,
    ) -> Optional[str]:
        """Generate an investment hypothesis for a confirmed theme."""
        payload = {
            "theme_name": theme_name,
            "description": description,
            "signal_summary": str(signal_summary),
            "sectors": ", ".join(sectors[:5]),
            "companies": ", ".join(companies[:10]),
        }
        cache_key = self._cache_key("theme_hypothesis", payload)
        if cache_key in self._cache:
            return self._cache[cache_key]

        prompt = self.TASK_PROMPTS["theme_hypothesis"].format(**payload)
        result = self._call_llm(prompt, "theme_hypothesis")

        if result:
            self._cache[cache_key] = result
            self._log_to_pg(pg_store, "theme_hypothesis", payload, result, theme_id)

        return result

    def generate_canonical_name(
        self,
        strongest_theme_name: str,
        all_theme_names: list[str],
        description: str = "",
    ) -> str:
        """Generate a clean canonical name for a cluster of similar themes.

        Called by ThemeCanonicalizer when llm_enabled=True and a cluster
        has 2+ members that warrant a shared canonical name.

        Returns:
            Canonical name string (max 7 words, investor-grade).
            Falls back to ``strongest_theme_name`` if the LLM fails or is
            over budget.
        """
        payload = {
            "theme_names": "\n".join(f"  - {n}" for n in all_theme_names),
            "description": description[:400],
        }
        cache_key = self._cache_key("canonical_naming", payload)
        if cache_key in self._cache:
            return self._cache[cache_key]

        prompt = self.TASK_PROMPTS["canonical_naming"].format(**payload)
        result = self._call_llm(prompt, "canonical_naming")

        if result:
            # Sanitise: strip quotes, trailing punctuation, limit length
            clean = result.strip().strip('"\'').rstrip(".,;:")
            # If LLM went off the rails (too long / empty), fallback
            if 3 <= len(clean.split()) <= 10:
                self._cache[cache_key] = clean
                return clean

        return strongest_theme_name

    def resolve_entity(
        self,
        entity_text: str,
        context: str,
        pg_store=None,
    ) -> Optional[dict]:
        """Resolve ambiguous entity to canonical name + ticker using LLM."""
        payload = {"entity_text": entity_text, "context": context[:300]}
        cache_key = self._cache_key("entity_resolution", payload)
        if cache_key in self._cache:
            return json.loads(self._cache[cache_key])

        prompt = self.TASK_PROMPTS["entity_resolution"].format(**payload)
        result = self._call_llm(prompt, "entity_resolution")

        if result:
            self._cache[cache_key] = result
            self._log_to_pg(pg_store, "entity_resolution", payload, result)
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        return None

    def validate_signal(
        self,
        signal_type: str,
        context: str,
        pg_store=None,
    ) -> Optional[dict]:
        """Validate that a rule-extracted signal is genuine using LLM."""
        payload = {"signal_type": signal_type, "context": context[:400]}
        cache_key = self._cache_key("signal_validation", payload)
        if cache_key in self._cache:
            return json.loads(self._cache[cache_key])

        prompt = self.TASK_PROMPTS["signal_validation"].format(**payload)
        result = self._call_llm(prompt, "signal_validation")

        if result:
            self._cache[cache_key] = result
            self._log_to_pg(pg_store, "signal_validation", payload, result)
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        return None

    def enrich_themes_batch(
        self,
        themes: list,
        beneficiary_map: dict,
        signal_summary_map: dict,
        pg_store=None,
        theme_id_map: dict = None,
    ) -> dict[str, str]:
        """Generate hypotheses for all confirmed/developing themes.

        Returns {theme_slug: hypothesis_text}
        """
        results = {}
        for theme in themes:
            slug = theme.theme_slug if hasattr(theme, "theme_slug") else theme.get("theme_slug")
            name = theme.theme_name if hasattr(theme, "theme_name") else theme.get("theme_name")
            conviction = str(getattr(theme, "conviction", "")) if hasattr(theme, "conviction") else ""

            # Only call LLM for confirmed or developing themes
            if conviction not in ("confirmed", "developing"):
                continue

            beneficiaries = beneficiary_map.get(slug, [])
            companies = [b.company_name or b.entity_name for b in beneficiaries[:10]]
            signals = signal_summary_map.get(slug, {})
            theme_id = (theme_id_map or {}).get(slug)

            hypothesis = self.generate_theme_hypothesis(
                theme_name=name,
                description=getattr(theme, "description", ""),
                signal_summary=signals,
                sectors=getattr(theme, "sectors", []),
                companies=companies,
                pg_store=pg_store,
                theme_id=theme_id,
            )
            if hypothesis:
                results[slug] = hypothesis

        logger.info(
            f"LLM enrichment: {len(results)} hypotheses generated "
            f"(cost: ${self._daily_cost:.4f} today)"
        )
        return results

    def _log_to_pg(self, pg_store, task_type: str, payload: dict,
                   output: str, theme_id: int = None):
        """Log LLM call to PostgreSQL audit table."""
        if not pg_store:
            return
        try:
            with pg_store._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO mg_llm_log
                            (task_type, input_summary, model_used, output_text,
                             cost_usd, related_theme_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        task_type,
                        json.dumps(payload)[:500],
                        self.model,
                        output[:2000],
                        round(self._daily_cost, 6),
                        theme_id,
                    ))
        except Exception as e:
            logger.warning(f"LLM log write failed: {e}")

    def analyze_shortlisted(
        self,
        themes: list,
        stocks: list,
        country: str = "US",
    ) -> Optional[str]:
        """Call Gemini to generate an investment analysis of shortlisted themes + ranked stocks.

        Args:
            themes: List of theme dicts from pg_store.get_shortlisted_themes()
            stocks:  List of RankedStock objects from RankingEngine.run() (may be empty)
            country: ISO-2 market code (US | IN)
        Returns:
            Gemini analysis text, or None if disabled / error.
        """
        market = "India (NSE/BSE)" if country == "IN" else "USA (NYSE/NASDAQ)"

        # Build themes summary
        themes_lines = []
        for i, t in enumerate(themes[:15], 1):
            name = t.get("theme_name", "") if isinstance(t, dict) else getattr(t, "theme_name", "")
            conviction = (t.get("conviction", "") if isinstance(t, dict) else getattr(t, "conviction", "")).upper()
            score = float(t.get("strength_score") or 0) if isinstance(t, dict) else float(getattr(t, "strength_score", 0) or 0)
            quarters = int(t.get("confirmed_quarters") or 0) if isinstance(t, dict) else int(getattr(t, "confirmed_quarters", 0) or 0)
            cos = int(t.get("company_count") or 0) if isinstance(t, dict) else int(getattr(t, "company_count", 0) or 0)
            themes_lines.append(
                f"{i}. {name} [{conviction}] | Score: {score:.0f} | {quarters}Q | {cos} companies"
            )
        themes_text = "\n".join(themes_lines) if themes_lines else "No shortlisted themes available yet."

        # Build stocks summary
        stocks_lines = []
        for s in (stocks or [])[:20]:
            ticker = getattr(s, "ticker", "")
            company = getattr(s, "company_name", "")
            role = getattr(s, "company_role", "")
            score = getattr(s, "final_score", 0)
            theme_list = getattr(s, "themes", [])[:3]
            stocks_lines.append(
                f"  {ticker} ({company}) | {role} | Score: {score:.4f} | Themes: {', '.join(theme_list)}"
            )
        stocks_text = "\n".join(stocks_lines) if stocks_lines else "No ranked stocks available yet."

        if stocks:
            payload = {"themes_text": themes_text, "stocks_text": stocks_text, "market": market}
            cache_key = self._cache_key("gemini_stocks_analysis", payload)
            if cache_key in self._cache:
                return self._cache[cache_key]
            prompt = self.TASK_PROMPTS["gemini_stocks_analysis"].format(**payload)
            task = "gemini_stocks_analysis"
        else:
            payload = {"themes_text": themes_text, "n_themes": len(themes), "market": market}
            cache_key = self._cache_key("gemini_themes_analysis", payload)
            if cache_key in self._cache:
                return self._cache[cache_key]
            prompt = self.TASK_PROMPTS["gemini_themes_analysis"].format(**payload)
            task = "gemini_themes_analysis"

        result = self._call_llm(prompt, task)
        if result:
            self._cache[cache_key] = result
        return result

    @property
    def budget_status(self) -> dict:
        """Return current LLM budget usage."""
        self._reset_daily_if_needed()
        return {
            "date": self._last_reset,
            "daily_calls": self._daily_calls,
            "daily_cost_usd": round(self._daily_cost, 4),
            "daily_cap_usd": self.daily_cost_cap_usd,
            "remaining_usd": round(max(0, self.daily_cost_cap_usd - self._daily_cost), 4),
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
        }
