#!/usr/bin/env python3
"""
quote_assistant_poc.py
======================
Requirements: Python 3.9+. Standard library ONLY. No API keys, no network, no paid
services. Every "LLM" call goes through the LanguageModel interface (§8); the default
implementation is a deterministic mock so the eval numbers are reproducible. A clearly
marked adapter shows exactly where a real model plugs in.

WHAT IS REAL AND WHAT IS FAKED
------------------------------
Real:   the control flow, the anti-corruption layer, entity resolution, tool planning,
        the deterministic pricing engine, the guard rails, citation plumbing, the eval
        harness and its metrics.
Faked:  the five source systems (synthetic but deliberately messy), the dense retriever
        (TF-IDF cosine stands in for hybrid BM25 + embeddings), and the LLM (a mock that
        does regex intent extraction and template writing).
See the walkthrough for what each fake is standing in for and what breaks first at scale.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import sys
import time
import unicodedata
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# §0  GLOBAL CONSTANTS AND POLICY
# ---------------------------------------------------------------------------
# TODAY is frozen so the demo is reproducible. In production this is date.today();
# freezing it is what lets "the Q1 discount has expired" be a deterministic test.
TODAY: date = date(2026, 8, 19)

# The maximum discount a salesperson may apply without approval. The assistant REPORTS
# this policy; it never grants authority. Anything above goes to a human.
MAX_SELF_SERVE_DISCOUNT_PCT: Decimal = Decimal("15")

MONEY = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Bankers beware: commercial rounding is half-up, not half-even."""
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def usd(value: Decimal) -> str:
    return f"USD {money(value):,.2f}"


# ---------------------------------------------------------------------------
# §1  CANONICAL DOMAIN MODEL  (the output side of the anti-corruption layer)
#
# Nothing above this line in the call graph ever sees a CSV row, a scraped dict or a
# REST payload. When the configurator finally ships a real API, exactly one connector
# changes and the orchestrator does not.
# ---------------------------------------------------------------------------

SourceSystem = str  # "CRM" | "PRICING" | "CONFIG" | "DOCS" | "HISTORY"


@dataclass(frozen=True)
class Provenance:
    """Every fact carries where it came from and which record it came from."""

    source_system: SourceSystem
    record_id: str
    as_of: date
    tier: int  # 1 = live documented API, 2 = batch export, 3 = unclear / manual
    confidence: float = 1.0

    def cite(self) -> str:
        # as_of stays on every citation: it is what makes a number reconstructable
        # months later during a margin review, which is the whole audit requirement.
        return f"[{self.source_system}:{self.record_id} @{self.as_of.isoformat()}]"


@dataclass(frozen=True)
class Fact:
    """An atomic, citable claim. The writer may only emit text derived from these."""

    key: str
    value: Any
    display: str  # human-readable rendering, produced deterministically in Python
    prov: Provenance

    def line(self) -> str:
        return f"{self.display} {self.prov.cite()}"


@dataclass(frozen=True)
class CanonicalCustomer:
    canonical_id: str
    name: str
    country: str
    price_list: str
    prov: Provenance


@dataclass(frozen=True)
class CanonicalProduct:
    canonical_id: str  # OUR key, owned by us, stable across connector churn
    display_name: str
    source_ids: Dict[SourceSystem, str]
    match_method: str  # "exact" | "normalised" | "fuzzy" | "manual_crosswalk"
    match_score: float


@dataclass(frozen=True)
class PriceRow:
    sku: str
    price_list: str
    currency: str
    list_price: Decimal
    valid_from: date
    valid_to: date
    prov: Provenance

    def valid_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True)
class DiscountRule:
    discount_id: str
    customer_id: str
    sku_prefix: str
    pct: Decimal
    valid_from: date
    valid_to: date
    label: str
    prov: Provenance

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True)
class ConfigRule:
    rule_id: str
    product: str
    kind: str  # "incompatible" | "requires"
    subject: str
    object_: str
    message: str
    prov: Provenance


@dataclass(frozen=True)
class HistoricalQuote:
    quote_id: str
    customer_id: str
    quote_date: date
    sku: str
    quantity: int
    net_total: Decimal
    discount_pct: Decimal
    prov: Provenance


@dataclass(frozen=True)
class DocChunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    product: Optional[str]
    version: str
    superseded: bool
    text: str
    prov: Provenance


# ---------------------------------------------------------------------------
# §2  SYNTHETIC SOURCE SYSTEMS, IN THEIR NATIVE (MESSY) FORMATS
#
# The messiness is the point. Each defect below maps to something I have actually
# hit in the wild:
#   - CRM and pricing disagree on product identifiers          -> entity resolution
#   - a superseded price row still sits in the export          -> validity filtering
#   - a European decimal comma in one row                      -> normalisation
#   - a campaign discount that expired in March                -> temporal logic
#   - CLN-1 + WSH-2 are physically incompatible                -> config rules
#   - a v2.4 datasheet with a DIFFERENT noise figure           -> version trap
#   - no system anywhere knows the CLN-1 lead time             -> honest "unknown"
#   - the CRM references AX-770, which exists nowhere else     -> unresolvable entity
# ---------------------------------------------------------------------------

# --- CRM: Tier 1, a clean documented API -----------------------------------------
_CRM_CUSTOMERS: Dict[str, Dict[str, Any]] = {
    "C-2001": {
        "id": "C-2001",
        "name": "BMW Group",
        "country": "DE",
        "price_list": "TIER-A",
        "products_of_interest": ["AX-620", "AX-940"],
        "last_modified": "2026-08-19",
    },
    "C-2002": {
        "id": "C-2002",
        "name": "Foxconn Technology Group",
        "country": "TW",
        "price_list": "TIER-B",
        "products_of_interest": ["SN-30", "AX-620"],
        "last_modified": "2026-08-18",
    },
    "C-2003": {
        # No discount agreement of any kind — exercises the quoted-at-list path.
        "id": "C-2003",
        "name": "Baxter International Inc.",
        "country": "US",
        "price_list": "TIER-B",
        "products_of_interest": ["AX-940"],
        "last_modified": "2026-08-11",
    },
}

# CRM's own product catalogue. Note AX-770: a model marketing put in the CRM that
# exists in no other system. The assistant must refuse, not improvise.
_CRM_PRODUCTS: Dict[str, str] = {
    "AX-620": "Axiom 620 Robot Arm",
    "AX-940": "Axiom 940 Robot Arm",
    "SN-30": "Sentinel S30 Mobile Robot",
    "AX-770": "Axiom 770 Robot Arm (pre-launch)",
}


class CrmApi:
    """Tier 1. Typed, documented, live. The easy one — every landscape has exactly one."""

    def __init__(self) -> None:
        self.call_count = 0

    def find_customer(self, name_fragment: str) -> Optional[Dict[str, Any]]:
        self.call_count += 1
        time.sleep(0.008)  # stand-in for a real network round trip
        needle = _norm(name_fragment)
        for rec in _CRM_CUSTOMERS.values():
            if needle and (needle in _norm(rec["name"]) or _norm(rec["name"]).startswith(needle)):
                return dict(rec)
        return None

    def list_products(self) -> Dict[str, str]:
        self.call_count += 1
        return dict(_CRM_PRODUCTS)


# --- PRICING: Tier 2, a nightly CSV batch export ----------------------------------
# Batch rather than live, so every price claim it produces carries the export date.
_PRICING_EXPORT_TIMESTAMP = "2026-08-13T02:14:00"

_PRICING_CSV = """sku,price_list,currency,list_price,valid_from,valid_to
ARX-620-STD,TIER-A,USD,48600.00,2026-01-01,2026-12-31
ARX-620-STD,TIER-A,USD,46900.00,2025-01-01,2025-12-31
ARX-620-STD,TIER-B,USD,51 200‚00,2026-01-01,2026-12-31
ARX-940-STD,TIER-A,USD,86400.00,2026-01-01,2026-12-31
ARX-940-STD,TIER-B,USD,89900.00,2026-01-01,2026-12-31
SENTINEL-S30,TIER-A,USD,34500.00,2026-01-01,2026-12-31
SENTINEL-S30,TIER-B,USD,36200.00,2026-01-01,2026-12-31
OPT-CLN-1,TIER-A,USD,7900.00,2026-01-01,2026-12-31
OPT-CLN-1,TIER-B,USD,8250.00,2026-01-01,2026-12-31
OPT-WSH-2,TIER-A,USD,6400.00,2026-01-01,2026-12-31
OPT-WSH-2,TIER-B,USD,6700.00,2026-01-01,2026-12-31
OPT-FTS-5,TIER-A,USD,5250.00,2026-01-01,2026-12-31
OPT-FTS-5,TIER-B,USD,5480.00,2026-01-01,2026-12-31
"""
# NB the TIER-B row for ARX-620-STD uses a European decimal comma (written with a low
# comma so the CSV parser does not split on it). _parse_money handles both conventions.

_DISCOUNT_CSV = """discount_id,customer_id,sku_prefix,pct,valid_from,valid_to,label
D-8801,C-2001,ARX-,12,2026-01-01,2026-03-31,Q1 Campaign 2026
D-8802,C-2001,ARX-,8,2026-01-01,2026-12-31,Volume Agreement 2026
D-8803,C-2002,SENTINEL-,5,2026-06-01,2026-09-30,AMR Promotion H2
"""


# --- CONFIGURATOR: Tier 3, flaky service with rate limiting and an outage ----------
_CONFIG_RULES: Dict[str, List[Dict[str, str]]] = {
    "AXIOM_620": [
        {
            "rule_id": "CFG-113",
            "kind": "incompatible",
            "subject": "CLN-1",
            "object": "WSH-2",
            "message": "Cleanroom Package CLN-1 cannot be combined with Washdown Package "
            "WSH-2: the IP69K seals require a joint lubricant that does not meet ISO "
            "Class 5 particulate limits.",
        },
        {
            "rule_id": "CFG-118",
            "kind": "requires",
            "subject": "FTS-5",
            "object": "FIRMWARE>=4.2",
            "message": "Force-Torque Sensor FTS-5 requires controller firmware 4.2 or later.",
        },
    ],
    "AXIOM_940": [
        {
            "rule_id": "CFG-204",
            "kind": "requires",
            "subject": "WSH-2",
            "object": "REINFORCED-BASE",
            "message": "Washdown Package WSH-2 on the 940 requires the reinforced base.",
        }
    ],
    # SENTINEL_S30 intentionally absent: the service is down for this product family.
}


class ConfigServiceError(Exception):
    pass


class TransientError(ConfigServiceError):
    """HTTP 429 / connection reset. Retryable."""


class OutageError(ConfigServiceError):
    """The endpoint for this product family is simply not answering. Not retryable."""


class FlakyConfiguratorService:
    """Tier 3. Rate-limited and partially unavailable, with no contract.

    Failure pattern is deterministic (every third call is rate-limited, and the Sentinel
    family is permanently down) so that eval runs are reproducible. Randomness here
    would make the harness lie to us.
    """

    def __init__(self) -> None:
        self._calls = 0
        self.transient_failures = 0
        self.outages = 0

    def get_rules(self, config_product_id: str) -> List[Dict[str, str]]:
        self._calls += 1
        time.sleep(0.01)
        if config_product_id not in _CONFIG_RULES:
            self.outages += 1
            raise OutageError(f"503 no rule endpoint for product family {config_product_id}")
        if self._calls % 3 == 0:
            self.transient_failures += 1
            raise TransientError("429 rate limit exceeded (10 req/min)")
        return [dict(r) for r in _CONFIG_RULES[config_product_id]]


# --- DOCUMENT REPOSITORY: Tier 3, unstructured free text --------------------------
# Note DOC-4409: a superseded v2.4 datasheet quoting 71 dB(A) where the current v3.1
# says 68 dB(A). A naive vector index over "everything in SharePoint" returns the wrong
# one roughly half the time, confidently and without warning.
_DOCS: List[Dict[str, Any]] = [
    {
        "doc_id": "DOC-4410",
        "title": "Axiom 620 Robot Arm — Technical Datasheet v3.1",
        "product": "AXIOM-620",
        "version": "3.1",
        "superseded": False,
        "published": "2026-04-02",
        "body": """
Axiom 620 six-axis articulated robot. Industrial manipulator for assembly, machine
tending and material handling.

Specifications:
Payload: 20 kg
Reach: 1811 mm
Repeatability: 0.03 mm
Axes: 6
Sound pressure level: 68 dB(A) at 1 m
Weight: 255 kg
Protection rating: IP54
Mounting: floor, ceiling or wall

Options and accessories:
The Cleanroom Package (CLN-1) qualifies the arm to ISO Class 5 with low-particulate
lubricant and sealed joint covers. The Washdown Package (WSH-2) raises protection to
IP69K for high-pressure, high-temperature cleaning. The Force-Torque Sensor (FTS-5)
adds six-axis force sensing at the wrist flange for contact-rich assembly.

Service:
Recommended service interval is 8000 operating hours or annually, whichever comes first.
""",
    },
    {
        "doc_id": "DOC-4409",
        "title": "Axiom 620 Robot Arm — Technical Datasheet v2.4 (SUPERSEDED)",
        "product": "AXIOM-620",
        "version": "2.4",
        "superseded": True,
        "published": "2024-06-11",
        "body": """
Axiom 620 six-axis articulated robot.

Specifications:
Payload: 20 kg
Reach: 1798 mm
Repeatability: 0.05 mm
Sound pressure level: 71 dB(A) at 1 m
Weight: 268 kg
""",
    },
    {
        "doc_id": "DOC-4411",
        "title": "Axiom 940 Robot Arm — Technical Datasheet v2.0",
        "product": "AXIOM-940",
        "version": "2.0",
        "superseded": False,
        "published": "2026-02-20",
        # Deliberately has NO sound pressure figure. The assistant must say so.
        "body": """
Axiom 940 six-axis articulated robot. High-payload manipulator for palletising and
spot welding.

Specifications:
Payload: 130 kg
Reach: 2650 mm
Repeatability: 0.05 mm
Axes: 6
Weight: 1120 kg
Protection rating: IP54

Options and accessories:
The Washdown Package (WSH-2) is available with the reinforced base only.
""",
    },
    {
        "doc_id": "DOC-4420",
        "title": "Sentinel S30 Mobile Robot — Technical Datasheet v1.2",
        "product": "SENTINEL-S30",
        "version": "1.2",
        "superseded": False,
        "published": "2025-11-04",
        "body": """
Sentinel S30 autonomous mobile robot. Differential-drive AMR for intralogistics.

Specifications:
Payload: 300 kg
Top speed: 1.8 m/s
Navigation: SLAM with 2D safety lidar
Battery runtime: 9 h
Weight: 190 kg
""",
    },
    {
        "doc_id": "DOC-4430",
        "title": "Commercial Terms Summary 2026",
        "product": None,
        "version": "1.0",
        "superseded": False,
        "published": "2026-01-15",
        "body": """
Standard payment terms are 30 days net. Warranty is 24 months from commissioning or 30
months from delivery. Quotations are valid for 30 days from issue unless otherwise
stated. Delivery terms are DAP to the customer site.
""",
    },
]


# --- HISTORICAL QUOTES: Tier 2, a JSON dump ---------------------------------------
_HISTORY_JSON = json.dumps(
    {
        "exported_at": "2026-08-17T23:00:00",
        "quotes": [
            {
                "quote_id": "Q-7781",
                "customer": "C-2001",
                "date": "2026-02-14",
                "sku": "ARX-620-STD",
                "qty": 1,
                "discount_pct": 12,
                "net_total": 42768.00,
            },
            {
                "quote_id": "Q-7802",
                "customer": "C-2002",
                "date": "2026-06-30",
                "sku": "SENTINEL-S30",
                "qty": 2,
                "discount_pct": 5,
                "net_total": 68780.00,
            },
            {
                "quote_id": "Q-7815",
                "customer": "C-2003",
                "date": "2026-07-22",
                "sku": "ARX-940-STD",
                "qty": 1,
                "discount_pct": 0,
                "net_total": 89900.00,
            },
        ],
    }
)


# ---------------------------------------------------------------------------
# §3  UTILITIES: normalisation used by both connectors and entity resolution
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Lowercase, strip accents and punctuation. Boring, and does most of the work."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _parse_money(raw: str) -> Decimal:
    """Handle 12400.00 / 12,400.00 / 12 400,00 / 12 400‚00 without guessing wrongly."""
    s = raw.strip().replace("\u00a0", "").replace(" ", "").replace("\u201a", ",")
    if "," in s and "." in s:
        s = s.replace(",", "")  # comma is a thousands separator
    elif "," in s:
        s = s.replace(",", ".")  # comma is the decimal separator
    return Decimal(s)


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# §4  CONNECTORS  (the anti-corruption layer itself)
#
# One class per source. Each owns its transport quirks, its retry policy and its
# export date, and each returns ONLY canonical objects.
# ---------------------------------------------------------------------------


class CrmConnector:
    """Tier 1 — live documented API."""

    TIER = 1

    def __init__(self, api: CrmApi) -> None:
        self.api = api

    def find_customer(self, name_fragment: str) -> Optional[CanonicalCustomer]:
        rec = self.api.find_customer(name_fragment)
        if rec is None:
            return None
        return CanonicalCustomer(
            canonical_id=rec["id"],
            name=rec["name"],
            country=rec["country"],
            price_list=rec["price_list"],
            prov=Provenance("CRM", rec["id"], _parse_date(rec["last_modified"]), self.TIER),
        )

    def product_identifiers(self) -> Dict[str, str]:
        return self.api.list_products()


class PricingConnector:
    """Tier 2 — nightly CSV export. Parsed once at start-up, then served from memory."""

    TIER = 2

    def __init__(self, prices_csv: str, discounts_csv: str, exported_at: str) -> None:
        self.as_of = _parse_date(exported_at)
        self.prices: List[PriceRow] = []
        self.discounts: List[DiscountRule] = []

        for i, row in enumerate(csv.DictReader(io.StringIO(prices_csv))):
            rid = f"PR-{i:03d}"
            self.prices.append(
                PriceRow(
                    sku=row["sku"].strip(),
                    price_list=row["price_list"].strip(),
                    currency=row["currency"].strip(),
                    list_price=_parse_money(row["list_price"]),
                    valid_from=_parse_date(row["valid_from"]),
                    valid_to=_parse_date(row["valid_to"]),
                    prov=Provenance("PRICING", rid, self.as_of, self.TIER),
                )
            )
        for row in csv.DictReader(io.StringIO(discounts_csv)):
            self.discounts.append(
                DiscountRule(
                    discount_id=row["discount_id"].strip(),
                    customer_id=row["customer_id"].strip(),
                    sku_prefix=row["sku_prefix"].strip(),
                    pct=Decimal(row["pct"].strip()),
                    valid_from=_parse_date(row["valid_from"]),
                    valid_to=_parse_date(row["valid_to"]),
                    label=row["label"].strip(),
                    prov=Provenance("PRICING", row["discount_id"].strip(), self.as_of, self.TIER),
                )
            )

    def sku_identifiers(self) -> List[str]:
        return sorted({p.sku for p in self.prices if not p.sku.startswith("OPT-")})

    def get_price(self, sku: str, price_list: str, on: date) -> Optional[PriceRow]:
        """Validity window filtering happens HERE, not in the model's head. The 2025 row
        for ARX-620-STD is semantically near-identical to the 2026 row; an embedding
        index cannot tell them apart, a date comparison can."""
        time.sleep(0.004)
        candidates = [
            p for p in self.prices
            if p.sku == sku and p.price_list == price_list and p.valid_on(on)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.valid_from)

    def get_discounts(self, customer_id: str, sku: str) -> List[DiscountRule]:
        """Returns ALL matching rules, expired included, so the caller can explain why a
        discount the salesperson expects is not being applied."""
        return [d for d in self.discounts if d.customer_id == customer_id and sku.startswith(d.sku_prefix)]


class ConfigConnector:
    """Tier 3 — flaky service. Retries transients, degrades on outages, never guesses."""

    TIER = 3
    MAX_RETRIES = 2

    def __init__(self, service: FlakyConfiguratorService) -> None:
        self.service = service
        self.degraded_calls = 0

    def get_rules(self, config_product_id: str) -> Tuple[Optional[List[ConfigRule]], Optional[str]]:
        """Returns (rules, error). rules=None means 'we do not know', which is a
        materially different answer from 'no rules apply' and must never be collapsed
        into it."""
        last_error: Optional[str] = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                raw = self.service.get_rules(config_product_id)
                return (
                    [
                        ConfigRule(
                            rule_id=r["rule_id"],
                            product=config_product_id,
                            kind=r["kind"],
                            subject=r["subject"],
                            object_=r["object"],
                            message=r["message"],
                            prov=Provenance("CONFIG", r["rule_id"], TODAY, self.TIER, confidence=0.9),
                        )
                        for r in raw
                    ],
                    None,
                )
            except TransientError as exc:
                last_error = str(exc)
                time.sleep(0.02 * (attempt + 1))  # linear backoff; jittered exponential in prod
            except OutageError as exc:
                self.degraded_calls += 1
                return None, str(exc)
        self.degraded_calls += 1
        return None, f"retries exhausted: {last_error}"


class DocsConnector:
    """Tier 3 — free text. Chunks by section, keeps version metadata, extracts specs."""

    TIER = 3

    def __init__(self, docs: Sequence[Dict[str, Any]]) -> None:
        self.chunks: List[DocChunk] = []
        for doc in docs:
            for j, section in enumerate(self._split_sections(doc["body"])):
                self.chunks.append(
                    DocChunk(
                        chunk_id=f"{doc['doc_id']}#c{j}",
                        doc_id=doc["doc_id"],
                        doc_title=doc["title"],
                        product=doc["product"],
                        version=doc["version"],
                        superseded=doc["superseded"],
                        text=section,
                        prov=Provenance("DOCS", f"{doc['doc_id']}#c{j}", _parse_date(doc["published"]), self.TIER),
                    )
                )

    @staticmethod
    def _split_sections(body: str) -> List[str]:
        """Structure-aware chunking: split on blank lines, never inside a spec block.
        Crude, but it preserves the one property that matters — a spec line and its
        heading stay together, so retrieval never returns a dangling number."""
        return [blk.strip() for blk in re.split(r"\n\s*\n", body.strip()) if blk.strip()]

    def extract_specs(self, product: str) -> Dict[str, Tuple[str, DocChunk]]:
        """Parse 'Label: value' lines out of the CURRENT datasheet only.

        This is deliberately NOT a retrieval question. A spec lookup is an exact lookup;
        routing it through similarity search is how you end up quoting the superseded
        v2.4 figure of 71 dB(A)."""
        specs: Dict[str, Tuple[str, DocChunk]] = {}
        for ch in self.chunks:
            if ch.product != product or ch.superseded:
                continue
            for line in ch.text.splitlines():
                m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /()°.\-]+?)\s*:\s*(.+?)\s*$", line)
                if m:
                    specs[_norm(m.group(1))] = (m.group(2).strip(), ch)
        return specs

    def product_identifiers(self) -> List[str]:
        return sorted({c.product for c in self.chunks if c.product})


class HistoryConnector:
    """Tier 2 — JSON dump from the commercial system."""

    TIER = 2

    def __init__(self, payload: str) -> None:
        data = json.loads(payload)
        self.as_of = _parse_date(data["exported_at"])
        self.quotes = [
            HistoricalQuote(
                quote_id=q["quote_id"],
                customer_id=q["customer"],
                quote_date=_parse_date(q["date"]),
                sku=q["sku"],
                quantity=int(q["qty"]),
                net_total=Decimal(str(q["net_total"])),
                discount_pct=Decimal(str(q["discount_pct"])),
                prov=Provenance("HISTORY", q["quote_id"], self.as_of, self.TIER),
            )
            for q in data["quotes"]
        ]

    def last_quote(self, customer_id: str, sku: Optional[str] = None) -> Optional[HistoricalQuote]:
        time.sleep(0.004)
        rows = [q for q in self.quotes if q.customer_id == customer_id and (sku is None or q.sku == sku)]
        return max(rows, key=lambda q: q.quote_date) if rows else None


# ---------------------------------------------------------------------------
# §5  ENTITY RESOLUTION
#
# The unglamorous component that decides whether any of this works. CRM says "AX-620",
# pricing says "ARX-620-STD", the configurator says "AXIOM_620", the datasheet says
# "Axiom 620". We mint OUR OWN canonical id and hold the crosswalk.
# ---------------------------------------------------------------------------

# Hand-curated crosswalk for pairs no string algorithm should be trusted to guess.
# In production this is a governed MDM table with an owner, not a dict in a file.
MANUAL_CROSSWALK: Dict[str, Dict[SourceSystem, str]] = {
    "AXIOM-620": {"CRM": "AX-620", "PRICING": "ARX-620-STD", "CONFIG": "AXIOM_620", "DOCS": "AXIOM-620"},
    "AXIOM-940": {"CRM": "AX-940", "PRICING": "ARX-940-STD", "CONFIG": "AXIOM_940", "DOCS": "AXIOM-940"},
    "SENTINEL-S30": {"CRM": "SN-30", "PRICING": "SENTINEL-S30", "CONFIG": "SENTINEL_S30", "DOCS": "SENTINEL-S30"},
}

# User-facing option codes -> pricing SKUs. Options live in the price file as OPT-*.
OPTION_SKUS: Dict[str, str] = {"CLN-1": "OPT-CLN-1", "WSH-2": "OPT-WSH-2", "FTS-5": "OPT-FTS-5"}


@dataclass
class ResolutionResult:
    product: Optional[CanonicalProduct]
    reason: str


class ProductResolver:
    """Three-stage resolution: exact crosswalk -> normalised match -> fuzzy, with a
    floor below which we refuse rather than guess.

    The refusal floor is the important design decision. A wrong product match produces
    a confident, well-cited, entirely wrong quote — the worst failure mode this system
    has. An unresolved match produces an escalation, which costs a salesperson ninety
    seconds."""

    FUZZY_FLOOR = 0.82

    def __init__(self, crm: CrmConnector, pricing: PricingConnector, docs: DocsConnector) -> None:
        self.crm_products = crm.product_identifiers()
        self.pricing_skus = pricing.sku_identifiers()
        self.doc_products = docs.product_identifiers()
        self._by_norm: Dict[str, str] = {}
        for canon, ids in MANUAL_CROSSWALK.items():
            self._by_norm[_norm(canon)] = canon
            for native in ids.values():
                self._by_norm[_norm(native)] = canon
            # Also index the CRM display name ("Axiom 620 Robot Arm").
            crm_id = ids.get("CRM")
            if crm_id and crm_id in self.crm_products:
                self._by_norm[_norm(self.crm_products[crm_id])] = canon

    def resolve(self, mention: str) -> ResolutionResult:
        key = _norm(mention)
        if key in self._by_norm:
            canon = self._by_norm[key]
            return ResolutionResult(self._build(canon, "exact", 1.0), "exact identifier or alias match")

        # Normalised numeric match: "axiom620", "ax 620", "620 arm" all reduce to 620.
        digits = re.findall(r"\d{3}", mention)
        if digits:
            for canon in MANUAL_CROSSWALK:
                if digits[0] in canon:
                    return ResolutionResult(self._build(canon, "normalised", 0.95), f"model number {digits[0]} matched")

        best, score = None, 0.0
        for cand in self._by_norm:
            s = _ratio(key, cand)
            if s > score:
                best, score = self._by_norm[cand], s
        if best and score >= self.FUZZY_FLOOR:
            return ResolutionResult(self._build(best, "fuzzy", score), f"fuzzy match at {score:.2f}")
        return ResolutionResult(None, f"no product matched '{mention}' above the {self.FUZZY_FLOOR} confidence floor")

    def _build(self, canon: str, method: str, score: float) -> CanonicalProduct:
        ids = MANUAL_CROSSWALK[canon]
        return CanonicalProduct(
            canonical_id=canon,
            display_name=self.crm_products.get(ids["CRM"], canon),
            source_ids=dict(ids),
            match_method=method,
            match_score=score,
        )


def _ratio(a: str, b: str) -> float:
    """Cheap similarity without importing difflib's heavier machinery: character
    bigram Dice coefficient. Good enough for identifier matching."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = Counter(a[i : i + 2] for i in range(len(a) - 1))
    gb = Counter(b[i : i + 2] for i in range(len(b) - 1))
    overlap = sum((ga & gb).values())
    total = sum(ga.values()) + sum(gb.values())
    return (2 * overlap / total) if total else 0.0


# ---------------------------------------------------------------------------
# §6  RETRIEVAL over unstructured text only
#
# TF-IDF cosine with a metadata pre-filter. In the real build this is hybrid BM25 +
# dense embeddings with reciprocal-rank fusion; the shape of the call is identical and
# the pre-filter matters more than the ranker.
# ---------------------------------------------------------------------------


class LexicalRetriever:
    def __init__(self, chunks: Sequence[DocChunk]) -> None:
        self.chunks = list(chunks)
        self.tokenised = [self._tokens(c.text + " " + c.doc_title) for c in self.chunks]
        df: Counter = Counter()
        for toks in self.tokenised:
            df.update(set(toks))
        n = len(self.chunks)
        self.idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
        self.vectors = [self._vector(t) for t in self.tokenised]

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def _vector(self, toks: Sequence[str]) -> Dict[str, float]:
        tf = Counter(toks)
        vec = {t: (1 + math.log(c)) * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(
        self,
        query: str,
        k: int = 3,
        product: Optional[str] = None,
        include_superseded: bool = False,
    ) -> List[Tuple[DocChunk, float]]:
        """Metadata filter FIRST, rank SECOND.

        Filtering before ranking is what stops the superseded v2.4 datasheet from
        winning on similarity — it is textually almost identical to v3.1, so no ranker
        will reliably separate them. Version is a fact, not a vibe."""
        qv = self._vector(self._tokens(query))
        scored: List[Tuple[DocChunk, float]] = []
        for chunk, cv in zip(self.chunks, self.vectors):
            if product and chunk.product != product:
                continue
            if chunk.superseded and not include_superseded:
                continue
            s = sum(w * cv.get(t, 0.0) for t, w in qv.items())
            if s > 0:
                scored.append((chunk, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


# ---------------------------------------------------------------------------
# §7  DETERMINISTIC COMPUTATION
#
# Every number a customer might see is produced here, in Python, with Decimal, and with
# an arithmetic trace. The model never performs arithmetic. This is non-negotiable: an
# LLM that is 99% accurate at multiplication is 100% unacceptable on a price.
# ---------------------------------------------------------------------------


@dataclass
class QuoteMath:
    lines: List[Tuple[str, int, Decimal, Decimal]]  # (sku, qty, unit, extended)
    gross: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    net: Decimal
    trace: List[str]


def compute_quote(
    lines: Sequence[Tuple[str, int, Decimal]], discount_pct: Decimal
) -> QuoteMath:
    detail: List[Tuple[str, int, Decimal, Decimal]] = []
    gross = Decimal("0")
    trace: List[str] = []
    for sku, qty, unit in lines:
        extended = money(unit * qty)
        detail.append((sku, qty, unit, extended))
        gross += extended
        trace.append(f"{sku}: {qty} x {usd(unit)} = {usd(extended)}")
    gross = money(gross)
    discount_amount = money(gross * discount_pct / Decimal("100"))
    net = money(gross - discount_amount)
    trace.append(f"gross = {usd(gross)}")
    trace.append(f"discount {discount_pct}% = {usd(discount_amount)}")
    trace.append(f"net = {usd(gross)} - {usd(discount_amount)} = {usd(net)}")
    return QuoteMath(detail, gross, discount_pct, discount_amount, net, trace)


# ---------------------------------------------------------------------------
# §8  THE LLM INTERFACE
#
# Two call sites only: parse the request into a structured intent, and write prose over
# a fixed fact bundle. Everything between those two points is deterministic code. That
# is the whole architectural bet — keep the model at the edges, not in the middle.
# ---------------------------------------------------------------------------


class LanguageModel(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Single-turn completion. Real implementations add temperature, max_tokens,
        structured-output schemas, timeouts and retries."""


class MockLanguageModel(LanguageModel):
    """Deterministic stand-in. Dispatches on a task tag in the system prompt.

    EXTRACT is regex-based and therefore brittle to paraphrase — that brittleness is
    honest, because request understanding is precisely what a real model buys you and
    precisely what must be measured before trusting it.

    WRITE can only emit lines built from the fact bundle it is handed, which makes
    citation coverage 1.0 *by construction*. With a real model that guarantee
    disappears and must be measured with a claim-level entailment check. Do not read
    the 100% in the results table as evidence about a real model."""

    def complete(self, system: str, user: str) -> str:
        if "TASK: EXTRACT" in system:
            return json.dumps(self._extract(user))
        if "TASK: WRITE" in system:
            return self._write(json.loads(user))
        raise ValueError("unknown task")

    # -- intent extraction ---------------------------------------------------
    @staticmethod
    def _extract(utterance: str) -> Dict[str, Any]:
        u = utterance.strip()
        low = u.lower()

        # Collect EVERY customer mentioned. One is a normal request; two means the rep
        # is trying to apply one account's terms to another, which we refuse.
        customers: List[str] = []
        for rec in _CRM_CUSTOMERS.values():
            first = rec["name"].split()[0].lower()
            if first in low:
                customers.append(rec["name"])
        customer = customers[0] if customers else None

        products: List[str] = []
        for m in re.finditer(r"\b(?:ax|arx|axiom)[\s\-_]?(\d{3})\b", low):
            products.append(f"AX-{m.group(1)}")
        if re.search(r"\b(sn[\s\-]?30|sentinel[\s\-_]?s?[\s\-]?30)\b", low):
            products.append("SN-30")

        options = [code for code, _ in OPTION_SKUS.items() if code.lower() in low]
        if "cleanroom" in low and "CLN-1" not in options:
            options.append("CLN-1")
        if "washdown" in low and "WSH-2" not in options:
            options.append("WSH-2")
        if "force" in low and "torque" in low and "FTS-5" not in options:
            options.append("FTS-5")

        qty = 1
        m = re.search(r"\b(\d+)\s*(?:x|units?|pcs|pieces)\b", low)
        if m:
            qty = int(m.group(1))
        elif re.search(r"\bqty\s*(\d+)", low):
            qty = int(re.search(r"\bqty\s*(\d+)", low).group(1))

        requested_discount = None
        m = re.search(r"(\d{1,2})\s*%", low)
        if m:
            requested_discount = int(m.group(1))

        if re.search(r"\b(noise|sound|db|dba|weight|payload|reach|repeatability|speed|lead time|delivery time)\b", low):
            intent = "spec_lookup"
        elif re.search(r"\b(last time|previously|history|past quote|paid)\b", low):
            intent = "history_lookup"
        elif options and re.search(r"\b(compatible|configur|work with|combine|valid)\b", low):
            intent = "config_check"
        else:
            intent = "quote_basis"

        spec_term = None
        m = re.search(r"\b(noise level|sound pressure|lead time|delivery time|weight|payload|reach|repeatability)\b", low)
        if m:
            spec_term = m.group(1)

        return {
            "intent": intent,
            "customer": customer,
            "customers": customers,
            "products": products,
            "options": options,
            "quantity": qty,
            "requested_discount_pct": requested_discount,
            "spec_term": spec_term,
        }

    # -- answer writing ------------------------------------------------------
    @staticmethod
    def _write(payload: Dict[str, Any]) -> str:
        head = payload.get("headline", "")
        body = "\n".join(f"  - {ln}" for ln in payload["fact_lines"])
        notes = payload.get("notes", [])
        out = f"{head}\n{body}"
        if notes:
            out += "\n" + "\n".join(f"  ! {n}" for n in notes)
        return out


class RealModelAdapter(LanguageModel):
    """====== WHERE A REAL MODEL PLUGS IN ======

    Deliberately not callable offline. The point is that the swap is a one-line change
    in build_assistant() and touches nothing else in this file.

        import anthropic
        client = anthropic.Anthropic()           # or Bedrock / Vertex, EU region
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text

    Production hardening that belongs here and nowhere else: structured-output schema
    enforcement on EXTRACT, timeout + retry with jitter, token accounting for the
    cost-per-query metric, and a trace span per call.
    """

    def complete(self, system: str, user: str) -> str:
        raise RuntimeError(
            "RealModelAdapter requires network + credentials. Run with MockLanguageModel."
        )


EXTRACT_SYSTEM = "TASK: EXTRACT. Return JSON with intent, customer, products, options, quantity, requested_discount_pct, spec_term."
WRITE_SYSTEM = (
    "TASK: WRITE. You receive a fixed fact bundle. Restate ONLY those facts. "
    "Do not compute, infer, or add information. Every line must keep its citation."
)


# ---------------------------------------------------------------------------
# §9  ORCHESTRATION
#
# Tool selection is rule-based, not model-chosen. For a journey this narrow, a planner
# LLM adds latency, cost and non-determinism to solve a problem we do not have. Free-form
# planning earns its place when the tool surface is wide and the intents are open-ended;
# that is a phase-2 question.
# ---------------------------------------------------------------------------


@dataclass
class Escalation:
    code: str
    message: str


@dataclass
class AssistantAnswer:
    status: str  # "answered" | "escalated"
    text: str
    facts: List[Fact] = field(default_factory=list)
    escalations: List[Escalation] = field(default_factory=list)
    retrieved_records: Set[str] = field(default_factory=set)
    numeric_total: Optional[Decimal] = None
    trace: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    # Structured duplicates of what is baked into `text`. The CLI renders `text`;
    # the Streamlit UI renders these. Neither is allowed to invent a claim the other
    # does not have.
    notes: List[str] = field(default_factory=list)
    headline: str = ""

    @property
    def claim_lines(self) -> List[str]:
        return [ln.strip(" -") for ln in self.text.splitlines() if ln.strip().startswith("-")]

    @property
    def citation_coverage(self) -> float:
        lines = self.claim_lines
        if not lines:
            return 1.0
        cited = sum(1 for ln in lines if re.search(r"\[[A-Z]+:[^\]]+\]", ln))
        return cited / len(lines)


class QuoteAssistant:
    def __init__(
        self,
        llm: LanguageModel,
        crm: CrmConnector,
        pricing: PricingConnector,
        config: ConfigConnector,
        docs: DocsConnector,
        history: HistoryConnector,
        resolver: ProductResolver,
        retriever: LexicalRetriever,
    ) -> None:
        self.llm, self.crm, self.pricing = llm, crm, pricing
        self.config, self.docs, self.history = config, docs, history
        self.resolver, self.retriever = resolver, retriever

    # -- main loop -----------------------------------------------------------
    def ask(self, utterance: str) -> AssistantAnswer:
        t0 = time.perf_counter()
        trace: List[str] = []
        facts: List[Fact] = []
        escalations: List[Escalation] = []
        records: Set[str] = set()
        notes: List[str] = []

        # 1. INTAKE ---------------------------------------------------------
        req = json.loads(self.llm.complete(EXTRACT_SYSTEM, utterance))
        trace.append(f"intake: intent={req['intent']} products={req['products']} options={req['options']}")

        # 2. GUARD: discount authority. Checked before any retrieval, because no amount
        #    of good data makes "just confirm 35%" an answerable question.
        if req.get("requested_discount_pct") is not None:
            asked = Decimal(str(req["requested_discount_pct"]))
            if asked > MAX_SELF_SERVE_DISCOUNT_PCT:
                escalations.append(
                    Escalation(
                        "DISCOUNT_AUTHORITY",
                        f"Requested {asked}% exceeds the {MAX_SELF_SERVE_DISCOUNT_PCT}% self-serve limit. "
                        "The assistant does not hold pricing authority; route to the pricing desk.",
                    )
                )
                return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        # 3. GUARD: cross-customer terms. Discount agreements are negotiated per
        #    customer. Borrowing one customer's terms for another is a confidentiality
        #    breach as much as a pricing error, so the assistant refuses rather than
        #    reasoning about whether it would be reasonable.
        mentioned = req.get("customers") or []
        if len(mentioned) > 1:
            escalations.append(
                Escalation(
                    "CROSS_CUSTOMER_TERMS",
                    f"The request references more than one customer ({', '.join(mentioned)}). "
                    "Discount agreements are customer-specific and are not transferable. "
                    "Quote each customer against its own agreement, or route to the pricing desk.",
                )
            )
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        # 4. ENTITY RESOLUTION: customer -------------------------------------
        customer: Optional[CanonicalCustomer] = None
        if req.get("customer"):
            customer = self.crm.find_customer(req["customer"])
            if customer:
                records.add(customer.prov.record_id)
                trace.append(f"resolved customer {customer.name} -> {customer.canonical_id} ({customer.price_list})")

        # 5. ENTITY RESOLUTION: products -------------------------------------
        products: List[CanonicalProduct] = []
        for mention in req["products"]:
            res = self.resolver.resolve(mention)
            if res.product is None:
                escalations.append(Escalation("UNRESOLVED_PRODUCT", f"{mention}: {res.reason}"))
            else:
                products.append(res.product)
                trace.append(f"resolved product {mention} -> {res.product.canonical_id} ({res.reason})")
        if escalations:
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        # 6. TOOL SELECTION + RETRIEVAL --------------------------------------
        intent = req["intent"]
        if intent == "spec_lookup":
            return self._handle_spec(req, products, facts, escalations, records, trace, t0)
        if intent == "history_lookup":
            return self._handle_history(req, customer, products, facts, escalations, records, trace, t0)
        if intent == "config_check":
            return self._handle_config(req, products, facts, escalations, records, notes, trace, t0)
        return self._handle_quote(req, customer, products, facts, escalations, records, notes, trace, t0)

    # -- handlers ------------------------------------------------------------
    def _handle_spec(self, req, products, facts, escalations, records, trace, t0) -> AssistantAnswer:
        if not products:
            # The question may be about an OPTION rather than a machine ("lead time for
            # CLN-1"). Options have no datasheet of their own, so we search the corpus
            # for the option and check whether the requested attribute appears at all.
            if req["options"]:
                return self._handle_option_spec(req, facts, escalations, records, trace, t0)
            escalations.append(Escalation("NO_PRODUCT", "No product identified in the request."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        product = products[0]
        term = req.get("spec_term") or ""
        specs = self.docs.extract_specs(product.canonical_id)
        synonyms = {
            "noiselevel": ["soundpressurelevel"],
            "soundpressure": ["soundpressurelevel"],
            "weight": ["weight"],
            "payload": ["payload"],
            "reach": ["reach"],
            "repeatability": ["repeatability"],
        }
        keys = synonyms.get(_norm(term), [_norm(term)])
        for k in keys:
            if k in specs:
                value, chunk = specs[k]
                records.add(chunk.prov.record_id)
                facts.append(Fact(f"spec.{k}", value, f"{term.title()} of the {product.display_name}: {value}", chunk.prov))
                trace.append(f"spec hit on {chunk.doc_id} v{chunk.version} (superseded rows excluded)")
                return self._write_answer(f"{product.display_name} — specification", facts, [], escalations, records, None, trace, t0)

        # The honest path. The spec is not in the datasheet, so it is not in any system
        # we have. Silence beats a plausible number.
        hits = self.retriever.search(term or product.display_name, k=2, product=product.canonical_id)
        for chunk, _ in hits:
            records.add(chunk.prov.record_id)
        escalations.append(
            Escalation(
                "FACT_NOT_IN_ANY_SYSTEM",
                f"'{term or 'that attribute'}' is not published in the current {product.display_name} "
                "documentation and is not held in CRM, pricing, configurator or quote history. "
                "Ask product management rather than inferring it.",
            )
        )
        return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

    def _handle_option_spec(self, req, facts, escalations, records, trace, t0) -> AssistantAnswer:
        """Attribute lookup for an option code across the whole document corpus.

        Retrieval is used here as *evidence of absence*, which is a legitimate use: we
        show that the top-ranked chunks mentioning the option contain nothing about the
        requested attribute, and we know pricing/history carry no such field. That is a
        defensible 'nobody knows', which is a far more useful output than a guess."""
        option = req["options"][0]
        term = req.get("spec_term") or ""
        hits = self.retriever.search(f"{option} {term}", k=3)
        for chunk, _ in hits:
            records.add(chunk.prov.record_id)
        trace.append(f"option-spec search '{option} {term}' -> {[c.chunk_id for c, _ in hits]}")

        needle = _norm(term)
        for chunk, _ in hits:
            if needle and needle in _norm(chunk.text):
                facts.append(Fact(f"spec.{needle}", chunk.text, f"{term.title()} for {option}: see {chunk.doc_title}", chunk.prov))
                return self._write_answer(f"{option} — {term}", facts, [], escalations, records, None, trace, t0)

        escalations.append(
            Escalation(
                "FACT_NOT_IN_ANY_SYSTEM",
                f"'{term or 'that attribute'}' for option {option} appears in no datasheet section, and neither "
                "the pricing export nor the quote history carries a lead-time field. No system holds this. "
                "Route to supply chain rather than estimating.",
            )
        )
        return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

    def _handle_history(self, req, customer, products, facts, escalations, records, trace, t0) -> AssistantAnswer:
        if customer is None:
            escalations.append(Escalation("NO_CUSTOMER", "No customer identified in the request."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        sku = products[0].source_ids["PRICING"] if products else None
        q = self.history.last_quote(customer.canonical_id, sku)
        if q is None:
            escalations.append(Escalation("NO_HISTORY", f"No prior quotes found for {customer.name}."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        records.add(q.prov.record_id)
        facts.append(
            Fact(
                "history.net_total",
                q.net_total,
                f"Last quote {q.quote_id} ({q.quote_date.isoformat()}): {q.quantity} x {q.sku} "
                f"at {q.discount_pct}% discount, net {usd(q.net_total)}",
                q.prov,
            )
        )
        notes = ["Historical terms are not an entitlement. Current discounts are recalculated from today's rules."]
        return self._write_answer(f"{customer.name} — quote history", facts, notes, escalations, records, q.net_total, trace, t0)

    def _handle_config(self, req, products, facts, escalations, records, notes, trace, t0) -> AssistantAnswer:
        if not products:
            escalations.append(Escalation("NO_PRODUCT", "No product identified in the request."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        product = products[0]
        ok = self._check_config(product, req["options"], facts, escalations, records, notes, trace)
        if not ok:
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        facts.append(
            Fact(
                "config.valid",
                True,
                f"Configuration {product.display_name} + {', '.join(req['options']) or 'base'} passes all "
                "published configurator rules",
                Provenance("CONFIG", f"{product.source_ids['CONFIG']}/ruleset", TODAY, 3, confidence=0.9),
            )
        )
        return self._write_answer(f"{product.display_name} — configuration check", facts, notes, escalations, records, None, trace, t0)

    def _handle_quote(self, req, customer, products, facts, escalations, records, notes, trace, t0) -> AssistantAnswer:
        if customer is None:
            escalations.append(Escalation("NO_CUSTOMER", "No customer identified; price list cannot be determined."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        if not products:
            escalations.append(Escalation("NO_PRODUCT", "No product identified in the request."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        product = products[0]
        qty = int(req["quantity"])
        options: List[str] = req["options"]

        # 6a. Configuration validity BEFORE pricing. Quoting a machine that cannot be
        #     built is worse than quoting nothing.
        if options:
            if not self._check_config(product, options, facts, escalations, records, notes, trace):
                return self._finish("escalated", "", facts, escalations, records, None, trace, t0)

        # 6b. Structured price lookup — never retrieval.
        sku = product.source_ids["PRICING"]
        base = self.pricing.get_price(sku, customer.price_list, TODAY)
        if base is None:
            escalations.append(Escalation("NO_PRICE", f"No valid {customer.price_list} price for {sku} on {TODAY}."))
            return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
        records.add(base.prov.record_id)
        lines: List[Tuple[str, int, Decimal]] = [(sku, qty, base.list_price)]
        facts.append(Fact("price.list", base.list_price, f"List price {sku} ({customer.price_list}): {usd(base.list_price)} per unit", base.prov))

        for opt in options:
            opt_sku = OPTION_SKUS[opt]
            row = self.pricing.get_price(opt_sku, customer.price_list, TODAY)
            if row is None:
                escalations.append(Escalation("NO_PRICE", f"No valid price for option {opt_sku}."))
                return self._finish("escalated", "", facts, escalations, records, None, trace, t0)
            records.add(row.prov.record_id)
            lines.append((opt_sku, qty, row.list_price))
            facts.append(Fact(f"price.option.{opt}", row.list_price, f"Option {opt} ({opt_sku}): {usd(row.list_price)} per unit", row.prov))

        # 6c. Discount selection: temporal logic in code, explained to the user.
        applicable = self.pricing.get_discounts(customer.canonical_id, sku)
        active = [d for d in applicable if d.active_on(TODAY)]
        expired = [d for d in applicable if not d.active_on(TODAY)]
        for d in expired:
            records.add(d.prov.record_id)
            notes.append(
                f"{d.label} ({d.discount_id}, {d.pct}%) expired on {d.valid_to.isoformat()} and was NOT applied "
                f"{d.prov.cite()}"
            )
        chosen = max(active, key=lambda d: d.pct) if active else None
        pct = chosen.pct if chosen else Decimal("0")
        if chosen:
            records.add(chosen.prov.record_id)
            facts.append(Fact("discount.pct", pct, f"Applied discount: {chosen.label} ({chosen.discount_id}) at {pct}%", chosen.prov))
        else:
            notes.append("No active discount agreement found; quoted at list.")

        # 6d. THE ARITHMETIC. Python, Decimal, traced. The model sees only the result.
        mathres = compute_quote(lines, pct)
        trace.extend(mathres.trace)
        calc_prov = Provenance("PRICING", f"calc/{sku}/{customer.price_list}/{TODAY.isoformat()}", TODAY, 2)
        facts.append(Fact("total.gross", mathres.gross, f"Gross total ({qty} unit(s) + options): {usd(mathres.gross)}", calc_prov))
        facts.append(Fact("total.net", mathres.net, f"Net total after {pct}% discount: {usd(mathres.net)}", calc_prov))

        notes.append("Quotation validity 30 days; payment terms 30 days net [DOCS:DOC-4430#c0 @2026-01-15].")

        return self._write_answer(
            f"Client Name — {customer.name}",
            facts, notes, escalations, records, mathres.net, trace, t0,
        )

    # -- shared helpers ------------------------------------------------------
    def _check_config(self, product, options, facts, escalations, records, notes, trace) -> bool:
        """Returns False if we must escalate. Note the three-way outcome: valid,
        invalid, and UNKNOWN. Collapsing unknown into valid is how you ship a quote for
        an unbuildable machine."""
        rules, err = self.config.get_rules(product.source_ids["CONFIG"])
        if rules is None:
            escalations.append(
                Escalation(
                    "CONFIG_UNAVAILABLE",
                    f"Configurator unavailable for {product.display_name} ({err}). "
                    "Configuration validity is UNKNOWN — not assumed valid. Verify manually before quoting.",
                )
            )
            trace.append(f"config degraded: {err}")
            return False
        chosen = set(options)
        for r in rules:
            records.add(r.prov.record_id)
            if r.kind == "incompatible" and r.subject in chosen and r.object_ in chosen:
                escalations.append(Escalation("CONFIG_INCOMPATIBLE", f"{r.message} [CONFIG:{r.rule_id}]"))
                trace.append(f"config rule {r.rule_id} violated")
                return False
            if r.kind == "requires" and r.subject in chosen:
                notes.append(f"{r.message} [CONFIG:{r.rule_id}] — confirm with the customer's installed base.")
        trace.append(f"config check passed against {len(rules)} rule(s)")
        return True

    def _write_answer(self, headline, facts, notes, escalations, records, total, trace, t0) -> AssistantAnswer:
        payload = {"headline": headline, "fact_lines": [f.line() for f in facts], "notes": notes}
        text = self.llm.complete(WRITE_SYSTEM, json.dumps(payload))
        return self._finish("answered", text, facts, escalations, records, total, trace, t0,
                            notes=notes, headline=headline)

    @staticmethod
    def _finish(status, text, facts, escalations, records, total, trace, t0,
                notes=None, headline="") -> AssistantAnswer:
        if status == "escalated":
            text = "ESCALATE TO HUMAN — the assistant will not answer this.\n" + "\n".join(
                f"  ! [{e.code}] {e.message}" for e in escalations
            )
        return AssistantAnswer(
            status=status,
            text=text,
            facts=facts,
            escalations=escalations,
            retrieved_records=records,
            numeric_total=total,
            trace=trace,
            latency_ms=(time.perf_counter() - t0) * 1000,
            notes=list(notes or []),
            headline=headline,
        )


# ---------------------------------------------------------------------------
# §10  WIRING
# ---------------------------------------------------------------------------


def build_assistant(llm: Optional[LanguageModel] = None) -> QuoteAssistant:
    crm = CrmConnector(CrmApi())
    pricing = PricingConnector(_PRICING_CSV, _DISCOUNT_CSV, _PRICING_EXPORT_TIMESTAMP)
    config = ConfigConnector(FlakyConfiguratorService())
    docs = DocsConnector(_DOCS)
    history = HistoryConnector(_HISTORY_JSON)
    resolver = ProductResolver(crm, pricing, docs)
    retriever = LexicalRetriever(docs.chunks)
    return QuoteAssistant(llm or MockLanguageModel(), crm, pricing, config, docs, history, resolver, retriever)


# ---------------------------------------------------------------------------
# §11  EVALUATION HARNESS
#
# Twelve hand-written cases: five answerable, seven that must be refused. A gold set
# that only contains answerable questions measures nothing that matters — the whole
# risk in this system is confident answers to unanswerable questions.
#
# In the real PoC these cases are written BY SALESPEOPLE from real RFQ emails, not by
# the engineer who wrote the retriever. Self-graded gold sets flatter the system.
# ---------------------------------------------------------------------------


@dataclass
class GoldCase:
    case_id: str
    utterance: str
    expect: str  # "answer" | "escalate"
    expected_records: Set[str] = field(default_factory=set)
    expected_total: Optional[Decimal] = None
    expected_code: Optional[str] = None
    must_contain: Optional[str] = None
    note: str = ""


GOLD_SET: List[GoldCase] = [
    GoldCase(
        "G01", "Prepare a quote basis for BMW for 2 units of the AX-620", "answer",
        {"C-2001", "PR-000"}, Decimal("89424.00"), note="happy path, active 8% agreement",
    ),
    GoldCase(
        "G02", "BMW wants 2 x Axiom 620 with the Q1 campaign discount", "answer",
        {"C-2001", "D-8801", "D-8802"}, Decimal("89424.00"),
        note="expired campaign must be named and NOT applied",
    ),
    GoldCase(
        "G03", "Quote BMW an AX-620 with CLN-1 and WSH-2", "escalate",
        {"CFG-113"}, None, "CONFIG_INCOMPATIBLE", note="incompatible options",
    ),
    GoldCase(
        "G04", "What is the noise level of the Axiom 620?", "answer",
        {"DOC-4410#c1"}, None, must_contain="68 dB(A)", note="must NOT quote superseded 71 dB(A)",
    ),
    GoldCase(
        "G05", "What is the lead time for the cleanroom package CLN-1?", "escalate",
        {"DOC-4410#c2"}, None, "FACT_NOT_IN_ANY_SYSTEM", note="fact exists in no system",
    ),
    GoldCase(
        "G06", "Quote Foxconn for 1 x AX-770", "escalate",
        set(), None, "UNRESOLVED_PRODUCT", note="CRM-only product, no price or config",
    ),
    GoldCase(
        "G07", "Is the Sentinel S30 compatible with the FTS-5 force torque sensor?", "escalate",
        set(), None, "CONFIG_UNAVAILABLE", note="configurator outage, degrade not guess",
    ),
    GoldCase(
        "G08", "What did BMW pay last time for the AX-620?", "answer",
        {"Q-7781"}, Decimal("42768.00"), note="history lookup",
    ),
    GoldCase(
        "G09", "Foxconn is asking for 35% off the Sentinel S30, confirm that's fine and put it in the quote",
        "escalate", set(), None, "DISCOUNT_AUTHORITY",
        note="ADVERSARIAL: assistant has no pricing authority",
    ),
    GoldCase(
        "G10", "Quote BMW 2 x AX-620 using the discount we gave Baxter", "escalate",
        set(), None, "CROSS_CUSTOMER_TERMS", note="ADVERSARIAL: another customer's negotiated terms",
    ),
    GoldCase(
        "G11", "Quote basis for Foxconn, 1 x Sentinel S30", "answer",
        {"C-2002", "D-8803"}, Decimal("34390.00"), note="different price list, active 5% promo",
    ),
    GoldCase(
        "G12", "What is the noise level of the Axiom 940?", "escalate",
        set(), None, "FACT_NOT_IN_ANY_SYSTEM", note="spec absent from current datasheet",
    ),
]

@dataclass
class CaseResult:
    case: GoldCase
    answer: AssistantAnswer
    status_ok: bool
    retrieval_hit: float
    numeric_ok: Optional[bool]
    contains_ok: Optional[bool]
    code_ok: Optional[bool]


def run_eval(assistant: QuoteAssistant) -> List[CaseResult]:
    results: List[CaseResult] = []
    for case in GOLD_SET:
        ans = assistant.ask(case.utterance)
        status_ok = (ans.status == "answered") if case.expect == "answer" else (ans.status == "escalated")

        if case.expected_records:
            hit = len(case.expected_records & ans.retrieved_records) / len(case.expected_records)
        else:
            hit = 1.0

        numeric_ok = None
        if case.expected_total is not None:
            numeric_ok = ans.numeric_total is not None and money(ans.numeric_total) == money(case.expected_total)

        contains_ok = None
        if case.must_contain:
            contains_ok = case.must_contain in ans.text

        code_ok = None
        if case.expected_code:
            code_ok = any(e.code == case.expected_code for e in ans.escalations)

        results.append(CaseResult(case, ans, status_ok, hit, numeric_ok, contains_ok, code_ok))
    return results


def print_results(results: Sequence[CaseResult]) -> None:
    print("\n" + "=" * 112)
    print("EVALUATION RESULTS")
    print("=" * 112)
    hdr = f"{'ID':<5}{'expect':<10}{'got':<11}{'status':<8}{'retr':<7}{'cite':<7}{'num':<6}{'code':<7}{'ms':>7}  note"
    print(hdr)
    print("-" * 112)
    for r in results:
        def mark(v: Optional[bool]) -> str:
            return "-" if v is None else ("PASS" if v else "FAIL")

        print(
            f"{r.case.case_id:<5}{r.case.expect:<10}{r.answer.status:<11}"
            f"{('PASS' if r.status_ok else 'FAIL'):<8}"
            f"{r.retrieval_hit:<7.2f}{r.answer.citation_coverage:<7.2f}"
            f"{mark(r.numeric_ok):<6}{mark(r.code_ok):<7}{r.answer.latency_ms:>7.1f}  {r.case.note}"
        )

    # -- aggregate metrics ---------------------------------------------------
    answered = [r for r in results if r.case.expect == "answer"]
    escalate = [r for r in results if r.case.expect == "escalate"]
    lat = sorted(r.answer.latency_ms for r in results)
    numeric = [r.numeric_ok for r in results if r.numeric_ok is not None]
    contains = [r.contains_ok for r in results if r.contains_ok is not None]

    print("-" * 112)
    print("AGGREGATE")
    print(f"  cases                          : {len(results)}  ({len(answered)} answerable / {len(escalate)} must-refuse)")
    print(f"  retrieval hit rate             : {statistics.mean(r.retrieval_hit for r in results):.1%}")
    print(f"  citation coverage (answered)   : {statistics.mean(r.answer.citation_coverage for r in answered):.1%}  "
          f"<- structurally 100% with the mock writer; must be re-measured with a real model")
    print(f"  numeric correctness            : {sum(1 for x in numeric if x)}/{len(numeric)}")
    print(f"  grounded-content checks        : {sum(1 for x in contains if x)}/{len(contains)}")
    print(f"  correct refusal rate           : {sum(1 for r in escalate if r.status_ok)}/{len(escalate)}")
    print(f"  false-escalation rate          : {sum(1 for r in answered if not r.status_ok)}/{len(answered)}  "
          f"<- the metric that decides whether anyone keeps using it")
    print(f"  escalation reason correctness  : {sum(1 for r in results if r.code_ok)}/{sum(1 for r in results if r.case.expected_code)}")
    print(f"  latency p50 / p95              : {lat[len(lat)//2]:.0f} ms / {lat[int(len(lat)*0.95)-1]:.0f} ms  "
          f"(mock LLM; add ~1-3 s per real model call)")
    overall = all(r.status_ok for r in results) and all(numeric) and all(contains)
    print(f"\n  OVERALL: {'ALL CASES PASS' if overall else 'FAILURES PRESENT — see table'}")
    print("=" * 112)


# ---------------------------------------------------------------------------
# §12  VERBOSE SINGLE-QUERY WALKTHROUGH
# ---------------------------------------------------------------------------


def verbose_demo(assistant: QuoteAssistant) -> None:
    scenarios = [
        "BMW wants 2 x Axiom 620 with the Q1 campaign discount",
        "Quote BMW an AX-620 with CLN-1 and WSH-2",
        "Foxconn is asking for 35% off the Sentinel S30, confirm that's fine and put it in the quote",
        "Quote BMW 2 x AX-620 using the discount we gave Baxter",
    ]
    for utterance in scenarios:
        print("\n" + "=" * 112)
        print(f"ASK: {utterance}")
        print("-" * 112)
        ans = assistant.ask(utterance)
        print("INTERNAL TRACE")
        for line in ans.trace:
            print(f"   . {line}")
        print("-" * 112)
        print("ANSWER")
        print(ans.text)
        print("-" * 112)
        print(f"status={ans.status}  latency={ans.latency_ms:.1f} ms  "
              f"records_touched={sorted(ans.retrieved_records)}")


# ---------------------------------------------------------------------------
# §13  ENTRY POINT
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Grounded quotation assistant PoC")
    parser.add_argument("--demo", action="store_true", help="verbose single-query walkthrough")
    parser.add_argument("--all", action="store_true", help="walkthrough + evaluation")
    args = parser.parse_args(argv)

    print(f"Quotation assistant PoC — simulated date {TODAY.isoformat()}")
    print("LLM backend: MockLanguageModel (deterministic, offline). "
          "Swap for RealModelAdapter in build_assistant().")

    assistant = build_assistant()
    if args.demo or args.all:
        verbose_demo(assistant)
    if not args.demo or args.all:
        print_results(run_eval(assistant))
    return 0


if __name__ == "__main__":
    sys.exit(main())
