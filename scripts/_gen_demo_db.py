"""
One-off generator for a realistic demo SQLite database (not part of the
package -- a throwaway script for building data/demo.sqlite).

Schema: a small B2B SaaS-style e-commerce dataset.
  customers(id, name, tier, region, signup_date)
  products(id, name, category, unit_price)
  orders(id, customer_id, product_id, revenue, status, order_date)
"""

from __future__ import annotations

import datetime
import random
import sqlite3
from pathlib import Path

random.seed(42)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "demo.sqlite"

TIERS = ["Free", "Pro", "Enterprise"]
TIER_WEIGHTS = [0.55, 0.30, 0.15]
REGIONS = ["North America", "Europe", "APAC", "LATAM"]
REGION_WEIGHTS = [0.45, 0.30, 0.18, 0.07]

CATEGORIES = {
    "Analytics": ["Insight Dashboard", "Metrics Suite", "Trend Tracker", "Pulse Reports"],
    "Automation": ["Flow Builder", "TaskBot", "Workflow Engine", "AutoSync"],
    "Storage": ["CloudVault", "ArchiveBox", "DataLake Pro", "BackupStream"],
    "Security": ["ShieldGuard", "AccessLock", "ThreatScan", "SecureGate"],
    "Collaboration": ["TeamSpace", "MeetSync", "NoteBoard", "ChatHub"],
}
CATEGORY_PRICE_RANGE = {
    "Analytics": (49, 499),
    "Automation": (29, 299),
    "Storage": (19, 199),
    "Security": (99, 799),
    "Collaboration": (9, 149),
}

COMPANY_PREFIXES = [
    "Bright", "Silver", "North", "Blue", "Summit", "Cedar", "River", "Vertex",
    "Clear", "Bold", "Swift", "Golden", "Ember", "Crest", "Lumen", "Anchor",
    "Vivid", "Quartz", "Harbor", "Falcon", "Maple", "Onyx", "Pioneer", "Delta",
]
COMPANY_SUFFIXES = [
    "Analytics", "Systems", "Labs", "Works", "Group", "Solutions", "Technologies",
    "Dynamics", "Ventures", "Partners", "Networks", "Industries", "Holdings", "Co.",
]

STATUSES = ["delivered", "shipped", "pending", "cancelled", "refunded"]
STATUS_WEIGHTS = [0.62, 0.15, 0.08, 0.10, 0.05]


def _random_date(start: datetime.date, end: datetime.date) -> datetime.date:
    delta_days = (end - start).days
    return start + datetime.timedelta(days=random.randint(0, delta_days))


def generate() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            tier TEXT NOT NULL,
            region TEXT NOT NULL,
            signup_date TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            revenue REAL NOT NULL,
            status TEXT NOT NULL,
            order_date TEXT NOT NULL
        )
        """
    )

    today = datetime.date(2026, 9, 5)
    history_start = today - datetime.timedelta(days=18 * 30)

    # ── Products ─────────────────────────────────────────────────────────────
    product_rows: list[tuple[int, str, str, float]] = []
    product_id = 1
    for category, names in CATEGORIES.items():
        low, high = CATEGORY_PRICE_RANGE[category]
        for name in names:
            price = round(random.uniform(low, high), 2)
            product_rows.append((product_id, name, category, price))
            product_id += 1
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", product_rows)

    # ── Customers ────────────────────────────────────────────────────────────
    # Company names aren't required to be globally unique -- two different
    # customer IDs sharing a name is realistic (and 24*14=336 combinations
    # can't uniquely cover 400 customers anyway; a prior version of this
    # script tried to enforce uniqueness here and looped forever once the
    # combination pool was exhausted).
    customer_rows: list[tuple[int, str, str, str, str]] = []
    for cid in range(1, 401):
        name = f"{random.choice(COMPANY_PREFIXES)}{random.choice(COMPANY_SUFFIXES)}"
        tier = random.choices(TIERS, weights=TIER_WEIGHTS)[0]
        region = random.choices(REGIONS, weights=REGION_WEIGHTS)[0]
        signup = _random_date(history_start, today)
        customer_rows.append((cid, name, tier, region, signup.isoformat()))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customer_rows)

    # ── Orders ───────────────────────────────────────────────────────────────
    # Enterprise/Pro tiers order more often and skew toward pricier products.
    tier_by_customer = {row[0]: row[2] for row in customer_rows}
    order_rows: list[tuple[int, int, int, float, str, str]] = []
    order_id = 1
    for cid, tier in tier_by_customer.items():
        base_orders = {"Free": (0, 3), "Pro": (2, 12), "Enterprise": (5, 30)}[tier]
        num_orders = random.randint(*base_orders)
        for _ in range(num_orders):
            product = random.choice(product_rows)
            _, _, category, unit_price = product
            quantity = random.randint(1, 5)
            revenue = round(unit_price * quantity * random.uniform(0.9, 1.05), 2)
            status = random.choices(STATUSES, weights=STATUS_WEIGHTS)[0]
            order_date = _random_date(history_start, today)
            order_rows.append(
                (order_id, cid, product[0], revenue, status, order_date.isoformat())
            )
            order_id += 1
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", order_rows)

    conn.execute("CREATE INDEX ix_orders_customer_id ON orders(customer_id)")
    conn.execute("CREATE INDEX ix_orders_product_id ON orders(product_id)")
    conn.execute("CREATE INDEX ix_orders_order_date ON orders(order_date)")
    conn.commit()
    conn.close()

    print(
        f"Generated {DB_PATH}: {len(product_rows)} products, "
        f"{len(customer_rows)} customers, {len(order_rows)} orders."
    )


if __name__ == "__main__":
    generate()
