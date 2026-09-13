"""Build the deterministic, statistically rich TachyonIQ demonstration database.

The data is synthetic. It intentionally contains documented business signals so
trend, cohort, profitability, service, anomaly, and correlation demonstrations
have reproducible evidence. ``generate`` accepts an alternate output path for
isolated verification and writes a matching ground-truth JSON manifest.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "demo.sqlite"
GROUND_TRUTH_PATH = ROOT / "data" / "demo_ground_truth.json"
SEED = 42
AS_OF_DATE = dt.date(2026, 9, 1)
START_MONTH = dt.date(2023, 9, 1)
WM_END_MONTH = dt.date(2026, 8, 1)

WM_REGIONS = {
    "Northeast": ["MA", "NY", "PA", "NJ"],
    "Southeast": ["FL", "GA", "NC", "TN"],
    "Midwest": ["IL", "IN", "MI", "OH"],
    "West": ["AZ", "CA", "CO", "WA"],
}
WM_SERVICE_LINES = [
    "Residential Collection",
    "Commercial Collection",
    "Roll-Off",
    "Recycling",
    "Transfer & Disposal",
]

REGIONS = {
    "North America": ["United States", "Canada"],
    "Europe": ["United Kingdom", "Germany", "France", "Netherlands"],
    "APAC": ["India", "Singapore", "Australia", "Japan"],
    "LATAM": ["Brazil", "Mexico", "Chile", "Colombia"],
}
REGION_WEIGHTS = [0.42, 0.28, 0.21, 0.09]
TIERS = ["Free", "Pro", "Enterprise"]
TIER_WEIGHTS = [0.46, 0.37, 0.17]
INDUSTRIES = [
    "Financial Services",
    "Retail",
    "Healthcare",
    "Manufacturing",
    "Technology",
    "Media",
    "Education",
    "Professional Services",
]
ACQUISITION_CHANNELS = ["Organic", "Paid Search", "Partner", "Event", "Outbound", "Referral"]
SALES_CHANNELS = ["Self-Service", "Inside Sales", "Partner", "Field Sales"]
SALES_REPS = ["Asha", "Daniel", "Elena", "Ibrahim", "Jia", "Lucas", "Maya", "Noah"]
CAMPAIGNS = [
    "Always On",
    "Cloud Modernization",
    "Data Leaders",
    "Secure Growth",
    "Automation Week",
    "Executive Analytics",
    "Partner Momentum",
    "Year End Scale",
]

PRODUCT_CATALOG = {
    "Analytics": {
        "Dashboards": ["Insight Studio", "Executive Pulse", "Metric Canvas"],
        "Forecasting": ["Trend AI", "Demand Lens", "Scenario Lab"],
        "Data Quality": ["Trust Monitor", "Lineage Guard", "Quality Radar"],
    },
    "Automation": {
        "Workflow": ["Flow Builder", "Process Pilot", "AutoSync"],
        "AI Assistants": ["Task Copilot", "Ops Agent", "Service Agent"],
        "Integration": ["Connector Hub", "Event Bridge", "API Fabric"],
    },
    "Security": {
        "Identity": ["Access Vault", "Identity Graph", "Privilege Watch"],
        "Threat": ["Threat Scan", "Risk Sentinel", "Secure Edge"],
        "Compliance": ["Policy Center", "Audit Ready", "Control Mapper"],
    },
    "Data Platform": {
        "Storage": ["Cloud Vault", "Archive Grid", "Lakehouse Store"],
        "Compute": ["Query Engine", "Compute Flex", "Stream Core"],
        "Governance": ["Catalog Pro", "Data Contracts", "Privacy Center"],
    },
    "Collaboration": {
        "Workspace": ["Team Space", "Decision Hub", "Knowledge Room"],
        "Communication": ["Meet Sync", "Async Brief", "Signal Chat"],
        "Planning": ["Roadmap Live", "Sprint Board", "Goal Tracker"],
    },
    "Customer Experience": {
        "Support": ["Service Desk", "Resolution AI", "Success Console"],
        "Feedback": ["Voice Loop", "NPS Studio", "Research Hub"],
        "Engagement": ["Journey Builder", "Lifecycle AI", "Message Flow"],
    },
}

COMPANY_PREFIXES = [
    "Apex",
    "Atlas",
    "Bright",
    "Cedar",
    "Clear",
    "Delta",
    "Ember",
    "Falcon",
    "Harbor",
    "Lumen",
    "Maple",
    "Northstar",
    "Onyx",
    "Pioneer",
    "Quartz",
    "River",
    "Summit",
    "Vertex",
    "Vivid",
    "Willow",
]
COMPANY_SUFFIXES = [
    "Advisory",
    "Analytics",
    "Dynamics",
    "Group",
    "Holdings",
    "Industries",
    "Labs",
    "Networks",
    "Partners",
    "Solutions",
    "Systems",
    "Technologies",
]


def _months(start: dt.date, end: dt.date) -> list[dt.date]:
    values: list[dt.date] = []
    current = start
    while current <= end:
        values.append(current)
        current = dt.date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return values


def _month_end(value: dt.date) -> dt.date:
    following = dt.date(value.year + (value.month == 12), value.month % 12 + 1, 1)
    return following - dt.timedelta(days=1)


def _date_in_month(rng: random.Random, month: dt.date) -> dt.date:
    end = min(_month_end(month), AS_OF_DATE)
    return month + dt.timedelta(days=rng.randint(0, max(0, (end - month).days)))


def _weighted_customer(rng: random.Random, active_ids: list[int], tiers: dict[int, str]) -> int:
    weights = {"Free": 1.0, "Pro": 3.2, "Enterprise": 7.5}
    return rng.choices(active_ids, weights=[weights[tiers[cid]] for cid in active_ids], k=1)[0]


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, account_name TEXT NOT NULL, tier TEXT NOT NULL,
            segment TEXT NOT NULL, region TEXT NOT NULL, country TEXT NOT NULL,
            industry TEXT NOT NULL, company_size TEXT NOT NULL,
            employee_count INTEGER NOT NULL, acquisition_channel TEXT NOT NULL,
            signup_date TEXT NOT NULL, account_status TEXT NOT NULL, churn_date TEXT,
            annual_contract_value REAL NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY, product_name TEXT NOT NULL, category TEXT NOT NULL,
            subcategory TEXT NOT NULL, pricing_model TEXT NOT NULL,
            unit_price REAL NOT NULL, unit_cost REAL NOT NULL, launch_date TEXT NOT NULL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            product_id INTEGER NOT NULL REFERENCES products(id), order_date TEXT NOT NULL,
            order_month TEXT NOT NULL, quantity INTEGER NOT NULL,
            gross_revenue REAL NOT NULL, discount_amount REAL NOT NULL,
            net_revenue REAL NOT NULL, cost_amount REAL NOT NULL, profit REAL NOT NULL,
            status TEXT NOT NULL, sales_channel TEXT NOT NULL, sales_rep TEXT NOT NULL,
            campaign_name TEXT NOT NULL, promised_delivery_days INTEGER NOT NULL,
            actual_delivery_days INTEGER, on_time_delivery INTEGER
        );
        CREATE TABLE support_tickets (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            order_id INTEGER REFERENCES orders(id), created_date TEXT NOT NULL,
            created_month TEXT NOT NULL, category TEXT NOT NULL, priority TEXT NOT NULL,
            status TEXT NOT NULL, first_response_hours REAL NOT NULL,
            resolution_hours REAL, csat_score REAL, escalated INTEGER NOT NULL,
            reopened INTEGER NOT NULL
        );
        CREATE TABLE customer_monthly_metrics (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            month TEXT NOT NULL, subscription_status TEXT NOT NULL, plan_tier TEXT NOT NULL,
            seats INTEGER NOT NULL, active_users INTEGER NOT NULL,
            monthly_sessions INTEGER NOT NULL, feature_adoption_pct REAL NOT NULL,
            mrr REAL NOT NULL, expansion_mrr REAL NOT NULL, contraction_mrr REAL NOT NULL,
            nps_score REAL, churn_risk_score REAL NOT NULL, support_tickets INTEGER NOT NULL,
            UNIQUE(customer_id, month)
        );
        CREATE TABLE marketing_performance (
            id INTEGER PRIMARY KEY, month TEXT NOT NULL, channel TEXT NOT NULL,
            region TEXT NOT NULL, campaign_name TEXT NOT NULL, spend REAL NOT NULL,
            impressions INTEGER NOT NULL, clicks INTEGER NOT NULL, leads INTEGER NOT NULL,
            conversions INTEGER NOT NULL, attributed_revenue REAL NOT NULL,
            UNIQUE(month, channel, region, campaign_name)
        );
        CREATE TABLE business_kpi_monthly (
            id INTEGER PRIMARY KEY, month TEXT NOT NULL, region TEXT NOT NULL,
            net_revenue REAL NOT NULL, revenue_target REAL NOT NULL, profit REAL NOT NULL,
            profit_target REAL NOT NULL, order_count INTEGER NOT NULL,
            active_customers INTEGER NOT NULL, new_accounts INTEGER NOT NULL,
            churned_accounts INTEGER NOT NULL, marketing_spend REAL NOT NULL,
            support_tickets INTEGER NOT NULL, avg_csat REAL, UNIQUE(month, region)
        );
        CREATE TABLE wm_customers (
            id INTEGER PRIMARY KEY, customer_name TEXT NOT NULL,
            customer_segment TEXT NOT NULL, region TEXT NOT NULL, state TEXT NOT NULL,
            industry TEXT NOT NULL, credit_terms_days INTEGER NOT NULL,
            customer_since TEXT NOT NULL, account_status TEXT NOT NULL
        );
        CREATE TABLE wm_contracts (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES wm_customers(id),
            contract_name TEXT NOT NULL, service_line TEXT NOT NULL,
            pricing_model TEXT NOT NULL, contract_start_date TEXT NOT NULL,
            contract_end_date TEXT, contract_status TEXT NOT NULL,
            base_monthly_fee REAL NOT NULL, price_per_ton REAL NOT NULL
        );
        CREATE TABLE wm_finance_monthly (
            id INTEGER PRIMARY KEY,
            contract_id INTEGER NOT NULL REFERENCES wm_contracts(id),
            month TEXT NOT NULL, service_events INTEGER NOT NULL,
            collection_volume_tons REAL NOT NULL, disposal_volume_tons REAL NOT NULL,
            recycling_volume_tons REAL NOT NULL, revenue REAL NOT NULL,
            revenue_budget REAL NOT NULL, fuel_cost REAL NOT NULL,
            labor_cost REAL NOT NULL, disposal_cost REAL NOT NULL,
            fleet_cost REAL NOT NULL, other_operating_cost REAL NOT NULL,
            operating_cost REAL NOT NULL, operating_profit REAL NOT NULL,
            invoices_billed REAL NOT NULL, cash_collected REAL NOT NULL,
            ar_balance REAL NOT NULL, overdue_ar_balance REAL NOT NULL,
            days_sales_outstanding REAL NOT NULL,
            UNIQUE(contract_id, month)
        );
        """
    )


def _generate_waste_finance(
    rng: random.Random,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Create a deterministic synthetic waste-management finance star schema.

    Every financial identity is calculated before insertion.  The data contains
    documented operating signals for demonstrations, but no response value is
    embedded in application code; answers must still be computed from SQL.
    """
    industries = [
        "Municipality",
        "Property Management",
        "Manufacturing",
        "Retail",
        "Healthcare",
        "Hospitality",
        "Construction",
    ]
    segment_weights = [0.20, 0.55, 0.25]
    segments = ["Municipal", "Commercial", "Industrial"]
    region_names = list(WM_REGIONS)
    region_weights = [0.25, 0.29, 0.27, 0.19]
    customers: list[tuple[Any, ...]] = []
    for customer_id in range(1, 421):
        segment = rng.choices(segments, weights=segment_weights, k=1)[0]
        region = rng.choices(region_names, weights=region_weights, k=1)[0]
        state = rng.choice(WM_REGIONS[region])
        joined = START_MONTH - dt.timedelta(days=rng.randint(90, 1800))
        customers.append(
            (
                customer_id,
                f"{rng.choice(COMPANY_PREFIXES)} {rng.choice(['Environmental', 'Community', 'Industrial', 'Properties', 'Municipal'])} {customer_id:04d}",
                segment,
                region,
                state,
                "Municipality" if segment == "Municipal" else rng.choice(industries[1:]),
                {"Municipal": 45, "Commercial": 30, "Industrial": 30}[segment],
                joined.isoformat(),
                "Active",
            )
        )

    contracts: list[tuple[Any, ...]] = []
    contract_id = 1
    for customer in customers:
        customer_id, segment = customer[0], customer[2]
        contract_count = 2 if customer_id % 3 else 1
        for contract_number in range(1, contract_count + 1):
            service_line = rng.choices(
                WM_SERVICE_LINES,
                weights=[0.18, 0.32, 0.20, 0.16, 0.14],
                k=1,
            )[0]
            start_offset = rng.randint(0, 7)
            start_date = _months(START_MONTH, dt.date(2024, 4, 1))[start_offset]
            ended = contract_id % 31 == 0
            end_date = dt.date(2026, rng.randint(2, 7), 1) if ended else None
            fee_base = {
                "Municipal": 39_000,
                "Commercial": 8_500,
                "Industrial": 24_000,
            }[segment]
            line_factor = {
                "Residential Collection": 1.30,
                "Commercial Collection": 1.00,
                "Roll-Off": 0.82,
                "Recycling": 0.75,
                "Transfer & Disposal": 1.15,
            }[service_line]
            contracts.append(
                (
                    contract_id,
                    customer_id,
                    f"WM-{customer_id:04d}-{contract_number}",
                    service_line,
                    "Fixed plus tonnage" if service_line != "Roll-Off" else "Per haul and tonnage",
                    start_date.isoformat(),
                    end_date.isoformat() if end_date else None,
                    "Expired" if ended else "Active",
                    round(fee_base * line_factor * rng.uniform(0.72, 1.35), 2),
                    round(rng.uniform(42, 78) * line_factor, 2),
                )
            )
            contract_id += 1

    customer_lookup = {row[0]: row for row in customers}
    finance: list[tuple[Any, ...]] = []
    row_id = 1
    west_anomaly_assigned = False
    for contract in contracts:
        (
            cid,
            customer_id,
            _,
            service_line,
            _,
            start_text,
            end_text,
            _,
            base_fee,
            price_per_ton,
        ) = contract
        customer = customer_lookup[customer_id]
        segment, region = customer[2], customer[3]
        start_month = max(START_MONTH, dt.date.fromisoformat(start_text).replace(day=1))
        end_month = min(
            WM_END_MONTH,
            dt.date.fromisoformat(end_text).replace(day=1) if end_text else WM_END_MONTH,
        )
        previous_ar = round(base_fee * rng.uniform(0.35, 0.75), 2)
        anomaly_contract = region == "West" and not west_anomaly_assigned
        if anomaly_contract:
            west_anomaly_assigned = True
        for month_index, month in enumerate(_months(start_month, end_month)):
            season = 1.0 + 0.08 * math.sin((month.month - 1) / 12 * 2 * math.pi)
            if service_line == "Roll-Off" and month.month in (4, 5, 6, 7, 8, 9):
                season *= 1.18
            growth = 1 + month_index * 0.006
            if region == "Southeast" and month >= dt.date(2025, 1, 1):
                growth *= 1.14
            events_base = {
                "Municipal": 380,
                "Commercial": 92,
                "Industrial": 145,
            }[segment]
            events = max(8, int(events_base * season * growth * rng.uniform(0.88, 1.12)))
            tons_per_event = {
                "Residential Collection": 0.45,
                "Commercial Collection": 1.10,
                "Roll-Off": 3.90,
                "Recycling": 0.82,
                "Transfer & Disposal": 5.30,
            }[service_line]
            collection_tons = round(events * tons_per_event * rng.uniform(0.88, 1.13), 2)
            recycling_share = 0.68 if service_line == "Recycling" else rng.uniform(0.08, 0.27)
            recycling_tons = round(collection_tons * recycling_share, 2)
            disposal_tons = round(collection_tons - recycling_tons, 2)
            price_escalation = 1 + 0.031 * max(0, month.year - start_month.year)
            revenue = round(
                (base_fee + collection_tons * price_per_ton + events * (18 if service_line == "Roll-Off" else 7))
                * price_escalation,
                2,
            )
            budget_growth = 1 + 0.007 * month_index
            revenue_budget = round(base_fee * 1.04 * budget_growth + collection_tons * price_per_ton, 2)
            fuel_pressure = 1.0
            disposal_pressure = 1.0
            if region == "West" and month >= dt.date(2026, 1, 1):
                fuel_pressure = 1.32
                disposal_pressure = 1.22
            fuel_cost = round((events * 10.5 + collection_tons * 4.1) * fuel_pressure, 2)
            labor_cost = round(events * (44 if segment == "Municipal" else 38) * rng.uniform(0.96, 1.05), 2)
            disposal_cost = round(disposal_tons * 31.5 * disposal_pressure, 2)
            fleet_cost = round(events * 15.8 + collection_tons * 2.4, 2)
            if anomaly_contract and month == dt.date(2026, 7, 1):
                fleet_cost = round(fleet_cost + 85_000, 2)
            other_cost = round(revenue * rng.uniform(0.055, 0.075), 2)
            operating_cost = round(
                fuel_cost + labor_cost + disposal_cost + fleet_cost + other_cost,
                2,
            )
            operating_profit = round(revenue - operating_cost, 2)
            invoices = revenue
            collection_factor = {
                "Municipal": 0.94,
                "Commercial": 0.985,
                "Industrial": 0.97,
            }[segment]
            if region == "West" and month >= dt.date(2026, 4, 1):
                collection_factor -= 0.035
            cash = round(
                min(
                    previous_ar + invoices,
                    max(0.0, invoices * rng.uniform(collection_factor - 0.025, collection_factor + 0.02) + previous_ar * 0.22),
                ),
                2,
            )
            ar_balance = round(previous_ar + invoices - cash, 2)
            overdue_rate = {"Municipal": 0.38, "Commercial": 0.22, "Industrial": 0.28}[segment]
            overdue_ar = round(ar_balance * overdue_rate * rng.uniform(0.82, 1.18), 2)
            dso = round(30 * ar_balance / invoices, 2) if invoices else 0.0
            finance.append(
                (
                    row_id,
                    cid,
                    month.isoformat(),
                    events,
                    collection_tons,
                    disposal_tons,
                    recycling_tons,
                    revenue,
                    revenue_budget,
                    fuel_cost,
                    labor_cost,
                    disposal_cost,
                    fleet_cost,
                    other_cost,
                    operating_cost,
                    operating_profit,
                    invoices,
                    cash,
                    ar_balance,
                    overdue_ar,
                    dso,
                )
            )
            previous_ar = ar_balance
            row_id += 1
    return customers, contracts, finance


def _generate_products(rng: random.Random) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    product_id = 1
    category_bases = {
        "Analytics": 420.0,
        "Automation": 330.0,
        "Security": 590.0,
        "Data Platform": 510.0,
        "Collaboration": 180.0,
        "Customer Experience": 270.0,
    }
    for category, subcategories in PRODUCT_CATALOG.items():
        for subcategory, names in subcategories.items():
            for edition, multiplier in (("Core", 0.72), ("Pro", 1.0), ("Enterprise", 1.65)):
                base_name = names[(product_id - 1) % len(names)]
                price = round(category_bases[category] * multiplier * rng.uniform(0.9, 1.1), 2)
                cost = round(price * rng.uniform(0.29, 0.52), 2)
                launch = AS_OF_DATE - dt.timedelta(days=rng.randint(240, 1500))
                rows.append(
                    (
                        product_id,
                        f"{base_name} {edition}",
                        category,
                        subcategory,
                        rng.choice(["Subscription", "Usage", "Per Seat"]),
                        price,
                        cost,
                        launch.isoformat(),
                    )
                )
                product_id += 1
    return rows


def _generate_customers(rng: random.Random) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    history_start = START_MONTH - dt.timedelta(days=365)
    for customer_id in range(1, 2501):
        tier = rng.choices(TIERS, weights=TIER_WEIGHTS, k=1)[0]
        segment = {"Free": "SMB", "Pro": "Mid-Market", "Enterprise": "Enterprise"}[tier]
        region = rng.choices(list(REGIONS), weights=REGION_WEIGHTS, k=1)[0]
        country = rng.choice(REGIONS[region])
        signup = history_start + dt.timedelta(
            days=rng.randint(0, (AS_OF_DATE - history_start).days)
        )
        churn_probability = {"Free": 0.12, "Pro": 0.075, "Enterprise": 0.035}[tier]
        churned = rng.random() < churn_probability and signup < AS_OF_DATE - dt.timedelta(days=120)
        churn_date = None
        if churned:
            earliest = max(signup + dt.timedelta(days=90), AS_OF_DATE - dt.timedelta(days=365))
            churn_date = earliest + dt.timedelta(days=rng.randint(0, (AS_OF_DATE - earliest).days))
        employee_range = {"Free": (10, 120), "Pro": (80, 900), "Enterprise": (700, 15000)}[tier]
        employees = rng.randint(*employee_range)
        acv = round(
            {"Free": 1200, "Pro": 14500, "Enterprise": 112000}[tier] * rng.uniform(0.65, 1.5), 2
        )
        rows.append(
            (
                customer_id,
                f"{rng.choice(COMPANY_PREFIXES)} {rng.choice(COMPANY_SUFFIXES)} {customer_id:04d}",
                tier,
                segment,
                region,
                country,
                rng.choice(INDUSTRIES),
                "Small" if employees < 100 else "Medium" if employees < 1000 else "Large",
                employees,
                rng.choice(ACQUISITION_CHANNELS),
                signup.isoformat(),
                "Churned" if churned else "Active",
                churn_date.isoformat() if churn_date else None,
                acv,
            )
        )
    return rows


def _generate_orders(
    rng: random.Random,
    customers: list[tuple[Any, ...]],
    products: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    tiers = {row[0]: row[2] for row in customers}
    regions = {row[0]: row[4] for row in customers}
    signups = {row[0]: dt.date.fromisoformat(row[10]) for row in customers}
    churns = {row[0]: dt.date.fromisoformat(row[12]) if row[12] else None for row in customers}
    product_by_id = {row[0]: row for row in products}
    product_ids = list(product_by_id)
    security_ids = [p[0] for p in products if p[2] == "Security"]
    order_id = 1

    for month_index, month in enumerate(_months(START_MONTH, AS_OF_DATE)):
        seasonality = {
            1: 0.84,
            2: 0.90,
            3: 1.02,
            4: 0.97,
            5: 1.01,
            6: 1.05,
            7: 0.88,
            8: 0.96,
            9: 1.08,
            10: 1.16,
            11: 1.30,
            12: 1.43,
        }[month.month]
        monthly_orders = int((1050 + month_index * 28) * seasonality)
        active_ids = [
            cid
            for cid in tiers
            if signups[cid] <= _month_end(month) and (churns[cid] is None or churns[cid] >= month)
        ]
        apac_active_ids = [cid for cid in active_ids if regions[cid] == "APAC"]
        for _ in range(monthly_orders):
            customer_id = _weighted_customer(rng, active_ids, tiers)
            if month >= dt.date(2025, 10, 1) and rng.random() < 0.12:
                customer_id = _weighted_customer(rng, apac_active_ids, tiers)
            region = regions[customer_id]
            if month >= dt.date(2026, 3, 1) and region == "Europe" and rng.random() < 0.42:
                continue
            product_id = rng.choice(product_ids)
            if month >= dt.date(2026, 1, 1) and rng.random() < 0.18:
                product_id = rng.choice(security_ids)
            product = product_by_id[product_id]
            category, unit_price, unit_cost = product[2], product[5], product[6]
            quantity = rng.randint(1, {"Free": 3, "Pro": 12, "Enterprise": 45}[tiers[customer_id]])
            gross = unit_price * quantity
            discount_rate = rng.uniform(0.01, 0.10)
            if tiers[customer_id] == "Enterprise":
                discount_rate += 0.09
            if region == "LATAM":
                discount_rate += 0.045
            if month.month in (11, 12):
                discount_rate += 0.025
            discount = round(gross * min(discount_rate, 0.32), 2)
            net = round(gross - discount, 2)
            cost = round(unit_cost * quantity, 2)
            profit = round(net - cost, 2)
            status = rng.choices(
                ["delivered", "shipped", "pending", "cancelled", "refunded"],
                weights=[0.73, 0.10, 0.06, 0.065, 0.045],
                k=1,
            )[0]
            promised = rng.choice([2, 3, 5, 7])
            actual = (
                None
                if status in ("pending", "cancelled")
                else max(1, int(rng.gauss(promised, 1.8)))
            )
            on_time = None if actual is None else int(actual <= promised)
            order_date = _date_in_month(rng, month)
            campaign = (
                "Secure Growth"
                if category == "Security" and month >= dt.date(2026, 1, 1)
                else rng.choice(CAMPAIGNS)
            )
            rows.append(
                (
                    order_id,
                    customer_id,
                    product_id,
                    order_date.isoformat(),
                    month.isoformat(),
                    quantity,
                    round(gross, 2),
                    discount,
                    net,
                    cost,
                    profit,
                    status,
                    rng.choice(SALES_CHANNELS),
                    rng.choice(SALES_REPS),
                    campaign,
                    promised,
                    actual,
                    on_time,
                )
            )
            order_id += 1

    apac_enterprise = next(c[0] for c in customers if c[2] == "Enterprise" and c[4] == "APAC")
    security_product = next(p for p in products if p[2] == "Security" and "Enterprise" in p[1])
    rows.append(
        (
            order_id,
            apac_enterprise,
            security_product[0],
            "2026-08-15",
            "2026-08-01",
            180,
            225_000.0,
            22_500.0,
            202_500.0,
            79_000.0,
            123_500.0,
            "delivered",
            "Field Sales",
            "Maya",
            "Secure Growth",
            5,
            4,
            1,
        )
    )
    return rows


def _generate_customer_months(
    rng: random.Random, customers: list[tuple[Any, ...]]
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    row_id = 1
    for customer in customers:
        cid, tier, signup_text, churn_text = customer[0], customer[2], customer[10], customer[12]
        signup = dt.date.fromisoformat(signup_text).replace(day=1)
        churn = dt.date.fromisoformat(churn_text).replace(day=1) if churn_text else None
        for month in _months(max(START_MONTH, signup), AS_OF_DATE):
            if churn and month > churn:
                break
            status = "Churned" if churn == month else "Active"
            base_seats = {"Free": 12, "Pro": 85, "Enterprise": 520}[tier]
            tenure = (month.year - signup.year) * 12 + month.month - signup.month
            seats = max(2, int(base_seats * (1 + tenure * 0.008) * rng.uniform(0.7, 1.35)))
            adoption_base = {"Free": 43, "Pro": 61, "Enterprise": 72}[tier]
            adoption = max(8.0, min(98.0, rng.gauss(adoption_base + min(tenure, 18) * 0.35, 13)))
            if churn and 0 <= (churn.year - month.year) * 12 + churn.month - month.month <= 3:
                adoption *= 0.55
            active_users = max(1, min(seats, int(seats * adoption / 100 * rng.uniform(0.82, 1.08))))
            sessions = int(active_users * rng.uniform(5.0, 18.0))
            tickets = max(0, int(rng.gauss(seats / 95 + (70 - adoption) / 24, 1.3)))
            mrr = round(
                {"Free": 90, "Pro": 1200, "Enterprise": 9200}[tier] * (seats / base_seats), 2
            )
            expansion = round(max(0.0, rng.gauss(mrr * 0.018, mrr * 0.02)), 2)
            contraction = round(max(0.0, rng.gauss(mrr * 0.009, mrr * 0.015)), 2)
            if churn == month:
                contraction = mrr
                mrr = 0.0
            nps = (
                None
                if (cid + month.month) % 3
                else round(max(-100, min(100, rng.gauss(adoption - 28, 22))), 1)
            )
            risk = max(
                1.0,
                min(
                    99.0,
                    78
                    - adoption * 0.62
                    + tickets * 5.2
                    + contraction / max(mrr, 1) * 18
                    + rng.gauss(0, 5),
                ),
            )
            if churn == month:
                risk = max(risk, 92.0)
            rows.append(
                (
                    row_id,
                    cid,
                    month.isoformat(),
                    status,
                    tier,
                    seats,
                    active_users,
                    sessions,
                    round(adoption, 2),
                    mrr,
                    expansion,
                    contraction,
                    nps,
                    round(risk, 2),
                    tickets,
                )
            )
            row_id += 1
    return rows


def _generate_support_tickets(
    rng: random.Random,
    monthly_rows: list[tuple[Any, ...]],
    orders: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    order_ids: dict[int, list[int]] = defaultdict(list)
    for order in orders:
        order_ids[order[1]].append(order[0])
    rows: list[tuple[Any, ...]] = []
    ticket_id = 1
    for monthly in monthly_rows:
        customer_id = monthly[1]
        month = dt.date.fromisoformat(monthly[2])
        risk, count = monthly[13], monthly[14]
        for _ in range(count):
            priority = rng.choices(
                ["Low", "Medium", "High", "Critical"], [0.31, 0.43, 0.20, 0.06], k=1
            )[0]
            category = rng.choice(
                ["Billing", "How To", "Integration", "Performance", "Security", "Bug"]
            )
            response = max(0.1, rng.lognormvariate(0.1 if priority == "Critical" else 0.65, 0.72))
            resolution = max(
                response, rng.lognormvariate(2.7 if priority in ("High", "Critical") else 2.1, 0.75)
            )
            escalated = int(priority in ("High", "Critical") and rng.random() < 0.42)
            reopened = int(rng.random() < (0.05 + risk / 500))
            closed = rng.random() < 0.91
            csat = (
                None
                if not closed or rng.random() < 0.14
                else max(
                    1.0,
                    min(
                        5.0,
                        5.05
                        - math.log1p(resolution) * 0.58
                        - escalated * 0.35
                        + rng.gauss(0, 0.35),
                    ),
                )
            )
            related = (
                rng.choice(order_ids.get(customer_id, [None])) if rng.random() < 0.58 else None
            )
            created = _date_in_month(rng, month)
            rows.append(
                (
                    ticket_id,
                    customer_id,
                    related,
                    created.isoformat(),
                    month.isoformat(),
                    category,
                    priority,
                    "Closed" if closed else "Open",
                    round(response, 2),
                    round(resolution, 2) if closed else None,
                    round(csat, 2) if csat else None,
                    escalated,
                    reopened,
                )
            )
            ticket_id += 1
    return rows


def _generate_marketing(rng: random.Random) -> list[tuple[Any, ...]]:
    channels = ["Paid Search", "Paid Social", "Email", "Partner", "Event", "Organic"]
    rows: list[tuple[Any, ...]] = []
    row_id = 1
    for month_index, month in enumerate(_months(START_MONTH, AS_OF_DATE)):
        for region in REGIONS:
            for channel in channels:
                campaign = rng.choice(CAMPAIGNS)
                spend_base = {
                    "Paid Search": 24000,
                    "Paid Social": 18000,
                    "Email": 4800,
                    "Partner": 13000,
                    "Event": 22000,
                    "Organic": 6200,
                }[channel]
                spend = spend_base * (1 + month_index * 0.012) * rng.uniform(0.75, 1.3)
                if region == "LATAM":
                    spend *= 0.55
                if region == "APAC" and month >= dt.date(2025, 10, 1):
                    spend *= 1.28
                    campaign = "Secure Growth"
                cpm = rng.uniform(12, 34)
                impressions = int(spend / cpm * 1000)
                ctr = {
                    "Email": 0.045,
                    "Organic": 0.034,
                    "Paid Search": 0.027,
                    "Paid Social": 0.018,
                    "Partner": 0.022,
                    "Event": 0.015,
                }[channel]
                clicks = int(impressions * ctr * rng.uniform(0.8, 1.2))
                leads = int(clicks * rng.uniform(0.055, 0.16))
                conversion_rate = rng.uniform(0.12, 0.34)
                if campaign == "Secure Growth" and region == "APAC":
                    conversion_rate *= 1.38
                conversions = max(1, int(leads * min(conversion_rate, 0.55)))
                base_roas = {
                    "Paid Search": 4.8,
                    "Paid Social": 3.2,
                    "Email": 6.4,
                    "Partner": 5.7,
                    "Event": 3.8,
                    "Organic": 7.1,
                }[channel]
                if campaign == "Secure Growth" and region == "APAC":
                    base_roas *= 1.32
                attributed_revenue = spend * base_roas * rng.uniform(0.78, 1.22)
                rows.append(
                    (
                        row_id,
                        month.isoformat(),
                        channel,
                        region,
                        campaign,
                        round(spend, 2),
                        impressions,
                        clicks,
                        leads,
                        conversions,
                        round(attributed_revenue, 2),
                    )
                )
                row_id += 1
    return rows


def _build_monthly_kpis(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    row_id = 1
    month_values = _months(START_MONTH, AS_OF_DATE)
    for month_index, month in enumerate(month_values):
        for region in REGIONS:
            sales = connection.execute(
                """SELECT COALESCE(SUM(o.net_revenue),0), COALESCE(SUM(o.profit),0),
                          COUNT(o.id), COUNT(DISTINCT o.customer_id)
                   FROM orders o JOIN customers c ON c.id=o.customer_id
                   WHERE o.order_month=? AND c.region=?
                     AND o.status NOT IN ('cancelled','refunded')""",
                (month.isoformat(), region),
            ).fetchone()
            new_accounts = connection.execute(
                "SELECT COUNT(*) FROM customers WHERE region=? AND substr(signup_date,1,7)=?",
                (region, month.isoformat()[:7]),
            ).fetchone()[0]
            churned = connection.execute(
                "SELECT COUNT(*) FROM customers WHERE region=? AND substr(churn_date,1,7)=?",
                (region, month.isoformat()[:7]),
            ).fetchone()[0]
            marketing = connection.execute(
                "SELECT COALESCE(SUM(spend),0) FROM marketing_performance "
                "WHERE month=? AND region=?",
                (month.isoformat(), region),
            ).fetchone()[0]
            service = connection.execute(
                """SELECT COUNT(t.id), AVG(t.csat_score)
                   FROM support_tickets t JOIN customers c ON c.id=t.customer_id
                   WHERE t.created_month=? AND c.region=?""",
                (month.isoformat(), region),
            ).fetchone()
            region_base = {
                "North America": 2_450_000,
                "Europe": 1_450_000,
                "APAC": 1_150_000,
                "LATAM": 500_000,
            }[region]
            target = region_base * (1 + month_index * 0.018)
            rows.append(
                (
                    row_id,
                    month.isoformat(),
                    region,
                    round(sales[0], 2),
                    round(target, 2),
                    round(sales[1], 2),
                    round(target * 0.47, 2),
                    sales[2],
                    sales[3],
                    new_accounts,
                    churned,
                    round(marketing, 2),
                    service[0],
                    round(service[1], 2) if service[1] is not None else None,
                )
            )
            row_id += 1
    return rows


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX ix_orders_date ON orders(order_date);
        CREATE INDEX ix_orders_month ON orders(order_month);
        CREATE INDEX ix_orders_customer ON orders(customer_id);
        CREATE INDEX ix_orders_product ON orders(product_id);
        CREATE INDEX ix_orders_status ON orders(status);
        CREATE INDEX ix_customers_region_tier ON customers(region, tier);
        CREATE INDEX ix_customers_signup ON customers(signup_date);
        CREATE INDEX ix_support_month ON support_tickets(created_month);
        CREATE INDEX ix_support_customer ON support_tickets(customer_id);
        CREATE INDEX ix_customer_metrics_month ON customer_monthly_metrics(month);
        CREATE INDEX ix_customer_metrics_customer ON customer_monthly_metrics(customer_id);
        CREATE INDEX ix_marketing_month_region ON marketing_performance(month, region);
        CREATE INDEX ix_kpi_month_region ON business_kpi_monthly(month, region);
        CREATE INDEX ix_wm_customers_region_segment ON wm_customers(region, customer_segment);
        CREATE INDEX ix_wm_contracts_customer ON wm_contracts(customer_id);
        CREATE INDEX ix_wm_contracts_service ON wm_contracts(service_line);
        CREATE INDEX ix_wm_finance_month ON wm_finance_monthly(month);
        CREATE INDEX ix_wm_finance_contract ON wm_finance_monthly(contract_id);
        """
    )


def _ground_truth(connection: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    tables = [
        "customers",
        "products",
        "orders",
        "support_tickets",
        "customer_monthly_metrics",
        "marketing_performance",
        "business_kpi_monthly",
        "wm_customers",
        "wm_contracts",
        "wm_finance_monthly",
    ]
    row_counts = {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }
    checks = {
        "completed_net_revenue": (
            "SELECT ROUND(SUM(net_revenue),2) FROM orders "
            "WHERE status NOT IN ('cancelled','refunded')"
        ),
        "completed_profit": (
            "SELECT ROUND(SUM(profit),2) FROM orders WHERE status NOT IN ('cancelled','refunded')"
        ),
        "purchasing_customers": "SELECT COUNT(DISTINCT customer_id) FROM orders",
        "active_accounts": "SELECT COUNT(*) FROM customers WHERE account_status='Active'",
        "apac_largest_order": (
            "SELECT MAX(net_revenue) FROM orders o "
            "JOIN customers c ON c.id=o.customer_id WHERE c.region='APAC'"
        ),
        "europe_prior_six_month_revenue": (
            "SELECT ROUND(SUM(o.net_revenue),2) FROM orders o "
            "JOIN customers c ON c.id=o.customer_id WHERE c.region='Europe' "
            "AND o.status NOT IN ('cancelled','refunded') "
            "AND o.order_date>='2025-09-01' AND o.order_date<'2026-03-01'"
        ),
        "europe_recent_six_month_revenue": (
            "SELECT ROUND(SUM(o.net_revenue),2) FROM orders o "
            "JOIN customers c ON c.id=o.customer_id WHERE c.region='Europe' "
            "AND o.status NOT IN ('cancelled','refunded') "
            "AND o.order_date>='2026-03-01' AND o.order_date<'2026-09-01'"
        ),
        "apac_secure_growth_roas": (
            "SELECT ROUND(SUM(attributed_revenue)/SUM(spend),4) "
            "FROM marketing_performance "
            "WHERE region='APAC' AND campaign_name='Secure Growth'"
        ),
        "high_risk_accounts_latest_month": (
            "SELECT COUNT(DISTINCT customer_id) FROM customer_monthly_metrics "
            "WHERE month='2026-09-01' AND churn_risk_score>=70"
        ),
        "average_closed_ticket_csat": (
            "SELECT ROUND(AVG(csat_score),4) FROM support_tickets WHERE status='Closed'"
        ),
        "regions_below_target_2026": (
            "SELECT COUNT(*) FROM (SELECT region FROM business_kpi_monthly "
            "WHERE month>='2026-01-01' GROUP BY region "
            "HAVING SUM(net_revenue)<SUM(revenue_target))"
        ),
        "low_adoption_average_risk": (
            "SELECT ROUND(AVG(churn_risk_score),4) FROM customer_monthly_metrics "
            "WHERE feature_adoption_pct<40"
        ),
        "high_adoption_average_risk": (
            "SELECT ROUND(AVG(churn_risk_score),4) FROM customer_monthly_metrics "
            "WHERE feature_adoption_pct>=70"
        ),
        "wm_2026_ytd_revenue": (
            "SELECT ROUND(SUM(revenue),2) FROM wm_finance_monthly "
            "WHERE month>='2026-01-01' AND month<'2026-09-01'"
        ),
        "wm_2026_ytd_operating_margin_pct": (
            "SELECT ROUND(100.0*SUM(operating_profit)/SUM(revenue),4) "
            "FROM wm_finance_monthly WHERE month>='2026-01-01' AND month<'2026-09-01'"
        ),
        "wm_2026_ytd_budget_variance": (
            "SELECT ROUND(SUM(revenue)-SUM(revenue_budget),2) FROM wm_finance_monthly "
            "WHERE month>='2026-01-01' AND month<'2026-09-01'"
        ),
        "wm_operating_cost_identity_violations": (
            "SELECT COUNT(*) FROM wm_finance_monthly WHERE "
            "ABS(operating_cost-(fuel_cost+labor_cost+disposal_cost+fleet_cost+other_operating_cost))>0.01"
        ),
        "wm_operating_profit_identity_violations": (
            "SELECT COUNT(*) FROM wm_finance_monthly WHERE "
            "ABS(operating_profit-(revenue-operating_cost))>0.01"
        ),
        "wm_duplicate_contract_months": (
            "SELECT COUNT(*) FROM (SELECT contract_id,month,COUNT(*) AS n "
            "FROM wm_finance_monthly GROUP BY contract_id,month HAVING n>1)"
        ),
        "wm_required_nulls": (
            "SELECT COUNT(*) FROM wm_finance_monthly WHERE month IS NULL OR revenue IS NULL "
            "OR operating_cost IS NULL OR operating_profit IS NULL OR ar_balance IS NULL"
        ),
        "wm_west_2026_margin_pct": (
            "SELECT ROUND(100.0*SUM(f.operating_profit)/SUM(f.revenue),4) "
            "FROM wm_finance_monthly f JOIN wm_contracts c ON c.id=f.contract_id "
            "JOIN wm_customers w ON w.id=c.customer_id "
            "WHERE w.region='West' AND f.month>='2026-01-01' AND f.month<'2026-09-01'"
        ),
        "wm_west_2025_margin_pct": (
            "SELECT ROUND(100.0*SUM(f.operating_profit)/SUM(f.revenue),4) "
            "FROM wm_finance_monthly f JOIN wm_contracts c ON c.id=f.contract_id "
            "JOIN wm_customers w ON w.id=c.customer_id "
            "WHERE w.region='West' AND f.month>='2025-01-01' AND f.month<'2025-09-01'"
        ),
        "wm_ar_rollforward_violations": (
            "SELECT COUNT(*) FROM wm_finance_monthly current "
            "JOIN wm_finance_monthly prior ON prior.contract_id=current.contract_id "
            "AND prior.month=DATE(current.month,'-1 month') "
            "WHERE ABS(current.ar_balance-(prior.ar_balance+current.invoices_billed-current.cash_collected))>0.01"
        ),
    }
    values = {name: connection.execute(sql).fetchone()[0] for name, sql in checks.items()}
    return {
        "schema_version": "3.0",
        "seed": SEED,
        "as_of_date": AS_OF_DATE.isoformat(),
        "database": str(db_path.resolve()),
        "row_counts": row_counts,
        "date_coverage": {"start": START_MONTH.isoformat(), "end": AS_OF_DATE.isoformat()},
        "proof_queries": {
            name: {"sql": sql, "expected_scalar": values[name]} for name, sql in checks.items()
        },
        "documented_signals": [
            "Europe demand declines during the latest six complete months.",
            "APAC commercial demand and marketing investment accelerate from October 2025.",
            "Security demand accelerates in 2026.",
            "One large APAC Security order on 2026-08-15 is a controlled anomaly.",
            "Low adoption, support burden, contraction, and churn risk move "
            "together by construction.",
            "LATAM has systematically higher discount pressure.",
            "Waste-management tables are clearly synthetic and use a contract-month fact grain.",
            "West fuel and disposal rates increase during 2026 and reduce operating margin.",
            "Southeast service volume accelerates from January 2025.",
            "One West contract receives a controlled fleet-repair anomaly in July 2026.",
            "Operating cost, operating profit, and accounts-receivable roll-forward identities reconcile.",
        ],
    }


def generate(
    output_path: Path | str = DB_PATH,
    *,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Generate the demo database and return its ground-truth manifest."""
    rng = random.Random(SEED)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    with sqlite3.connect(target) as connection:
        _create_schema(connection)
        products = _generate_products(rng)
        customers = _generate_customers(rng)
        orders = _generate_orders(rng, customers, products)
        monthly = _generate_customer_months(rng, customers)
        tickets = _generate_support_tickets(rng, monthly, orders)
        marketing = _generate_marketing(rng)
        wm_customers, wm_contracts, wm_finance = _generate_waste_finance(rng)
        connection.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?)", products)
        connection.executemany(
            "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", customers
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", orders
        )
        connection.executemany(
            "INSERT INTO customer_monthly_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", monthly
        )
        connection.executemany(
            "INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tickets
        )
        connection.executemany(
            "INSERT INTO marketing_performance VALUES (?,?,?,?,?,?,?,?,?,?,?)", marketing
        )
        kpis = _build_monthly_kpis(connection)
        connection.executemany(
            "INSERT INTO business_kpi_monthly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", kpis
        )
        connection.executemany(
            "INSERT INTO wm_customers VALUES (?,?,?,?,?,?,?,?,?)", wm_customers
        )
        connection.executemany(
            "INSERT INTO wm_contracts VALUES (?,?,?,?,?,?,?,?,?,?)", wm_contracts
        )
        connection.executemany(
            "INSERT INTO wm_finance_monthly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            wm_finance,
        )
        _create_indexes(connection)
        connection.execute("ANALYZE")
        connection.commit()
        manifest = _ground_truth(connection, target)

    manifest["database_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    resolved_manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else (
            GROUND_TRUTH_PATH
            if target.resolve() == DB_PATH.resolve()
            else target.with_suffix(".ground_truth.json")
        )
    )
    resolved_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    total_rows = sum(manifest["row_counts"].values())
    print(f"Generated {target}: {total_rows:,} rows across 10 analytical tables.")
    print(f"Ground truth: {resolved_manifest}")
    return manifest


if __name__ == "__main__":
    generate()
