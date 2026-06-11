"""India Supply Chain Database (India Pipeline — Layer 6).

Maintains explicit supply-chain graphs for key India sectors:
  Solar    → Cells → Wafers → Polysilicon
  Power    → Transformers → CRGO Steel
  Electronics → EMS → PCB → Components
  EV       → Battery Pack → Cells → Cathode → Lithium
  Railway  → Rolling Stock → Traction Motor → Steel / Copper
  5G       → Antenna/Radio → Optical Fiber → Preform

Each supply chain node carries:
  - domestic suppliers (India companies)
  - import dependency flag
  - capacity constraint signal words (for signal matching)
  - estimated lead time
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SupplyChainNode:
    name: str                        # canonical product/component name
    sector: str                      # parent sector
    stage: int                       # 1 = end-product, 2 = subcomponent, 3 = raw material
    parent: Optional[str]            # upstream parent node name (None for root)
    domestic_suppliers: list[str]    # known Indian companies at this node
    is_import_dependent: bool
    import_share: float              # 0.0–1.0
    constraint_keywords: list[str]   # NLP keywords that indicate supply pressure
    lead_time_weeks: int             # typical sourcing lead time
    bottleneck_risk: str             # "critical" | "high" | "moderate" | "low"


# ---------------------------------------------------------------------------
# India Supply Chain Graph — static knowledge base
# ---------------------------------------------------------------------------

INDIA_SUPPLY_CHAIN: list[SupplyChainNode] = [

    # ── Solar supply chain ─────────────────────────────────────────────────
    SupplyChainNode("Solar Module", "solar", 1, None,
        domestic_suppliers=["Waaree Energies", "Adani Solar", "Vikram Solar",
                             "Goldi Solar", "RenewSys", "Tata Power Solar"],
        is_import_dependent=False, import_share=0.10,
        constraint_keywords=["module shortage", "capacity booked", "delivery delay",
                              "order backlog", "sold out", "long lead time"],
        lead_time_weeks=8, bottleneck_risk="moderate"),

    SupplyChainNode("Solar Cell", "solar", 2, "Solar Module",
        domestic_suppliers=["Adani Solar", "Waaree Energies", "Vikram Solar",
                             "Premier Energies"],
        is_import_dependent=True, import_share=0.75,
        constraint_keywords=["cell shortage", "cell import", "cell availability",
                              "cell supply", "cell constraint"],
        lead_time_weeks=14, bottleneck_risk="high"),

    SupplyChainNode("Solar Wafer", "solar", 3, "Solar Cell",
        domestic_suppliers=[],
        is_import_dependent=True, import_share=0.98,
        constraint_keywords=["wafer shortage", "wafer import", "wafer supply",
                              "wafer constraint", "wafer availability"],
        lead_time_weeks=20, bottleneck_risk="critical"),

    SupplyChainNode("Polysilicon", "solar", 4, "Solar Wafer",
        domestic_suppliers=[],
        is_import_dependent=True, import_share=1.00,
        constraint_keywords=["polysilicon", "poly shortage", "poly supply",
                              "silicon feedstock"],
        lead_time_weeks=26, bottleneck_risk="critical"),

    SupplyChainNode("Solar Glass", "solar", 2, "Solar Module",
        domestic_suppliers=["Borosil Renewables", "Asahi India Glass"],
        is_import_dependent=True, import_share=0.30,
        constraint_keywords=["glass shortage", "solar glass", "anti-reflective glass"],
        lead_time_weeks=10, bottleneck_risk="moderate"),

    # ── Power Transformer / Transmission supply chain ──────────────────────
    SupplyChainNode("Power Transformer", "power_transmission", 1, None,
        domestic_suppliers=["Hitachi Energy India", "CG Power", "ABB India",
                             "Transformers & Rectifiers India", "EMCO", "Voltamp",
                             "Indo Tech Transformers", "Bharat Heavy Electricals"],
        is_import_dependent=False, import_share=0.15,
        constraint_keywords=["transformer shortage", "transformer backlog",
                              "lead time extended", "transformer waiting time",
                              "transformer capacity full", "order backlog"],
        lead_time_weeks=52, bottleneck_risk="critical"),

    SupplyChainNode("CRGO Steel", "power_transmission", 2, "Power Transformer",
        domestic_suppliers=["SAIL (limited)", "JSW Steel (limited)"],
        is_import_dependent=True, import_share=0.75,
        constraint_keywords=["CRGO shortage", "CRGO import", "core steel",
                              "cold rolled grain oriented", "electrical steel"],
        lead_time_weeks=24, bottleneck_risk="high"),

    SupplyChainNode("Copper Winding Wire", "power_transmission", 2, "Power Transformer",
        domestic_suppliers=["Sterlite Copper", "Hindustan Copper", "KEC International",
                             "Polycab", "Havells"],
        is_import_dependent=False, import_share=0.20,
        constraint_keywords=["copper shortage", "winding wire", "copper price surge",
                              "copper supply", "conductor shortage"],
        lead_time_weeks=12, bottleneck_risk="moderate"),

    SupplyChainNode("HV Cable", "power_transmission", 2, "Power Transformer",
        domestic_suppliers=["KEC International", "Polycab", "Sterlite Power",
                             "Finolex Cables", "KEI Industries"],
        is_import_dependent=False, import_share=0.25,
        constraint_keywords=["cable shortage", "cable supply", "HV cable",
                              "EHV cable", "HVDC cable", "cable backlog"],
        lead_time_weeks=20, bottleneck_risk="high"),

    # ── Electronics / EMS supply chain ────────────────────────────────────
    SupplyChainNode("Consumer Electronics", "electronics_manufacturing", 1, None,
        domestic_suppliers=["Dixon Technologies", "Amber Enterprises", "Kaynes Technology",
                             "Syrma SGS", "VVDN Technologies"],
        is_import_dependent=False, import_share=0.20,
        constraint_keywords=["electronics shortage", "supply chain disruption",
                              "component shortage"],
        lead_time_weeks=12, bottleneck_risk="moderate"),

    SupplyChainNode("EMS / Contract Manufacturing", "electronics_manufacturing", 2,
        "Consumer Electronics",
        domestic_suppliers=["Dixon Technologies", "Amber Enterprises", "Kaynes Technology",
                             "Syrma SGS", "Avalon Technologies"],
        is_import_dependent=False, import_share=0.25,
        constraint_keywords=["EMS capacity full", "contract manufacturing backlog",
                              "PCB assembly shortage", "EMS lead time"],
        lead_time_weeks=16, bottleneck_risk="high"),

    SupplyChainNode("PCB / Printed Circuit Board", "electronics_manufacturing", 3,
        "EMS / Contract Manufacturing",
        domestic_suppliers=["AT&S India", "Genus Power (PCB)", "SFPL"],
        is_import_dependent=True, import_share=0.80,
        constraint_keywords=["PCB shortage", "PCB supply", "printed circuit board",
                              "PCB import", "PCB lead time"],
        lead_time_weeks=20, bottleneck_risk="critical"),

    SupplyChainNode("Semiconductor IC", "electronics_manufacturing", 4,
        "PCB / Printed Circuit Board",
        domestic_suppliers=["Tata Semiconductor (planned)", "CG Power ATMP (planned)"],
        is_import_dependent=True, import_share=0.95,
        constraint_keywords=["chip shortage", "semiconductor shortage", "IC shortage",
                              "chip supply", "component allocation"],
        lead_time_weeks=40, bottleneck_risk="critical"),

    # ── EV / Battery supply chain ──────────────────────────────────────────
    SupplyChainNode("EV Battery Pack", "electric_vehicle", 1, None,
        domestic_suppliers=["Tata AutoComp", "Exide Industries", "Amara Raja",
                             "Greenko", "Lucas TVS"],
        is_import_dependent=True, import_share=0.60,
        constraint_keywords=["battery shortage", "battery supply", "cell shortage",
                              "pack shortage", "battery capacity"],
        lead_time_weeks=24, bottleneck_risk="high"),

    SupplyChainNode("Battery Cell (Li-ion)", "battery_storage", 2, "EV Battery Pack",
        domestic_suppliers=["ACME Solar (planned)", "Ola Electric (planned)",
                             "Rajesh Exports (ACC)"],
        is_import_dependent=True, import_share=0.90,
        constraint_keywords=["cell shortage", "cell supply", "Li-ion cell",
                              "GWh capacity", "cell availability"],
        lead_time_weeks=32, bottleneck_risk="critical"),

    SupplyChainNode("Cathode Active Material", "battery_storage", 3, "Battery Cell (Li-ion)",
        domestic_suppliers=[],
        is_import_dependent=True, import_share=0.95,
        constraint_keywords=["cathode shortage", "LFP material", "NMC material",
                              "cathode supply"],
        lead_time_weeks=20, bottleneck_risk="critical"),

    SupplyChainNode("Lithium", "battery_storage", 4, "Cathode Active Material",
        domestic_suppliers=[],
        is_import_dependent=True, import_share=1.00,
        constraint_keywords=["lithium shortage", "lithium supply", "lithium price",
                              "Li supply", "lithium import"],
        lead_time_weeks=28, bottleneck_risk="critical"),

    # ── Railway supply chain ───────────────────────────────────────────────
    SupplyChainNode("Rolling Stock / Locomotives", "railway_infrastructure", 1, None,
        domestic_suppliers=["BEML", "Bharat Heavy Electricals (BHEL)", "Titagarh Wagons",
                             "Jupiter Wagons", "Texmaco Rail"],
        is_import_dependent=False, import_share=0.15,
        constraint_keywords=["wagon shortage", "locomotive order", "rolling stock supply",
                              "coach backlog"],
        lead_time_weeks=52, bottleneck_risk="moderate"),

    SupplyChainNode("Traction Motor", "railway_infrastructure", 2, "Rolling Stock / Locomotives",
        domestic_suppliers=["ABB India", "Siemens India", "BHEL", "CG Power"],
        is_import_dependent=True, import_share=0.40,
        constraint_keywords=["traction motor shortage", "motor supply", "traction equipment"],
        lead_time_weeks=36, bottleneck_risk="high"),

    SupplyChainNode("Railway Steel (Rail/Wheel)", "railway_infrastructure", 3,
        "Rolling Stock / Locomotives",
        domestic_suppliers=["Steel Authority of India (SAIL)", "JSW Steel",
                             "Tata Steel"],
        is_import_dependent=False, import_share=0.10,
        constraint_keywords=["rail steel shortage", "wheel shortage",
                              "steel supply for railways"],
        lead_time_weeks=16, bottleneck_risk="low"),

    # ── 5G / Telecom supply chain ──────────────────────────────────────────
    SupplyChainNode("5G BTS / Radio Unit", "5g_telecom", 1, None,
        domestic_suppliers=["VVDN Technologies", "Tejas Networks", "HFCL",
                             "ITI Limited"],
        is_import_dependent=True, import_share=0.70,
        constraint_keywords=["5G equipment shortage", "radio unit supply",
                              "BTS availability", "5G rollout delay"],
        lead_time_weeks=24, bottleneck_risk="high"),

    SupplyChainNode("Optical Fiber Cable", "5g_telecom", 2, "5G BTS / Radio Unit",
        domestic_suppliers=["Sterlite Technologies", "Finolex Cables", "HFCL",
                             "Birla Cable"],
        is_import_dependent=False, import_share=0.20,
        constraint_keywords=["fiber shortage", "optical fiber supply",
                              "cable shortage", "fiber backlog"],
        lead_time_weeks=16, bottleneck_risk="moderate"),

    SupplyChainNode("Optical Fiber Preform", "5g_telecom", 3, "Optical Fiber Cable",
        domestic_suppliers=["Sterlite Technologies (limited)"],
        is_import_dependent=True, import_share=0.55,
        constraint_keywords=["preform shortage", "OVD capacity", "fiber preform"],
        lead_time_weeks=28, bottleneck_risk="high"),
]

# Build lookup index: name → node
_NODE_INDEX: dict[str, SupplyChainNode] = {n.name: n for n in INDIA_SUPPLY_CHAIN}

# Build adjacency list: parent → [children]
_CHILDREN: dict[str, list[str]] = {}
for _node in INDIA_SUPPLY_CHAIN:
    if _node.parent:
        _CHILDREN.setdefault(_node.parent, []).append(_node.name)


class IndiaSupplyChainDB:
    """Layer 6: Query the India supply chain graph."""

    def get_chain(self, root: str) -> list[SupplyChainNode]:
        """Return all nodes in a supply chain starting from root, BFS order."""
        result: list[SupplyChainNode] = []
        queue = [root]
        visited: set[str] = set()
        while queue:
            name = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)
            node = _NODE_INDEX.get(name)
            if node:
                result.append(node)
                queue.extend(_CHILDREN.get(name, []))
        return result

    def get_bottleneck_nodes(
        self, severity: str = "critical"
    ) -> list[SupplyChainNode]:
        """Return all supply chain nodes at or above the given severity level."""
        order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
        threshold = order.get(severity, 2)
        return [
            n for n in INDIA_SUPPLY_CHAIN
            if order.get(n.bottleneck_risk, 9) <= threshold
        ]

    def get_by_sector(self, sector: str) -> list[SupplyChainNode]:
        return [n for n in INDIA_SUPPLY_CHAIN if n.sector == sector]

    def get_domestic_suppliers(self, component: str) -> list[str]:
        node = _NODE_INDEX.get(component)
        return node.domestic_suppliers if node else []

    def get_constraint_keywords(self) -> dict[str, list[str]]:
        """Return mapping of component name → constraint NLP keywords.
        Used by OrderBookPressureDetector for signal scanning."""
        return {n.name: n.constraint_keywords for n in INDIA_SUPPLY_CHAIN}

    def get_upstream_path(self, component: str) -> list[SupplyChainNode]:
        """Walk up the supply chain from a component to its root."""
        path: list[SupplyChainNode] = []
        current = _NODE_INDEX.get(component)
        visited: set[str] = set()
        while current and current.name not in visited:
            path.append(current)
            visited.add(current.name)
            current = _NODE_INDEX.get(current.parent) if current.parent else None
        return path

    def all_nodes(self) -> list[SupplyChainNode]:
        return list(INDIA_SUPPLY_CHAIN)
