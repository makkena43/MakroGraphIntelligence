# Reviewed primary-source constraint observations

Source connectors may place JSON packets in this directory. The monthly
pipeline validates and ingests them as immutable point-in-time events before
constraint state is materialized.

Each packet contains `country` and an `observations` list. Every accepted row
needs a reviewed exact product alias, `observed_at`, `published_at`,
`available_at`, primary-source URL/title/family, a positive `revision`, and
type-specific `metrics`. Capacity metrics use `CURRENT_SUPPLY_DEMAND` (with
numerator and denominator); import metrics use `SOURCE_REPORTED_IMPORT_SHARE`
or `CURRENT_IMPORT_SHARE` (with numerator/denominator or
`ratio_reported_by_source: true`).

Packets must contain economic facts only. They must not contain tickers,
desired winners, security returns, rankings, or investment decisions.
