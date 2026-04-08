#!/usr/bin/env python3
"""Seed the real estate ontology into Neptune.

Usage:
    .venv/bin/python scripts/seed_ontology.py [--api-url URL] [--api-key KEY] [--tenant TENANT]

Defaults to the demo-tenant dev environment.
"""

import argparse
import sys

import httpx

TYPES = [
    {
        "name": "Property",
        "description": "A real estate property (house, condo, land, etc.)",
        "attributes": [
            {"name": "address", "datatype": "string", "description": "Street address"},
            {"name": "city", "datatype": "string", "description": "City name"},
            {"name": "state", "datatype": "string", "description": "State or province"},
            {"name": "zip_code", "datatype": "string", "description": "Postal code"},
            {"name": "price", "datatype": "integer", "description": "Listing price in dollars"},
            {"name": "bedrooms", "datatype": "integer", "description": "Number of bedrooms"},
            {"name": "bathrooms", "datatype": "integer", "description": "Number of bathrooms"},
            {"name": "sqft", "datatype": "integer", "description": "Living area in square feet"},
            {"name": "lot_sqft", "datatype": "integer", "description": "Lot size in square feet"},
            {"name": "year_built", "datatype": "integer", "description": "Year the property was built"},
            {"name": "property_type", "datatype": "string", "description": "Type: single-family, condo, townhouse, etc."},
            {"name": "status", "datatype": "string", "description": "Listing status: active, pending, sold"},
            {"name": "location", "datatype": "Place", "description": "Where the property is located"},
            {"name": "listed_by", "datatype": "Person", "description": "Agent who listed the property"},
        ],
    },
    {
        "name": "Place",
        "description": "A geographic location (neighborhood, city, landmark, etc.)",
        "attributes": [
            {"name": "name", "datatype": "string", "description": "Place name"},
            {"name": "latitude", "datatype": "float", "description": "Latitude coordinate"},
            {"name": "longitude", "datatype": "float", "description": "Longitude coordinate"},
            {"name": "place_type", "datatype": "string", "description": "Type: city, neighborhood, landmark, park"},
            {"name": "population", "datatype": "integer", "description": "Population count"},
            {"name": "walk_score", "datatype": "integer", "description": "Walk Score (0-100)"},
        ],
    },
    {
        "name": "Park",
        "description": "A public park or recreational area",
        "parent_type": "Place",
        "attributes": [
            {"name": "acreage", "datatype": "float", "description": "Park size in acres"},
            {"name": "amenities", "datatype": "string", "description": "Available amenities (comma-separated)"},
            {"name": "open_hours", "datatype": "string", "description": "Operating hours"},
        ],
    },
    {
        "name": "Person",
        "description": "A person (agent, buyer, seller, etc.)",
        "attributes": [
            {"name": "name", "datatype": "string", "description": "Full name"},
            {"name": "email", "datatype": "string", "description": "Email address"},
            {"name": "phone", "datatype": "string", "description": "Phone number"},
            {"name": "role", "datatype": "string", "description": "Role: agent, buyer, seller, inspector"},
            {"name": "license_number", "datatype": "string", "description": "Professional license number"},
        ],
    },
    {
        "name": "Transaction",
        "description": "A real estate transaction (sale, lease, etc.)",
        "attributes": [
            {"name": "transaction_type", "datatype": "string", "description": "Type: sale, lease, rental"},
            {"name": "sale_price", "datatype": "integer", "description": "Final sale price"},
            {"name": "closing_date", "datatype": "datetime", "description": "Date of closing"},
            {"name": "days_on_market", "datatype": "integer", "description": "Days the property was listed"},
            {"name": "property", "datatype": "Property", "description": "The property being transacted"},
            {"name": "buyer", "datatype": "Person", "description": "The buyer in the transaction"},
            {"name": "seller", "datatype": "Person", "description": "The seller in the transaction"},
            {"name": "agent", "datatype": "Person", "description": "The agent handling the transaction"},
        ],
    },
]


def seed(api_url: str, api_key: str, tenant: str) -> None:
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    base = f"{api_url}/graphs/{tenant}/ontology"

    # Fetch existing types
    try:
        res = httpx.get(f"{base}/types", headers=headers, timeout=10)
        existing = {t["name"] for t in res.json()} if res.status_code == 200 else set()
    except Exception:
        existing = set()

    created = 0
    skipped = 0

    for type_def in TYPES:
        name = type_def["name"]
        if name in existing:
            print(f"  SKIP  {name} (already exists)")
            skipped += 1
            continue

        res = httpx.post(f"{base}/types", headers=headers, json=type_def, timeout=10)
        if res.status_code == 201:
            print(f"  CREATE {name} ({len(type_def.get('attributes', []))} attrs)")
            created += 1
        else:
            print(f"  ERROR  {name}: {res.status_code} {res.text}")

    print(f"\nDone: {created} created, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(description="Seed real estate ontology")
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Omnix API base URL",
    )
    parser.add_argument("--api-key", default="dev-key-001", help="API key")
    parser.add_argument("--tenant", default="demo-tenant", help="Tenant ID")
    args = parser.parse_args()

    print(f"Seeding ontology → {args.api_url}")
    print(f"Tenant: {args.tenant}\n")
    seed(args.api_url, args.api_key, args.tenant)


if __name__ == "__main__":
    main()
