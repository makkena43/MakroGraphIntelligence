"""Integration test — Gemini Flash REST API.

Run directly:
    python tests/test_gemini_api.py

Or via pytest (skipped automatically when no API key is present):
    pytest tests/test_gemini_api.py -v

The test loads settings.yaml for model / timeout / key, then falls back to
the GEMINI_API_KEY environment variable.  It exercises three scenarios:

    1. themes-only prompt  (gemini_themes_analysis task)
    2. stocks + themes prompt  (gemini_stocks_analysis task)
    3. timeout enforcement  (short timeout → ConnectTimeout/ReadTimeout expected)
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
import requests

# ── locate repo root & add src to path ───────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# ── load config ───────────────────────────────────────────────────────────────
def _load_settings() -> dict:
    """Load settings.yaml and merge secrets.json (mirrors app.py load_config)."""
    import json as _json
    try:
        import yaml
        with open(REPO_ROOT / "config" / "settings.yaml") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:
        cfg = {}

    secrets_path = REPO_ROOT / "config" / "secrets.json"
    if secrets_path.exists():
        try:
            with open(secrets_path) as fh:
                secrets = _json.load(fh)
            for section, values in secrets.items():
                if section.startswith("_"):
                    continue
                if isinstance(values, dict):
                    cfg.setdefault(section, {}).update(
                        {k: v for k, v in values.items() if v}
                    )
        except Exception:
            pass

    return cfg


_SETTINGS      = _load_settings()
_GEMINI_CFG    = _SETTINGS.get("gemini", {})
_API_KEY       = _GEMINI_CFG.get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
_MODEL         = _GEMINI_CFG.get("model", "gemini-flash-latest")
_MAX_TOKENS    = int(_GEMINI_CFG.get("max_tokens", 1200))
_TEMPERATURE   = float(_GEMINI_CFG.get("temperature", 0.4))
_TIMEOUT       = int(_GEMINI_CFG.get("timeout_seconds", 45))
_CONN_TIMEOUT  = int(_GEMINI_CFG.get("connect_timeout_seconds", 10))

_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
)

needs_key = pytest.mark.skipif(not _API_KEY, reason="GEMINI_API_KEY not set")


# ── sample data ───────────────────────────────────────────────────────────────
SAMPLE_THEMES = [
    {
        "rank": 1,
        "theme_name": "AI Infrastructure Power Constraint",
        "conviction": "CONFIRMED",
        "strength_score": 94,
        "confirmed_quarters": 4,
        "company_count": 18,
        "sectors": ["Technology", "Utilities", "REITs"],
        "key_signals": "data center power demand surge, utility capex spike, grid interconnection delays",
    },
    {
        "rank": 2,
        "theme_name": "HBM / Advanced Memory Supply Constraint",
        "conviction": "CONFIRMED",
        "strength_score": 88,
        "confirmed_quarters": 3,
        "company_count": 12,
        "sectors": ["Semiconductors", "Technology"],
        "key_signals": "HBM3E qualification ramp, SK Hynix / Micron allocation limits, AI accelerator BOM dependency",
    },
    {
        "rank": 3,
        "theme_name": "GLP-1 Obesity Drug Manufacturing Scale-Up",
        "conviction": "CONFIRMED",
        "strength_score": 82,
        "confirmed_quarters": 3,
        "company_count": 9,
        "sectors": ["Healthcare", "Pharma", "Medical Devices"],
        "key_signals": "Novo Nordisk / Eli Lilly capacity expansion, CMO capex, API supply chain build-out",
    },
    {
        "rank": 4,
        "theme_name": "US Onshore Defense Electronics",
        "conviction": "DEVELOPING",
        "strength_score": 71,
        "confirmed_quarters": 2,
        "company_count": 14,
        "sectors": ["Defense", "Electronics", "Semiconductors"],
        "key_signals": "Pentagon domestic sourcing mandates, CHIPS Act defense allocation, DoD multi-year contracts",
    },
    {
        "rank": 5,
        "theme_name": "Grid Modernization & Battery Storage Buildout",
        "conviction": "DEVELOPING",
        "strength_score": 65,
        "confirmed_quarters": 2,
        "company_count": 11,
        "sectors": ["Utilities", "Clean Energy", "Industrials"],
        "key_signals": "IRA storage ITC, utility BESS capex, interconnection queue growth",
    },
]

SAMPLE_STOCKS = [
    {"ticker": "NVDA",   "company": "NVIDIA Corporation",        "role": "direct",   "score": 0.9821, "themes": ["AI Infrastructure Power Constraint", "HBM / Advanced Memory Supply Constraint"]},
    {"ticker": "VST",    "company": "Vistra Corp",               "role": "indirect", "score": 0.8734, "themes": ["AI Infrastructure Power Constraint", "Grid Modernization & Battery Storage Buildout"]},
    {"ticker": "MU",     "company": "Micron Technology",         "role": "direct",   "score": 0.8612, "themes": ["HBM / Advanced Memory Supply Constraint"]},
    {"ticker": "NVO",    "company": "Novo Nordisk A/S",          "role": "direct",   "score": 0.8405, "themes": ["GLP-1 Obesity Drug Manufacturing Scale-Up"]},
    {"ticker": "CEG",    "company": "Constellation Energy",      "role": "indirect", "score": 0.8198, "themes": ["AI Infrastructure Power Constraint", "Grid Modernization & Battery Storage Buildout"]},
    {"ticker": "LMT",    "company": "Lockheed Martin",           "role": "direct",   "score": 0.7943, "themes": ["US Onshore Defense Electronics"]},
    {"ticker": "ANET",   "company": "Arista Networks",           "role": "direct",   "score": 0.7832, "themes": ["AI Infrastructure Power Constraint"]},
    {"ticker": "ENPH",   "company": "Enphase Energy",            "role": "direct",   "score": 0.7215, "themes": ["Grid Modernization & Battery Storage Buildout"]},
]


# ── helper ────────────────────────────────────────────────────────────────────
class InvalidAPIKeyError(Exception):
    pass


def _call(prompt: str, timeout: int = _TIMEOUT, connect_timeout: int = _CONN_TIMEOUT) -> dict:
    """Call Gemini and return {text, in_tokens, out_tokens, latency_ms}."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": _TEMPERATURE,
            "maxOutputTokens": _MAX_TOKENS,
        },
    }
    t0 = time.perf_counter()
    resp = requests.post(
        _ENDPOINT,
        headers={"Content-Type": "application/json", "X-goog-api-key": _API_KEY},
        json=payload,
        timeout=(connect_timeout, timeout),
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code in (401, 403):
        raise InvalidAPIKeyError(
            f"API key rejected ({resp.status_code}): key may be expired or invalid. "
            f"Get a fresh key at https://aistudio.google.com/app/apikey"
        )
    resp.raise_for_status()
    data = resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})
    return {
        "text": text,
        "in_tokens":  usage.get("promptTokenCount", 0),
        "out_tokens": usage.get("candidatesTokenCount", 0),
        "latency_ms": latency_ms,
    }


def _build_themes_text() -> str:
    lines = []
    for t in SAMPLE_THEMES:
        lines.append(
            f"{t['rank']}. {t['theme_name']} [{t['conviction']}] | "
            f"Score: {t['strength_score']} | {t['confirmed_quarters']}Q confirmed | "
            f"{t['company_count']} companies | Sectors: {', '.join(t['sectors'])}"
        )
    return "\n".join(lines)


def _build_stocks_text() -> str:
    lines = []
    for s in SAMPLE_STOCKS:
        lines.append(
            f"  {s['ticker']} ({s['company']}) | {s['role']} | "
            f"Score: {s['score']:.4f} | Themes: {', '.join(s['themes'])}"
        )
    return "\n".join(lines)


# ── tests ─────────────────────────────────────────────────────────────────────
@needs_key
def test_themes_only_prompt():
    """gemini_themes_analysis — themes-only shortlist scenario."""
    themes_text = _build_themes_text()
    prompt = (
        "You are an expert macro investment analyst. The following investment themes were "
        "auto-detected from USA (NYSE/NASDAQ) market company filings and earnings data over "
        "multiple quarters.\n\n"
        f"SHORTLISTED THEMES ({len(SAMPLE_THEMES)} themes):\n{themes_text}\n\n"
        "Provide a concise investment analysis covering:\n"
        "1. Top 3 themes with the strongest multi-year investment case (2-3 sentences each)\n"
        "2. Cross-theme connections and amplifying factors\n"
        "3. Key macro risks to monitor across these themes\n"
        "4. Sector rotation implications\n\n"
        "Be specific, data-driven, and actionable. Write for a professional equity investor."
    )
    result = _call(prompt)

    print("\n" + "=" * 70)
    print(f"[THEMES-ONLY]  latency={result['latency_ms']}ms  "
          f"in={result['in_tokens']} out={result['out_tokens']} tokens")
    print("=" * 70)
    print(result["text"])
    print("=" * 70)

    assert result["text"], "Expected non-empty response text"
    assert result["out_tokens"] > 20, "Response suspiciously short"


@needs_key
def test_stocks_and_themes_prompt():
    """gemini_stocks_analysis — ranked stocks + themes scenario."""
    themes_text = _build_themes_text()
    stocks_text = _build_stocks_text()
    prompt = (
        "You are an expert thematic portfolio manager. The following stocks were ranked using "
        "multi-factor thematic analysis of USA (NYSE/NASDAQ) market company filings.\n\n"
        f"ACTIVE THEMES:\n{themes_text}\n\n"
        f"TOP RANKED STOCKS:\n{stocks_text}\n\n"
        "Provide:\n"
        "1. Top 5 high-conviction positions with brief rationale (1-2 sentences each)\n"
        "2. Portfolio construction guidance (supply chain vs end beneficiary vs direct plays)\n"
        "3. Key theme concentration risks to hedge\n"
        "4. One contrarian view worth considering\n\n"
        "Be concise and actionable. Write for a professional portfolio manager."
    )
    result = _call(prompt)

    print("\n" + "=" * 70)
    print(f"[STOCKS+THEMES]  latency={result['latency_ms']}ms  "
          f"in={result['in_tokens']} out={result['out_tokens']} tokens")
    print("=" * 70)
    print(result["text"])
    print("=" * 70)

    assert result["text"], "Expected non-empty response text"
    assert result["out_tokens"] > 20, "Response suspiciously short"


@needs_key
def test_response_is_valid_text():
    """Smoke test — minimal prompt to verify auth + connectivity."""
    result = _call("Reply with exactly: OK")
    assert "OK" in result["text"], f"Unexpected response: {result['text']}"
    print(f"\n[SMOKE]  latency={result['latency_ms']}ms  response='{result['text'].strip()}'")


def test_timeout_enforcement():
    """Verify that a near-zero connect timeout raises requests.exceptions.Timeout.

    Uses a non-routable IP (RFC 5737 TEST-NET-3) so the TCP SYN is dropped and
    the connect timeout fires — no valid API key required.
    """
    dead_url = "http://192.0.2.1/v1/generate"
    with pytest.raises(requests.exceptions.Timeout):
        requests.post(
            dead_url,
            headers={"Content-Type": "application/json"},
            json={"contents": []},
            timeout=(1, 1),
        )


def test_missing_key_raises_http_error():
    """A blank/bad API key must raise an HTTPError (401 / 400)."""
    payload = {
        "contents": [{"parts": [{"text": "Hi"}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 10},
    }
    resp = requests.post(
        _ENDPOINT,
        headers={"Content-Type": "application/json", "X-goog-api-key": "INVALID_KEY"},
        json=payload,
        timeout=(_CONN_TIMEOUT, _TIMEOUT),
    )
    assert resp.status_code in (400, 401, 403), (
        f"Expected auth error, got {resp.status_code}: {resp.text[:200]}"
    )


# ── standalone runner ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not _API_KEY:
        print("ERROR: No Gemini API key found.")
        print("  Set GEMINI_API_KEY env var  OR  add api_key to config/settings.yaml [gemini] block.")
        sys.exit(1)

    print(f"Model   : {_MODEL}")
    print(f"Endpoint: {_ENDPOINT}")
    print(f"Timeout : connect={_CONN_TIMEOUT}s  read={_TIMEOUT}s")
    print(f"Params  : temperature={_TEMPERATURE}  max_tokens={_MAX_TOKENS}")
    print()

    tests = [
        ("Smoke test", test_response_is_valid_text),
        ("Themes-only analysis", test_themes_only_prompt),
        ("Stocks + themes analysis", test_stocks_and_themes_prompt),
        ("Timeout enforcement", test_timeout_enforcement),
        ("Bad key → HTTP error", test_missing_key_raises_http_error),
    ]

    passed = failed = key_errors = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except InvalidAPIKeyError as exc:
            print(f"  KEY?  {name} → {exc}")
            key_errors += 1
        except Exception as exc:
            print(f"  FAIL  {name} → {exc}")
            failed += 1

    if key_errors:
        print(f"\n  ⚠  {key_errors} test(s) failed due to invalid/expired API key.")
        print("     Get a fresh key → https://aistudio.google.com/app/apikey")
        print("     Then set it in  config/secrets.json  under  gemini.api_key")

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
