// ============================================================
// MakroGraph Intelligence - Neo4j Graph Schema
// Knowledge graph for ontology and relationship tracking
// ============================================================

// ============================================================
// NODE CONSTRAINTS (enforce uniqueness + create indexes)
// ============================================================

CREATE CONSTRAINT company_name_unique IF NOT EXISTS
FOR (c:Company) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT technology_name_unique IF NOT EXISTS
FOR (t:Technology) REQUIRE t.name IS UNIQUE;

CREATE CONSTRAINT sector_name_unique IF NOT EXISTS
FOR (s:Sector) REQUIRE s.name IS UNIQUE;

CREATE CONSTRAINT concept_name_unique IF NOT EXISTS
FOR (c:Concept) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT product_name_unique IF NOT EXISTS
FOR (p:Product) REQUIRE p.name IS UNIQUE;

CREATE CONSTRAINT person_name_unique IF NOT EXISTS
FOR (p:Person) REQUIRE p.name IS UNIQUE;

CREATE CONSTRAINT regulation_name_unique IF NOT EXISTS
FOR (r:Regulation) REQUIRE r.name IS UNIQUE;

CREATE CONSTRAINT theme_slug_unique IF NOT EXISTS
FOR (t:Theme) REQUIRE t.slug IS UNIQUE;

// ============================================================
// NODE INDEXES
// ============================================================

CREATE INDEX company_ticker_index IF NOT EXISTS
FOR (c:Company) ON (c.ticker);

CREATE INDEX company_cik_index IF NOT EXISTS
FOR (c:Company) ON (c.cik);

CREATE INDEX node_first_seen_index IF NOT EXISTS
FOR (n:Company) ON (n.first_seen_at);

CREATE INDEX theme_strength_index IF NOT EXISTS
FOR (t:Theme) ON (t.strength_score);

// ============================================================
// NODE SCHEMAS (documentation - no enforcement in Neo4j)
// ============================================================

// Company {
//   name: string (unique)
//   ticker: string
//   cik: string               -- SEC CIK
//   sector: string
//   industry: string
//   exchange: string          -- NYSE, NASDAQ, NSE, BSE
//   market_cap_usd: float
//   is_public: boolean
//   mention_count: integer
//   first_seen_at: date
//   last_seen_at: date
//   importance_score: float
// }

// Technology {
//   name: string (unique)
//   category: string          -- AI, Semiconductor, Biotech, EV, etc.
//   maturity_stage: string    -- emerging | growing | mature | declining
//   mention_count: integer
//   first_seen_at: date
//   last_seen_at: date
//   momentum: float
// }

// Sector {
//   name: string (unique)
//   gics_code: string         -- GICS sector code
//   macro_theme: string
// }

// Concept {
//   name: string (unique)
//   concept_type: string      -- bottleneck | demand_driver | macro_trend | risk
//   description: string
//   mention_count: integer
// }

// Theme {
//   slug: string (unique)
//   name: string
//   strength_score: float
//   conviction: string        -- emerging | developing | confirmed | declining
//   first_detected: date
//   last_updated: date
// }

// ============================================================
// RELATIONSHIP TYPES (with properties)
// ============================================================

// (Company)-[:DEVELOPS]->(Technology)
//   weight: float, since_date: date, evidence_count: integer

// (Company)-[:INVESTS_IN]->(Technology | Sector | Company)
//   amount_usd: float, investment_type: string, date: date, weight: float

// (Company)-[:USES]->(Technology)
//   adoption_stage: string, weight: float

// (Company)-[:COMPETES_WITH]->(Company)
//   market_overlap: float, weight: float

// (Company)-[:SUPPLIES_TO]->(Company)
//   supply_type: string, weight: float, criticality: float

// (Company)-[:PART_OF]->(Sector)
//   primary: boolean

// (Technology)-[:ENABLES]->(Technology | Concept)
//   weight: float

// (Technology)-[:DISRUPTS]->(Technology | Sector)
//   weight: float, disruption_stage: string

// (Regulation)-[:AFFECTS]->(Company | Sector | Technology)
//   impact_direction: string, severity: float

// (Company)-[:MENTIONED_IN]->(Document)
//   mention_count: integer, sentiment: float

// (Company | Technology)-[:PART_OF]->(Theme)
//   role: string, relevance_score: float

// ============================================================
// SAMPLE SETUP QUERIES
// ============================================================

// Create initial sector nodes
MERGE (s:Sector {name: 'Technology'}) SET s.gics_code = '45';
MERGE (s:Sector {name: 'Healthcare'}) SET s.gics_code = '35';
MERGE (s:Sector {name: 'Industrials'}) SET s.gics_code = '20';
MERGE (s:Sector {name: 'Energy'}) SET s.gics_code = '10';
MERGE (s:Sector {name: 'Consumer Discretionary'}) SET s.gics_code = '25';
MERGE (s:Sector {name: 'Financials'}) SET s.gics_code = '40';
MERGE (s:Sector {name: 'Materials'}) SET s.gics_code = '15';
MERGE (s:Sector {name: 'Utilities'}) SET s.gics_code = '55';
MERGE (s:Sector {name: 'Real Estate'}) SET s.gics_code = '60';
MERGE (s:Sector {name: 'Communication Services'}) SET s.gics_code = '50';
MERGE (s:Sector {name: 'Consumer Staples'}) SET s.gics_code = '30';

// ============================================================
// USEFUL QUERY PATTERNS
// ============================================================

// Find all companies developing a specific technology
// MATCH (c:Company)-[:DEVELOPS]->(t:Technology {name: 'AI Chips'})
// RETURN c.name, c.ticker, t.name ORDER BY c.mention_count DESC;

// Find supply chain for a technology
// MATCH path = (c:Company)-[:SUPPLIES_TO*1..3]->(end:Company)
// WHERE c.name = 'TSMC'
// RETURN path;

// Find companies associated with a theme
// MATCH (c:Company)-[:PART_OF]->(th:Theme {slug: 'ai-infrastructure'})
// RETURN c.name, c.ticker, c.sector
// ORDER BY c.importance_score DESC;

// Cross-sector theme detection: find technologies mentioned across >3 sectors
// MATCH (t:Technology)<-[:DEVELOPS|USES]-(c:Company)-[:PART_OF]->(s:Sector)
// WITH t, COUNT(DISTINCT s) as sector_count
// WHERE sector_count >= 3
// RETURN t.name, sector_count ORDER BY sector_count DESC;

// Find emerging technology relationships (last 90 days)
// MATCH (c:Company)-[r:DEVELOPS]->(t:Technology)
// WHERE r.since_date >= date() - duration({days: 90})
// RETURN c.name, t.name, r.weight ORDER BY r.weight DESC;

// ============================================================
// MACRO & POLICY LAYER — Node Constraints + Indexes
// ============================================================

CREATE CONSTRAINT country_iso_unique IF NOT EXISTS
FOR (c:Country) REQUIRE c.iso2 IS UNIQUE;

CREATE CONSTRAINT commodity_id_unique IF NOT EXISTS
FOR (c:Commodity) REQUIRE c.commodity_id IS UNIQUE;

CREATE CONSTRAINT policy_id_unique IF NOT EXISTS
FOR (p:Policy) REQUIRE p.policy_id IS UNIQUE;

CREATE CONSTRAINT macro_indicator_unique IF NOT EXISTS
FOR (m:MacroIndicator) REQUIRE m.series_id IS UNIQUE;

CREATE INDEX country_name_index IF NOT EXISTS
FOR (c:Country) ON (c.name);

CREATE INDEX commodity_category_index IF NOT EXISTS
FOR (c:Commodity) ON (c.category);

CREATE INDEX policy_type_index IF NOT EXISTS
FOR (p:Policy) ON (p.policy_type);

CREATE INDEX macro_indicator_series_index IF NOT EXISTS
FOR (m:MacroIndicator) ON (m.series_id);

// ============================================================
// MACRO NODE SCHEMAS (documentation)
// ============================================================

// Country {
//   iso2: string (unique)         -- "US", "CN", "DE"
//   name: string
//   region: string                -- Americas | Asia | Europe | Africa | Oceania
//   gdp_usd_bn: float             -- latest GDP in billions USD
//   population_mn: float
//   debt_to_gdp: float
//   fx_reserves_usd_bn: float
//   is_g20: boolean
//   last_updated: date
// }

// Commodity {
//   commodity_id: string (unique) -- "WTI_CRUDE", "HENRY_HUB", "CORN", "COPPER"
//   name: string
//   category: string              -- energy | agriculture | metals | freight
//   units: string
//   last_price: float
//   last_price_date: date
//   yoy_change_pct: float
//   supply_constraint: boolean    -- true when inventory below 5th percentile
//   last_updated: date
// }

// Policy {
//   policy_id: string (unique)    -- "congress::HR-1234" or "federal_register::2024-12345"
//   title: string
//   policy_type: string           -- bill | executive_order | rule | notice
//   status: string                -- introduced | enacted | proposed | final
//   enacted_date: date
//   effective_date: date
//   impact_direction: string      -- positive | negative | neutral | mixed
//   impact_magnitude: float       -- 0-100
//   sectors_affected: [string]
//   technologies_affected: [string]
// }

// MacroIndicator {
//   series_id: string (unique)    -- "GDP", "CPIAUCSL", "DGS10"
//   series_name: string
//   source: string                -- fred | world_bank | imf
//   country: string               -- ISO-2
//   frequency: string
//   units: string
//   latest_value: float
//   latest_date: date
//   yoy_change_pct: float
//   trend: string                 -- rising | falling | stable
//   last_updated: date
// }

// ============================================================
// MACRO RELATIONSHIP TYPES
// ============================================================

// (Country)-[:EXPORTS]->(Commodity)
//   share_pct: float         -- share of global exports
//   value_usd_bn: float
//   year: integer

// (Country)-[:IMPORTS]->(Commodity)
//   share_pct: float
//   value_usd_bn: float
//   year: integer

// (Country)-[:HAS_INDICATOR]->(MacroIndicator)
//   latest_value: float
//   as_of_date: date

// (Policy)-[:SUBSIDISES]->(Sector | Technology | Commodity)
//   amount_usd_bn: float
//   duration_years: integer

// (Policy)-[:RESTRICTS]->(Sector | Technology | Company | Commodity)
//   severity: float          -- 0-100

// (Policy)-[:INCENTIVISES]->(Sector | Technology | Company)
//   incentive_type: string   -- tax_credit | grant | loan | tariff_exemption

// (MacroIndicator)-[:CONSTRAINS]->(Sector | Theme)
//   when_above: float        -- trigger threshold
//   when_below: float
//   constraint_type: string  -- rate_sensitive | credit_constrained | commodity_input

// (MacroIndicator)-[:CORRELATES_WITH]->(Technology | Sector)
//   correlation: float       -- -1 to 1
//   window_days: integer
//   updated_at: date

// (Commodity)-[:IS_INPUT_FOR]->(Sector | Technology | Company)
//   criticality: float       -- 0-100: how critical this input is

// (Commodity)-[:COMPETES_WITH]->(Commodity)
//   substitution_elasticity: float

// (Country)-[:TRADES_WITH]->(Country)
//   trade_value_usd_bn: float
//   year: integer
//   dominant_commodity: string

// ============================================================
// INITIAL MACRO SETUP QUERIES
// ============================================================

// Key country nodes
MERGE (c:Country {iso2: 'US'})  SET c.name = 'United States', c.region = 'Americas', c.is_g20 = true;
MERGE (c:Country {iso2: 'CN'})  SET c.name = 'China', c.region = 'Asia', c.is_g20 = true;
MERGE (c:Country {iso2: 'DE'})  SET c.name = 'Germany', c.region = 'Europe', c.is_g20 = true;
MERGE (c:Country {iso2: 'JP'})  SET c.name = 'Japan', c.region = 'Asia', c.is_g20 = true;
MERGE (c:Country {iso2: 'IN'})  SET c.name = 'India', c.region = 'Asia', c.is_g20 = true;
MERGE (c:Country {iso2: 'SA'})  SET c.name = 'Saudi Arabia', c.region = 'Middle East', c.is_g20 = true;
MERGE (c:Country {iso2: 'KR'})  SET c.name = 'South Korea', c.region = 'Asia', c.is_g20 = true;
MERGE (c:Country {iso2: 'TW'})  SET c.name = 'Taiwan', c.region = 'Asia', c.is_g20 = false;
MERGE (c:Country {iso2: 'AU'})  SET c.name = 'Australia', c.region = 'Oceania', c.is_g20 = true;
MERGE (c:Country {iso2: 'BR'})  SET c.name = 'Brazil', c.region = 'Americas', c.is_g20 = true;

// Key commodity nodes
MERGE (c:Commodity {commodity_id: 'WTI_CRUDE'})       SET c.name = 'WTI Crude Oil', c.category = 'energy', c.units = 'USD/barrel';
MERGE (c:Commodity {commodity_id: 'BRENT_CRUDE'})     SET c.name = 'Brent Crude Oil', c.category = 'energy', c.units = 'USD/barrel';
MERGE (c:Commodity {commodity_id: 'HENRY_HUB'})       SET c.name = 'Natural Gas (Henry Hub)', c.category = 'energy', c.units = 'USD/MMBtu';
MERGE (c:Commodity {commodity_id: 'COAL_THERMAL'})    SET c.name = 'Thermal Coal', c.category = 'energy', c.units = 'USD/MT';
MERGE (c:Commodity {commodity_id: 'URANIUM'})         SET c.name = 'Uranium', c.category = 'energy', c.units = 'USD/lb U3O8';
MERGE (c:Commodity {commodity_id: 'COPPER'})          SET c.name = 'Copper', c.category = 'metals', c.units = 'USD/MT';
MERGE (c:Commodity {commodity_id: 'LITHIUM'})         SET c.name = 'Lithium Carbonate', c.category = 'metals', c.units = 'USD/MT';
MERGE (c:Commodity {commodity_id: 'COBALT'})          SET c.name = 'Cobalt', c.category = 'metals', c.units = 'USD/MT';
MERGE (c:Commodity {commodity_id: 'SILICON_WAFER'})   SET c.name = 'Silicon Wafers', c.category = 'metals', c.units = 'USD/wafer';
MERGE (c:Commodity {commodity_id: 'RARE_EARTH'})      SET c.name = 'Rare Earth Elements', c.category = 'metals', c.units = 'Index';
MERGE (c:Commodity {commodity_id: 'CORN'})            SET c.name = 'Corn', c.category = 'agriculture', c.units = 'USD/bushel';
MERGE (c:Commodity {commodity_id: 'WHEAT'})           SET c.name = 'Wheat', c.category = 'agriculture', c.units = 'USD/bushel';
MERGE (c:Commodity {commodity_id: 'SOYBEAN'})         SET c.name = 'Soybean', c.category = 'agriculture', c.units = 'USD/bushel';
MERGE (c:Commodity {commodity_id: 'FERTILIZER_N'})    SET c.name = 'Nitrogen Fertilizer', c.category = 'agriculture', c.units = 'USD/MT';
MERGE (c:Commodity {commodity_id: 'BALTIC_DRY'})      SET c.name = 'Baltic Dry Index', c.category = 'freight', c.units = 'Index';
MERGE (c:Commodity {commodity_id: 'CONTAINER_RATE'})  SET c.name = 'Container Freight Rate', c.category = 'freight', c.units = 'USD/FEU';

// Key macro indicator nodes
MERGE (m:MacroIndicator {series_id: 'GDP'})       SET m.series_name = 'US Real GDP', m.source = 'fred', m.country = 'US', m.frequency = 'quarterly';
MERGE (m:MacroIndicator {series_id: 'CPIAUCSL'})  SET m.series_name = 'US CPI (All Urban)', m.source = 'fred', m.country = 'US', m.frequency = 'monthly';
MERGE (m:MacroIndicator {series_id: 'UNRATE'})    SET m.series_name = 'US Unemployment Rate', m.source = 'fred', m.country = 'US', m.frequency = 'monthly';
MERGE (m:MacroIndicator {series_id: 'DGS10'})     SET m.series_name = '10-Year Treasury Yield', m.source = 'fred', m.country = 'US', m.frequency = 'daily';
MERGE (m:MacroIndicator {series_id: 'DGS2'})      SET m.series_name = '2-Year Treasury Yield', m.source = 'fred', m.country = 'US', m.frequency = 'daily';
MERGE (m:MacroIndicator {series_id: 'FEDFUNDS'})  SET m.series_name = 'Federal Funds Rate', m.source = 'fred', m.country = 'US', m.frequency = 'monthly';
MERGE (m:MacroIndicator {series_id: 'INDPRO'})    SET m.series_name = 'Industrial Production Index', m.source = 'fred', m.country = 'US', m.frequency = 'monthly';
MERGE (m:MacroIndicator {series_id: 'M2SL'})      SET m.series_name = 'M2 Money Supply', m.source = 'fred', m.country = 'US', m.frequency = 'monthly';
MERGE (m:MacroIndicator {series_id: 'DCOILWTICO'}) SET m.series_name = 'WTI Crude Oil Price', m.source = 'fred', m.country = 'US', m.frequency = 'daily';
