"""
Configuration package for the financial document intelligence platform.
"""

import json
import os

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

def load_json(filename: str) -> dict:
    """Load a JSON config file from the config directory."""
    path = os.path.join(CONFIG_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_financial_terms() -> dict:
    """Load financial terms dictionary."""
    return load_json("financial_terms.json")

def get_financial_synonyms() -> dict:
    """Load financial synonyms dictionary."""
    return load_json("financial_synonyms.json")

def get_common_words() -> dict:
    """Load common words with IDF weights."""
    return load_json("common_words.json")