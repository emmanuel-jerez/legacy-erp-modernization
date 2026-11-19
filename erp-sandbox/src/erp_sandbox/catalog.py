"""Catalog for the fictional demo brand, Fernhollow Skincare.

Fernhollow is invented for this sandbox: product names, prices, and descriptions are
placeholders shaped like a direct-to-consumer skincare catalog. No real company's data
is used. ``build_catalog`` is deterministic so every machine seeds the same ERP.
"""

from __future__ import annotations

from typing import Any

BRAND = "Fernhollow Skincare (fictional)"

# (handle, title, product_type, base_price, sizes, description)
_PRODUCTS: list[tuple[str, str, str, float, list[str], str]] = [
    ("cooling-eye-gel", "Cooling Eye Gel", "Eye Care", 39.00, ["0.5 oz", "1 oz"],
     "Lightweight gel for the under-eye area with caffeine and cucumber extract."),
    ("overnight-eye-mask", "Overnight Eye Mask", "Eye Care", 44.00, ["0.5 oz"],
     "Rich overnight balm for the eye contour."),
    ("brightening-eye-patches", "Brightening Eye Patches", "Eye Care", 28.00, ["30 pairs", "60 pairs"],
     "Single-use hydrogel patches with niacinamide."),
    ("vitamin-c-serum", "Vitamin C Serum", "Serums", 52.00, ["1 oz", "2 oz"],
     "15% ascorbic acid serum with ferulic acid."),
    ("retinol-night-serum", "Retinol Night Serum", "Serums", 58.00, ["1 oz"],
     "Encapsulated 0.3% retinol serum for evening use."),
    ("hyaluronic-hydration-serum", "Hyaluronic Hydration Serum", "Serums", 46.00, ["1 oz", "2 oz"],
     "Multi-weight hyaluronic acid serum."),
    ("peptide-firming-serum", "Peptide Firming Serum", "Serums", 64.00, ["1 oz"],
     "Peptide complex serum for firmness."),
    ("niacinamide-pore-serum", "Niacinamide Pore Serum", "Serums", 34.00, ["1 oz"],
     "10% niacinamide with zinc."),
    ("daily-moisturizer", "Daily Moisturizer", "Moisturizers", 42.00, ["1.7 oz", "3.4 oz"],
     "Everyday cream with ceramides and squalane."),
    ("rich-barrier-cream", "Rich Barrier Cream", "Moisturizers", 48.00, ["1.7 oz"],
     "Occlusive cream for dry skin."),
    ("oil-free-gel-cream", "Oil-Free Gel Cream", "Moisturizers", 40.00, ["1.7 oz"],
     "Water-gel moisturizer for oily skin."),
    ("gentle-gel-cleanser", "Gentle Gel Cleanser", "Cleansers", 26.00, ["5 oz", "10 oz"],
     "Low-foam, pH-balanced daily cleanser."),
    ("cleansing-balm", "Cleansing Balm", "Cleansers", 32.00, ["3 oz"],
     "Melting balm that removes sunscreen and makeup."),
    ("exfoliating-toner", "Exfoliating Toner", "Toners", 30.00, ["6 oz"],
     "Glycolic and lactic acid toner."),
    ("hydrating-essence", "Hydrating Essence", "Toners", 36.00, ["5 oz"],
     "Fermented essence with panthenol."),
    ("mineral-sunscreen-spf-40", "Mineral Sunscreen SPF 40", "Sun Care", 34.00, ["1.7 oz"],
     "Zinc oxide daily sunscreen, sheer finish."),
    ("tinted-sunscreen-spf-30", "Tinted Sunscreen SPF 30", "Sun Care", 38.00, ["1.7 oz"],
     "Tinted mineral sunscreen in three shades."),
    ("clay-detox-mask", "Clay Detox Mask", "Masks", 29.00, ["2 oz"],
     "Kaolin and bentonite wash-off mask."),
    ("overnight-recovery-mask", "Overnight Recovery Mask", "Masks", 45.00, ["2 oz"],
     "Leave-on sleeping mask."),
    ("lip-repair-balm", "Lip Repair Balm", "Lip Care", 16.00, ["0.35 oz"],
     "Shea and lanolin lip balm."),
    ("body-firming-lotion", "Body Firming Lotion", "Body", 38.00, ["8 oz"],
     "Caffeine body lotion."),
    ("hand-cream", "Hand Cream", "Body", 18.00, ["2.5 oz"],
     "Fast-absorbing hand cream."),
    ("jade-roller", "Jade Roller", "Tools", 24.00, ["One size"],
     "Facial roller, stone may vary."),
    ("gua-sha-stone", "Gua Sha Stone", "Tools", 22.00, ["One size"],
     "Rose quartz gua sha."),
    ("eye-care-duo", "Eye Care Duo", "Bundles", 72.00, ["Kit"],
     "Cooling Eye Gel and Overnight Eye Mask."),
    ("morning-routine-kit", "Morning Routine Kit", "Bundles", 118.00, ["Kit"],
     "Cleanser, Vitamin C Serum, Daily Moisturizer and Mineral Sunscreen."),
    ("night-routine-kit", "Night Routine Kit", "Bundles", 129.00, ["Kit"],
     "Cleansing Balm, Retinol Night Serum and Rich Barrier Cream."),
    ("travel-minis-set", "Travel Minis Set", "Bundles", 49.00, ["Kit"],
     "Five travel-size favourites."),
]

_PACKS = {"Bundles": [""], "Tools": [""]}


def build_catalog() -> dict[str, Any]:
    """Return the fictional catalog in a storefront-like shape (products with variants)."""
    products: list[dict[str, Any]] = []
    for index, (handle, title, ptype, base, sizes, description) in enumerate(_PRODUCTS, start=1):
        variants = []
        for size_index, size in enumerate(sizes):
            price = round(base * (1 + 0.65 * size_index), 2)
            packs = _PACKS.get(ptype, ["", "2-Pack"]) if size_index == 0 else [""]
            for pack in packs:
                pack_price = round(price * 1.8, 2) if pack else price
                code = f"FH-{index:03d}-{size_index + 1}{'P2' if pack else ''}"
                variant_title = " / ".join(part for part in (size, pack) if part)
                variants.append(
                    {
                        "sku": code,
                        "title": variant_title,
                        "price": f"{pack_price:.2f}",
                        "compare_at_price": f"{round(pack_price * 1.2, 2):.2f}" if pack else None,
                        "grams": 0 if ptype == "Bundles" else (60 + 40 * size_index) * (2 if pack else 1),
                    }
                )
        products.append(
            {
                "handle": handle,
                "title": title,
                "product_type": ptype,
                "vendor": BRAND,
                "description": description,
                "variants": variants,
            }
        )
    return {"brand": BRAND, "fictional": True, "products": products}
