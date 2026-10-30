"""The strategies each product can be sold with, as adtini offers them.

From the "adtini orders" sheet, Product Overview tab, Strategy Values
column (1tOAJ3s8qUxacOliSEZawXo5JcX_ejTUmYC6rpX2KndI, 2026-10-30). Where a
product sells several ad types ("Video: ...", "CTV: ...") the lists are
merged: the grid row is already one ad type's line item.
"""
from __future__ import annotations

import re

_SHEET = {
    "Amazon Premium CTV + Video Ads": "Video: Category Targeting, Custom Audience Lookalike, Custom Audience Matching, Product Targeting, Website Retargeting\nCTV: Category Targeting, Custom Audience Lookalike, Custom Audience Matching, Product Targeting, Website Retargeting\nPrime CTV: Genre Targeting",
    "Amazon Premium Display Ads": "Category Targeting, Custom Audience Lookalike, Custom Audience Matching, Product Targeting, Website Retargeting",
    "Connected TV Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, CTV Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Website Retargeting Lookalike",
    "CTV + Video Ads": "Video: AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Geo-Fencing, Geo-Retargeting, Keyword Targeting, Website Visitor ID Feed, Website Retargeting Lookalike\nCTV: AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, CTV Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Website Retargeting Lookalike",
    "Digital Out-Of-Home (DOOH) Display & Video Ads": "Venue Targeting, Weather Trigger",
    "Display Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Keyword Targeting, Website Visitor ID Feed, Geo-Fencing, Geo-Retargeting, Mobile Cross Platform Targeting, Website Retargeting Lookalike",
    "LinkedIn Ads": "Audience DNA, Category Targeting, Custom Audience Matching, Custom Audience Predictive, Website Retargeting",
    "Meta Display + Video Ads": "Category Targeting, Custom Audience Lookalike, Custom Audience Matching, Mobile Cross Platform Targeting, Website Visitor ID Feed, Website Retargeting",
    "Meta Lead Display + Video": "Custom Audience Lookalike, Custom Audience Matching, Category Targeting, Website Retargeting",
    "Mobile Conquesting Display & Video Ads": "Address Targeting & Retargeting, Audience DNA, Category Targeting, Geo-Fencing, Geo-Retargeting, Geo-Retargeting Lookalike, Weather Trigger, Website Retargeting",
    "Mobile Conquesting Event/Political Display & Video Ads": "Event: Event Geo-Fencing, Event Geo-Retargeting, Website Retargeting\nPolitical: Category Targeting, Geo-Fencing, Geo-Retargeting, Geo-Retargeting Lookalike, Website Retargeting",
    "Native Display Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Keyword Targeting, Website Visitor ID Feed, Geo-Fencing, Geo-Retargeting, Mobile Cross Platform Targeting, Website Retargeting Lookalike",
    "Native Video Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Keyword Targeting, Website Retargeting, Website Visitor ID Feed, Website Retargeting Lookalike",
    "Online Audio Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Website Visitor ID Feed, Website Retargeting Lookalike",
    "Pay-Per-Click Ads": "Dynamic URL Targets, Keywords",
    "Performance Max Ads": "Search Theme Targeting, Category Targeting, Website Retargeting",
    "Social Mirror Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Keyword Targeting, Website Visitor ID Feed, Geo-Fencing, Geo-Retargeting, Mobile Cross Platform Targeting, Website Retargeting Lookalike",
    "Social Mirror CTV Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, CTV Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Website Retargeting Lookalike",
    "TikTok Display & Video Ads": "Category Targeting, Hashtag Targeting, Website Retargeting",
    "Video Ads": "AI Targeting, B2B Behavioral Targeting, Behavioral Targeting, Content Targeting, Custom Audience Lookalike, Custom Audience Matching, Website Retargeting, Keyword Targeting, Website Visitor ID Feed, Geo-Fencing, Geo-Retargeting, Mobile Cross Platform Targeting, Website Retargeting Lookalike",
    "YouTube+ Video Ads": "Category Targeting, Website Retargeting",
}


def _key(product: str | None) -> str:
    text = (product or "").lower()
    text = re.sub(r"\((barck\+|spend|mgmt)\)|\*", "", text)
    text = text.replace("&", "and").replace("+", "and")
    return re.sub(r"[^a-z0-9]", "", text)


def _parse(raw: str) -> list[str]:
    names: list[str] = []
    for line in raw.splitlines():
        line = re.sub(r"^[A-Za-z ]+:\s*", "", line.strip())
        for name in line.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


_MENU = {_key(product): _parse(raw) for product, raw in _SHEET.items()}


def options(product: str | None) -> list[str] | None:
    """adtini's strategies for this product, or None when it is not listed."""
    key = _key(product)
    if key in _MENU:
        return _MENU[key]
    # "Meta Display & Video Ads" is sold under a few spellings.
    for name, menu in _MENU.items():
        if key and (key.startswith(name) or name.startswith(key)):
            return menu
    return None
