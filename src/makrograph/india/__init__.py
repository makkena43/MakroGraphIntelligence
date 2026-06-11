"""India-specific upstream intelligence layers.

Adds 10 engines that feed into the existing bottleneck, causal-chain,
and ranking engines — without touching the US pipeline at all.

Target architecture (India):
    Policy Docs + Concalls + Filings + Tender Data + Import Data
        → PolicyIntelligenceEngine
        → CapacityRequirementGenerator
        → CapacityGapDetector
        → ImportDependencyEngine
        → LocalizationOpportunityEngine
        → IndiaSupplyChainDB
        → BeneficiaryDiscoveryLayer
        → TenderIntelligence
        → OrderBookPressureDetector
        → IndiaCausalChainGenerator
        → [existing] Supply Bottlenecks → Ranking
"""
