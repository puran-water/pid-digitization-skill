#!/usr/bin/env python3
"""
Validation for PID Extraction Results.

Stage 3 of the PID digitization pipeline:
- Validates ISA tag formats with 3-tier pattern recognition
- Checks equipment-instrument linkage via spatial proximity
- Detects cross-page duplicates and flags them
- Groups instruments into control loops by tag prefix
- Applies VLM classification data (from apply_review.py)
- Computed confidence scoring with bonuses/penalties
- Required field validation by device class
- Cross-reference validation (equipment_tag must exist)
- Flags low-confidence items for review

Usage:
    # With merged results (recommended)
    python validate_extraction.py merged_results.json --output-dir ./validated/

    # With project config for disambiguation
    python validate_extraction.py merged_results.json --config project_config.yaml

    # Strict mode (warnings as errors)
    python validate_extraction.py merged_results.json --strict
"""

import argparse
import difflib
import json
import re
import sys
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    print("Error: pyyaml required. Install with: pip install pyyaml")
    sys.exit(1)


# =============================================================================
# VALIDATION CONFIGURATION
# =============================================================================

VALIDATION_CONFIG = {
    # Confidence scoring parameters (per plan document)
    'confidence': {
        'base': 0.50,
        'bonuses': {
            'bbox_evidence': 0.20,        # Has source bounding box
            'vlm_consensus': 0.20,        # Claude + Gemini agree (plan: +0.20)
            'isa_compliant_tag': 0.10,    # Matches canonical ISA format
            'context_validated': 0.10,    # Nearby context matched rules (plan: +0.10)
            'gemini_verified': 0.10,      # Gemini reviewed and agreed
            'tag_existence_validated': 0.15,  # Tag found in vector text extraction
        },
        'penalties': {
            'vlm_disagree': -0.20,        # Gemini corrected Claude
            'no_bbox': -0.15,             # VLM-only discovery
            'abbreviation_ambiguous': -0.10,  # TS, SM, EJ ambiguous
            'missing_required_field': -0.10,  # Per missing field (plan: -0.10)
            'duplicate_tag': -0.10,       # Tag appears multiple times
            'tag_not_in_vector': -0.10,   # VLM-only tag (no vector text corroboration)
        }
    },

    # Review threshold - below this triggers review_required
    'review_threshold': 0.80,

    # Required fields by device class (per plan document)
    # Note: 'primary_signal_type' is the actual field name used in the schema;
    # plan used 'signal_type' as shorthand
    'required_fields': {
        'transmitter': ['primary_signal_type', 'range', 'loop_key'],
        'switch': ['setpoint_type', 'loop_key'],  # H, L, HH, LL
        'gauge': ['range'],
        'indicator': [],
        'control_valve': ['fail_position', 'actuator_type', 'primary_signal_type'],
        'on_off_valve': ['fail_position', 'actuator_type'],
        'analyzer': ['analyte'],
    },

    # Validation checks to run
    'checks': {
        'uniqueness': True,           # Flag duplicate tags
        'bbox_evidence': True,        # Prefer items with bbox
        'required_fields': True,      # Check device-class required fields
        'cross_reference': True,      # equipment_tag must exist
        'isa_format': True,           # Tag must match ISA pattern
        'tag_existence': True,        # Validate tags exist in vector text
    },

    # Tag existence validation settings
    'tag_existence': {
        'fuzzy_threshold': 0.85,      # Minimum similarity for fuzzy matching
        'reject_not_found': False,    # If True, reject tags not in vector text
    }
}


# ISA-5.1 First Letters (Measured Variable)
FIRST_LETTERS = {
    "A": "Analysis", "B": "Burner/Combustion", "C": "Conductivity",
    "D": "Density", "E": "Voltage", "F": "Flow Rate",
    "G": "Gaging/Position", "H": "Hand/Manual", "I": "Current",
    "J": "Power", "K": "Time", "L": "Level",
    "M": "Moisture", "N": "User Choice", "O": "User Choice",
    "P": "Pressure", "Q": "Quantity", "R": "Radiation",
    "S": "Speed", "T": "Temperature", "U": "Multivariable",
    "V": "Vibration", "W": "Weight", "X": "Unclassified",
    "Y": "Event/State", "Z": "Position",
}

# ISA-5.1 Succeeding Letters (Function)
SUCCEEDING_LETTERS = {
    "A": "Alarm", "B": "User Choice", "C": "Control",
    "D": "Differential", "E": "Sensing Element", "G": "Glass/Viewing",
    "H": "High", "I": "Indicate", "K": "Control Station",
    "L": "Low/Light", "M": "Middle", "N": "User Choice",
    "O": "Orifice", "P": "Point/Test", "Q": "Integrate/Totalize",
    "R": "Record", "S": "Switch/Safety", "T": "Transmit",
    "U": "Multifunction", "V": "Valve", "W": "Well",
    "X": "Unclassified", "Y": "Relay/Compute", "Z": "Driver/Actuator",
}

# Equipment code patterns
EQUIPMENT_CODES = {
    "P": "Pump", "TK": "Tank", "V": "Vessel", "R": "Reactor",
    "C": "Compressor", "B": "Blower", "M": "Mixer", "AG": "Agitator",
    "HX": "Heat Exchanger", "F": "Filter", "SC": "Screen",
    "CL": "Clarifier", "TH": "Thickener", "CT": "Cooling Tower",
    "MBR": "Membrane Bioreactor", "RO": "Reverse Osmosis",
    "UF": "Ultrafiltration", "UV": "UV Disinfection",
}

# Tag regex patterns (3-tier system)
# Tier 1: ISA Strict - Area-Letters-Number format
ISA_STRICT_PATTERN = re.compile(r"^(\d{3})-([A-Z]{2,5})-(\d{2,4})([A-Z]?)$")
# Tier 2: ISA-like - No area prefix
ISA_LIKE_PATTERN = re.compile(r"^([A-Z]{2,5})-(\d{1,4})([A-Z]?)$")
# Tier 3: Descriptive equipment tags
DESCRIPTIVE_PATTERN = re.compile(r"^[A-Z][A-Z0-9\-]{3,25}$")
# Line number patterns: various formats used in P&IDs
# Standard: SIZE-SERVICE-SEQ-CLASS (e.g., 2-PW-001-A1, 30-VB-5151-CVPU-DC)
# Project-specific: SERVICE-SEQ-SPEC (e.g., PW-001-A1, GF-61-05-CVPU)
# Note: Must exclude instrument tags (FIT, PG, LT, etc.) and equipment tags (P-01, TK-02)
LINE_NUMBER_PATTERNS = [
    # Standard: SIZE(numeric)-SERVICE-SEQ with additional segments
    re.compile(r"^\d{1,3}-[A-Z]{2,6}-\d{1,4}(-[A-Z0-9]{1,6})+$"),
    # Simpler format: SIZE-SERVICE-SEQ only
    re.compile(r"^\d{1,3}-[A-Z]{2,6}-\d{2,4}$"),
    # With inch symbol: 2"-PW-001-A1
    re.compile(r'^\d{1,3}["\']?-[A-Z]{2,6}-\d{1,4}'),
    # Project format: SERVICE(2-6 letters)-SEQ-CLASS (e.g., PW-001-A1, GF-61-05-CVPU)
    # Must have at least 3 segments with multiple hyphens to distinguish from equipment tags
    re.compile(r"^[A-Z]{2,6}-\d{2,4}(-[A-Z0-9]{1,8})+$"),
]

# Prefixes to exclude from line detection
# These are instrument abbreviations and valve equipment codes
TAG_PREFIXES_NOT_LINES = {
    # ISA first letters (measurement variables)
    'F', 'L', 'P', 'T', 'A', 'S', 'H', 'X', 'Z', 'I', 'C', 'Q', 'N', 'Y', 'U',
    # Common instrument types that look like service codes
    'FI', 'FIT', 'FE', 'FT', 'FC', 'FV', 'FIC', 'FY',  # Flow
    'LI', 'LIT', 'LE', 'LT', 'LC', 'LS', 'LIC', 'LY',  # Level
    'PI', 'PIT', 'PE', 'PT', 'PC', 'PS', 'PIC', 'PG', 'PY',  # Pressure
    'TI', 'TIT', 'TE', 'TT', 'TC', 'TS', 'TIC', 'TY',  # Temperature
    'AI', 'AIT', 'AE', 'AT', 'AC', 'AIC', 'AY',  # Analysis
    'XI', 'XV', 'XS', 'XY',  # On/Off
    'ZI', 'ZT', 'ZS', 'ZIC',  # Position
    'HIC', 'HV', 'HS',  # Hand
    # Valve equipment codes (per valve_catalog.yaml)
    'BV', 'VB', 'VNX', 'BLV',  # Ball valves
    'GV', 'GTV', 'HV',  # Gate valves
    'GLV',  # Globe valves
    'BFV',  # Butterfly valves
    'NRV', 'CV', 'CKV',  # Check/Non-return valves
    'PRV', 'PSV', 'RV',  # Relief valves
    'MOV', 'AOV', 'SOV', 'DV',  # Actuated valves
}

def is_line_number(text: str) -> bool:
    """Check if text matches a line number pattern.

    Line numbers are piping designations, not instrument/equipment tags.
    Distinguishing characteristics:
    - Multiple segments with hyphens (3+ parts)
    - Includes pipe class or material spec
    - Does NOT start with instrument function letters or valve codes
    """
    if not text:
        return False

    # Check for instrument/valve prefixes - these are not line numbers
    first_part = text.split('-')[0].upper()
    if first_part in TAG_PREFIXES_NOT_LINES:
        return False

    # Single-letter equipment codes followed by number are not lines (P-01, B-02)
    if len(first_part) == 1 and first_part.isalpha():
        return False

    return any(p.match(text) for p in LINE_NUMBER_PATTERNS)

# Legacy patterns for compatibility
INSTRUMENT_TAG_PATTERN = re.compile(r"^(\d{3})-([A-Z]+)-(\d+)([A-Z]?)$")
EQUIPMENT_TAG_PATTERN = re.compile(r"^(\d{3})-([A-Z]{1,5})-(\d+)([A-Z]?)$")
LOOP_KEY_PATTERN = re.compile(r"^(\d{3})-([A-Z])-(\d+)$")

# Ambiguous abbreviations that need context disambiguation
AMBIGUOUS_PATTERNS = {
    'TS': {
        'default': ('instrument', 'Temperature Switch', 'T'),
        'context': {
            'keywords': ['TRQ', 'TORQUE', 'N.m', 'N-m', 'Nm', 'N·m'],
            'override': ('instrument', 'Torque Switch', 'M'),
        }
    },
    'SM': {
        'default': ('equipment', 'Static Mixer', 'AG'),
        'context': {
            'keywords': ['SPEED', 'RPM', 'MONITOR'],
            'override': ('instrument', 'Speed Monitor', 'S'),
        }
    },
    'EJ': {
        'default': ('equipment', 'Ejector', 'EJ'),
        'context': {
            'keywords': ['EXPANSION', 'JOINT'],
            'override': ('piping', 'Expansion Joint', None),
        }
    },
    'PS': {
        'default': ('instrument', 'Pressure Switch', 'P'),
        'context': {
            'keywords': ['POSITION', 'LIMIT', 'TRAVEL', 'STROKE'],
            'override': ('instrument', 'Position Switch', 'Z'),
        }
    },
    'LS': {
        'default': ('instrument', 'Level Switch', 'L'),
        'context': {
            'keywords': ['LIMIT', 'POSITION', 'TRAVEL'],
            'override': ('instrument', 'Limit Switch', 'Z'),
        }
    },
}


def decode_instrument_tag(tag: str) -> Optional[dict]:
    """Decode an ISA-5.1 instrument tag into components."""
    match = INSTRUMENT_TAG_PATTERN.match(tag.upper())
    if not match:
        return None

    area, letters, loop_number, suffix = match.groups()

    if len(letters) < 2:
        return None

    variable = letters[0]
    if variable not in FIRST_LETTERS:
        return None

    function = letters[1:]
    functions = list(function)

    # Validate function letters
    for f in functions:
        if f not in SUCCEEDING_LETTERS:
            return None

    loop_key = f"{area}-{variable}-{loop_number.zfill(2)}"

    return {
        "area": area,
        "variable": variable,
        "variable_name": FIRST_LETTERS[variable],
        "function": function,
        "functions": functions,
        "loop_number": loop_number,
        "suffix": suffix,
        "loop_key": loop_key,
        "full_tag": tag.upper(),
        "tag_type": "instrument",
    }


def decode_equipment_tag(tag: str) -> Optional[dict]:
    """Decode an equipment tag into components."""
    match = EQUIPMENT_TAG_PATTERN.match(tag.upper())
    if not match:
        return None

    area, code, seq_number, suffix = match.groups()

    # Check if it looks like an instrument (2+ letter code starting with ISA first letter)
    if len(code) >= 2 and code[0] in FIRST_LETTERS and code[1] in SUCCEEDING_LETTERS:
        return None  # This is an instrument, not equipment

    equipment_type = EQUIPMENT_CODES.get(code, "Unknown")

    return {
        "tag": tag.upper(),
        "area": int(area),
        "code": code,
        "seq_number": seq_number,
        "suffix": suffix,
        "equipment_type": equipment_type,
        "tag_type": "equipment",
    }


def classify_tag(tag: str) -> dict:
    """Classify a tag as instrument, equipment, or unknown."""
    # Try instrument first
    result = decode_instrument_tag(tag)
    if result:
        return result

    # Try equipment
    result = decode_equipment_tag(tag)
    if result:
        return result

    return {
        "tag": tag.upper(),
        "tag_type": "unknown",
        "error": "Could not parse tag format",
    }


def bbox_distance(bbox1: list, bbox2: list) -> float:
    """Calculate minimum distance between two bounding boxes."""
    # bbox format: [x0, y0, x1, y1]
    x1_center = (bbox1[0] + bbox1[2]) / 2
    y1_center = (bbox1[1] + bbox1[3]) / 2
    x2_center = (bbox2[0] + bbox2[2]) / 2
    y2_center = (bbox2[1] + bbox2[3]) / 2

    return ((x2_center - x1_center) ** 2 + (y2_center - y1_center) ** 2) ** 0.5


def find_nearby_equipment(
    instrument: dict,
    equipment_list: list,
    max_distance: float = 150.0
) -> Optional[str]:
    """Find equipment tag near an instrument based on spatial proximity."""
    if "bbox" not in instrument:
        return None

    inst_bbox = instrument["bbox"]
    closest_tag = None
    closest_distance = float("inf")

    for equip in equipment_list:
        if "bbox" not in equip:
            continue

        dist = bbox_distance(inst_bbox, equip["bbox"])
        if dist < max_distance and dist < closest_distance:
            closest_distance = dist
            closest_tag = equip.get("tag")

    return closest_tag


def detect_duplicates(entities: list) -> dict[str, list]:
    """Detect duplicate tags across pages."""
    tag_occurrences = defaultdict(list)

    for entity in entities:
        tag = entity.get("tag") or entity.get("full_tag")
        if tag:
            tag_occurrences[tag.upper()].append({
                "page": entity.get("page"),
                "bbox": entity.get("bbox"),
            })

    # Return only duplicates
    return {tag: locs for tag, locs in tag_occurrences.items() if len(locs) > 1}


def check_page_coverage(
    extraction_results: dict,
    classifications: list,
    skip_pages: Optional[set] = None
) -> dict:
    """
    Check if all P&ID pages have classifications.

    This ensures Claude reviewed ALL pages, not just a sample.
    Pages without classifications result in Gemini flagging items as MISSING,
    which inflates the review queue with false positives.

    Args:
        extraction_results: Contains 'pages' array from vector extraction
        classifications: List of classifications (from Claude)
        skip_pages: Set of page numbers to skip (title pages, legends, etc.)

    Returns:
        {
            'total_pages': int,
            'classified_pages': set,
            'missing_pages': set,
            'coverage_percent': float,
            'is_complete': bool
        }
    """
    skip_pages = skip_pages or set()

    # Get all pages from extraction results
    all_pages = set()
    for page_data in extraction_results.get('pages', []):
        page_num = page_data.get('page', 0)
        if page_num and page_num not in skip_pages:
            all_pages.add(page_num)

    # Get pages that have classifications
    classified_pages = set()
    for c in classifications:
        page = c.get('page')
        if page:
            classified_pages.add(page)

    # Also check discovered_items if present
    for d in extraction_results.get('discovered_items', []):
        page = d.get('page')
        if page:
            classified_pages.add(page)

    # Calculate missing pages
    missing_pages = all_pages - classified_pages

    # Calculate coverage
    total_pages = len(all_pages)
    coverage_percent = (len(classified_pages) / total_pages * 100) if total_pages > 0 else 0

    return {
        'total_pages': total_pages,
        'classified_pages': classified_pages,
        'missing_pages': missing_pages,
        'coverage_percent': round(coverage_percent, 1),
        'is_complete': len(missing_pages) == 0
    }


def determine_device_role(functions: list[str]) -> str:
    """Determine device role from ISA function letters."""
    if "T" in functions:
        return "measurement"
    elif "C" in functions:
        return "control"
    elif "V" in functions:
        return "final_element"
    elif "S" in functions:
        return "switch"
    elif "A" in functions:
        return "alarm"
    elif "I" in functions:
        return "indication"
    return "measurement"  # Default


def group_into_loops(instruments: list) -> list[dict]:
    """Group instruments into control loops by loop_key with devices array."""
    loops = defaultdict(list)

    for inst in instruments:
        loop_key = inst.get("loop_key")
        if loop_key:
            device = {
                "full_tag": inst.get("full_tag"),
                "functions": inst.get("functions", []),
                "role": determine_device_role(inst.get("functions", [])),
            }
            loops[loop_key].append(device)

    return [
        {
            "loop_key": key,
            "tag_area": int(key.split("-")[0]),
            "variable": key.split("-")[1],
            "loop_number": int(key.split("-")[2]),
            "devices": sorted(devices, key=lambda d: d["full_tag"]),
        }
        for key, devices in sorted(loops.items())
    ]


def load_project_config(config_path: Optional[Path]) -> dict:
    """Load project-specific configuration for disambiguation."""
    if not config_path or not config_path.exists():
        return {}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"WARNING: Could not load project config: {e}", file=sys.stderr)
        return {}


def apply_manual_overrides(entities: list, project_config: dict) -> list:
    """Apply manual overrides from project config."""
    overrides = project_config.get('manual_overrides', [])
    if not overrides:
        return entities

    override_map = {o['original_tag']: o['correction'] for o in overrides}

    for entity in entities:
        tag = entity.get('tag') or entity.get('original_tag')
        if tag and tag in override_map:
            correction = override_map[tag]
            entity.update(correction)
            entity['override_applied'] = True
            entity['override_reason'] = correction.get('override_reason', 'Manual QA correction')
            # Clear review flag if override explicitly sets it
            if 'review_required' in correction:
                entity['review_required'] = correction['review_required']

    return entities


def apply_equipment_abbreviations(entities: list, project_config: dict) -> list:
    """
    Apply equipment_abbreviations from project config.
    Forces certain abbreviations to be classified as equipment, not instruments.

    Example: SM (Static Mixer) should be equipment, not Speed Monitor instrument.
    """
    equip_abbrs = set(project_config.get('equipment_abbreviations', []))
    inst_abbrs = set(project_config.get('instrument_abbreviations', []))

    if not equip_abbrs and not inst_abbrs:
        return entities

    for entity in entities:
        tag = entity.get('tag') or entity.get('original_tag', '')
        if not tag:
            continue

        # Extract prefix (2-3 letters before hyphen or digits)
        match = re.match(r'^(\d+-)?([A-Z]{2,3})[-\d]', tag.upper())
        if not match:
            continue

        prefix = match.group(2)

        # Check equipment abbreviations - force to equipment
        if prefix in equip_abbrs:
            current_type = entity.get('tag_type')
            if current_type != 'equipment':
                entity['tag_type'] = 'equipment'
                entity['equipment_code'] = prefix
                entity['abbreviation_override'] = True
                entity['abbreviation_override_reason'] = f"Project config: {prefix} is equipment"
                # Remove instrument-specific fields
                entity.pop('variable', None)
                entity.pop('functions', None)
                entity.pop('loop_key', None)

        # Check instrument abbreviations - force to instrument
        elif prefix in inst_abbrs:
            current_type = entity.get('tag_type')
            if current_type != 'instrument':
                entity['tag_type'] = 'instrument'
                entity['abbreviation_override'] = True
                entity['abbreviation_override_reason'] = f"Project config: {prefix} is instrument"

    return entities


def apply_context_rules(entities: list, project_config: dict, extraction_results: dict = None) -> list:
    """
    Apply context_rules from project config for disambiguation.

    Uses nearby text from extraction results to disambiguate ambiguous tags
    like TS (Temperature vs Torque Switch).
    """
    context_rules = project_config.get('context_rules', [])
    if not context_rules:
        return entities

    # Build context lookup from extraction results if available
    tag_context = {}
    if extraction_results:
        for page_data in extraction_results.get('pages', []):
            for candidate in page_data.get('tag_candidates', []):
                tag = candidate.get('text', '').strip().upper()
                if tag:
                    # Collect nearby text as context
                    context = candidate.get('nearby_text', '')
                    if not context:
                        context = candidate.get('context_text', '')
                    tag_context[tag] = context

        # Also check classifications if using merged format
        for classification in extraction_results.get('classifications', []):
            tag = (classification.get('tag') or classification.get('original_tag', '')).upper()
            if tag:
                context = classification.get('context_text', '')
                if not context:
                    context = classification.get('nearby_text', '')
                if context:
                    tag_context[tag] = context

    for entity in entities:
        tag = entity.get('tag') or entity.get('original_tag', '')
        if not tag:
            continue

        tag_upper = tag.upper()

        for rule in context_rules:
            pattern = rule.get('pattern')
            if not pattern:
                continue

            # Check if tag matches the pattern
            if not re.match(pattern, tag_upper):
                continue

            # Get context for this tag
            context_text = tag_context.get(tag_upper, '')
            if not context_text:
                context_text = entity.get('context_text', '')

            context_check = rule.get('context_check', {})
            nearby_keywords = context_check.get('nearby_text', [])

            # Check if any context keywords are present
            context_matched = False
            matched_keyword = None
            if context_text and nearby_keywords:
                context_upper = context_text.upper()
                for keyword in nearby_keywords:
                    if keyword.upper() in context_upper:
                        context_matched = True
                        matched_keyword = keyword
                        break

            # Apply the appropriate override
            if context_matched:
                override = rule.get('if_found', {})
            else:
                override = rule.get('else', {})

            if override:
                # Apply override fields
                for key, value in override.items():
                    if key != 'review_note':
                        entity[key] = value

                entity['context_rule_applied'] = True
                # Per plan: context_validated bonus applies when ANY disambiguation
                # rule is applied (either if_found or else branch)
                entity['context_validated'] = True
                if context_matched:
                    entity['context_match_keyword'] = matched_keyword
                    entity['context_match_type'] = 'if_found'
                    entity['context_override_reason'] = f"Context '{matched_keyword}' found - {override.get('instrument_type', override.get('equipment_type', 'classified'))}"
                else:
                    entity['context_match_type'] = 'else_default'
                    type_applied = override.get('instrument_type') or override.get('equipment_type') or 'default'
                    entity['context_override_reason'] = f"No context keywords found - using default: {type_applied}"
                    if 'review_note' in rule.get('else', {}):
                        entity['context_override_reason'] = rule['else']['review_note']

                break  # Only apply first matching rule

    return entities


def apply_builtin_ambiguous_patterns(entities: list, extraction_results: dict = None) -> list:
    """
    Apply built-in AMBIGUOUS_PATTERNS as fallback for entities not handled by project config.

    This ensures LS, TS, SM, EJ, PS patterns get proper classification even without
    project-specific context_rules defined.
    """
    # Build context lookup from extraction results if available
    tag_context = {}
    if extraction_results:
        for page_data in extraction_results.get('pages', []):
            for candidate in page_data.get('tag_candidates', []):
                tag = candidate.get('text', '').strip().upper()
                if tag:
                    context = candidate.get('nearby_text', '') or candidate.get('context_text', '')
                    tag_context[tag] = context

        for classification in extraction_results.get('classifications', []):
            tag = (classification.get('tag') or classification.get('original_tag', '')).upper()
            if tag:
                context = classification.get('context_text', '') or classification.get('nearby_text', '')
                if context:
                    tag_context[tag] = context

    for entity in entities:
        # Skip if already handled by project config context rules
        if entity.get('context_rule_applied'):
            continue

        tag = entity.get('tag') or entity.get('original_tag', '')
        if not tag:
            continue

        tag_upper = tag.upper()

        # Extract prefix (2-3 letters)
        match = re.match(r'^(\d+-)?([A-Z]{2,3})[-\d]', tag_upper)
        if not match:
            continue

        prefix = match.group(2)

        # Check if prefix is in AMBIGUOUS_PATTERNS
        if prefix not in AMBIGUOUS_PATTERNS:
            continue

        pattern = AMBIGUOUS_PATTERNS[prefix]

        # Get context for this tag
        context_text = tag_context.get(tag_upper, '')
        if not context_text:
            context_text = entity.get('context_text', '')

        # Check if any context keywords match
        context_matched = False
        matched_keyword = None
        if context_text:
            context_upper = context_text.upper()
            for keyword in pattern['context']['keywords']:
                if keyword in context_upper:
                    context_matched = True
                    matched_keyword = keyword
                    break

        # Apply default or override based on context
        if context_matched:
            tag_type, type_name, variable = pattern['context']['override']
            entity['tag_type'] = tag_type
            if tag_type == 'instrument':
                entity['instrument_type'] = type_name
                if variable:
                    entity['variable'] = variable
            elif tag_type == 'equipment':
                entity['equipment_type'] = type_name
                entity['equipment_code'] = prefix
            entity['context_validated'] = True
            entity['context_match_keyword'] = matched_keyword
            entity['builtin_pattern_applied'] = True
            entity['context_override_reason'] = f"Built-in pattern: '{matched_keyword}' found - {type_name}"
        else:
            # Apply default
            tag_type, type_name, variable = pattern['default']
            entity['tag_type'] = tag_type
            if tag_type == 'instrument':
                entity['instrument_type'] = type_name
                if variable:
                    entity['variable'] = variable
            elif tag_type == 'equipment':
                entity['equipment_type'] = type_name
                entity['equipment_code'] = prefix
            # Still mark as context_validated since default was applied
            entity['context_validated'] = True
            entity['builtin_pattern_applied'] = True
            entity['context_override_reason'] = f"Built-in pattern: no context keywords - using default {type_name}"

    return entities


def check_ambiguous_abbreviation(tag: str, context_text: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """
    Check if a tag prefix is ambiguous and needs context disambiguation.

    Returns:
        (is_ambiguous, override_reason) - If ambiguous and context matches, returns override reason
    """
    # Extract prefix (first 2-3 letters before hyphen)
    match = re.match(r'^(\d+-)?([A-Z]{2,3})-', tag.upper())
    if not match:
        return False, None

    prefix = match.group(2)

    if prefix not in AMBIGUOUS_PATTERNS:
        return False, None

    pattern = AMBIGUOUS_PATTERNS[prefix]

    # Check context if provided
    if context_text:
        context_upper = context_text.upper()
        for keyword in pattern['context']['keywords']:
            if keyword in context_upper:
                override = pattern['context']['override']
                return True, f"Context '{keyword}' found - classified as {override[1]}"

    # Return ambiguous flag without override
    return True, None


# =============================================================================
# TAG EXISTENCE VALIDATION (Vector Text Correlation)
# =============================================================================

def build_vector_tag_set(extraction_results: dict) -> set[str]:
    """
    Build a set of all tags found in vector text extraction.

    This set is used to check if VLM-identified tags have corresponding
    vector text evidence. Tags not in vector text may be:
    - Rendered as graphics (instrument bubbles drawn as shapes)
    - In raster/image overlays within the PDF
    - Missed by vector extraction due to font encoding
    - Using different formatting than the VLM read

    Tags are normalized to uppercase for matching.

    Args:
        extraction_results: Output from vector_extractor.py or merged_results.json

    Returns:
        Set of normalized tag strings from vector extraction
    """
    vector_tags = set()

    # Extract from pages (vector extraction output)
    for page_data in extraction_results.get('pages', []):
        for candidate in page_data.get('tag_candidates', []):
            text = candidate.get('text', '').strip().upper()
            if text:
                vector_tags.add(text)

        # Also include other_text in case tags were mis-classified
        for other in page_data.get('other_text', []):
            text = other.get('text', '').strip().upper()
            if text and len(text) >= 3:  # Minimum tag length
                vector_tags.add(text)

    # Also check extraction_results if present (from vector extractor)
    for page_data in extraction_results.get('extraction_results', {}).get('pages', []):
        for candidate in page_data.get('tag_candidates', []):
            text = candidate.get('text', '').strip().upper()
            if text:
                vector_tags.add(text)

    return vector_tags


def validate_tag_exists_in_vector(
    entity: dict,
    vector_tags: set[str],
    fuzzy_threshold: float = 0.85
) -> tuple[bool, Optional[str], float]:
    """
    Check if a VLM-identified tag has corresponding vector text evidence.

    Tags with vector text correlation have higher confidence since they're
    confirmed by two independent sources (VLM + vector extraction).

    Tags without vector text may still be valid - they could be rendered
    as graphics rather than searchable text in the PDF.

    Args:
        entity: Entity dict containing 'tag' or 'original_tag'
        vector_tags: Set of tags from vector text extraction
        fuzzy_threshold: Minimum similarity for fuzzy matching (0-1)

    Returns:
        (exists, matched_tag, similarity) tuple:
        - exists: True if tag found (exact or fuzzy)
        - matched_tag: The matching tag from vector text (if fuzzy matched)
        - similarity: The similarity score (1.0 for exact match)
    """
    tag = entity.get('tag') or entity.get('original_tag', '')
    if not tag:
        return False, None, 0.0

    tag_upper = tag.strip().upper()

    # Exact match
    if tag_upper in vector_tags:
        return True, tag_upper, 1.0

    # Normalize variations (remove hyphens, spaces for comparison)
    tag_normalized = re.sub(r'[-\s]', '', tag_upper)
    for vector_tag in vector_tags:
        vector_normalized = re.sub(r'[-\s]', '', vector_tag)
        if tag_normalized == vector_normalized:
            return True, vector_tag, 0.99  # Near-exact match

    # Fuzzy match for minor OCR/reconstruction differences
    best_match = None
    best_similarity = 0.0

    for vector_tag in vector_tags:
        # Use SequenceMatcher for similarity
        similarity = difflib.SequenceMatcher(None, tag_upper, vector_tag).ratio()
        if similarity >= fuzzy_threshold and similarity > best_similarity:
            best_similarity = similarity
            best_match = vector_tag

    if best_match:
        return True, best_match, best_similarity

    return False, None, 0.0


def apply_tag_existence_validation(
    entities: list,
    vector_tags: set[str],
    config: dict = VALIDATION_CONFIG
) -> tuple[list, dict]:
    """
    Apply tag existence validation to all entities.

    Args:
        entities: List of classified entities
        vector_tags: Set of tags from vector text extraction
        config: Validation configuration

    Returns:
        (validated_entities, stats) tuple
    """
    tag_config = config.get('tag_existence', {})
    fuzzy_threshold = tag_config.get('fuzzy_threshold', 0.85)
    reject_not_found = tag_config.get('reject_not_found', False)

    stats = {
        'exact_matches': 0,
        'fuzzy_matches': 0,
        'not_found': 0,
        'rejected': 0,
    }

    validated = []

    for entity in entities:
        tag = entity.get('tag') or entity.get('original_tag', '')
        if not tag:
            validated.append(entity)
            continue

        exists, matched_tag, similarity = validate_tag_exists_in_vector(
            entity, vector_tags, fuzzy_threshold
        )

        if exists:
            entity['tag_existence_validated'] = True
            entity['tag_existence_similarity'] = round(similarity, 3)

            if similarity >= 0.99:
                stats['exact_matches'] += 1
            else:
                stats['fuzzy_matches'] += 1
                # Update tag if fuzzy matched to different string
                if matched_tag and matched_tag != tag.upper():
                    entity['tag_vector_match'] = matched_tag
                    entity['tag_fuzzy_corrected'] = True

            validated.append(entity)
        else:
            entity['tag_existence_validated'] = False
            entity['tag_not_in_vector'] = True
            stats['not_found'] += 1

            if reject_not_found:
                # Mark for review (VLM-only identification, no vector corroboration)
                entity['review_required'] = True
                entity['review_reason'] = f"Tag '{tag}' identified by VLM only - no vector text evidence"
                stats['rejected'] += 1

            validated.append(entity)

    return validated, stats


def compute_confidence(entity: dict, config: dict = VALIDATION_CONFIG) -> float:
    """
    Compute confidence score for an entity based on evidence and validation.

    Uses bonuses and penalties from config to calculate final score.

    Bonuses applied:
    - bbox_evidence: Has source bounding box
    - vlm_consensus: Claude + Gemini agree (both reviewed without WRONG)
    - isa_compliant_tag: Matches canonical ISA format
    - context_validated: Nearby context matched disambiguation rules
    - gemini_verified: Gemini reviewed and agreed

    Penalties applied:
    - vlm_disagree: Gemini corrected Claude (WRONG issue)
    - no_bbox: VLM-only discovery without extraction evidence
    - abbreviation_ambiguous: TS, SM, EJ pattern without context resolution
    - missing_required_field: Per missing required field
    - duplicate_tag: Tag appears multiple times
    """
    conf_config = config['confidence']
    score = conf_config['base']
    bonuses = conf_config['bonuses']
    penalties = conf_config['penalties']

    # Check for existing confidence (from VLM)
    existing_conf = entity.get('confidence')
    if isinstance(existing_conf, (int, float)) and 0 < existing_conf <= 1:
        # Use VLM confidence as base, but still apply bonuses/penalties
        score = existing_conf

    # ============================================================
    # BONUSES
    # ============================================================

    # Bbox evidence bonus - has source bounding box from extraction
    if entity.get('bbox') or (entity.get('provenance', {}).get('bbox')):
        score += bonuses['bbox_evidence']

    # Gemini verified bonus - Gemini reviewed and agreed (not WRONG)
    if entity.get('gemini_reviewed') and entity.get('gemini_issue') != 'WRONG':
        score += bonuses['gemini_verified']

    # VLM consensus bonus - both Claude and Gemini agree
    # This applies when Gemini reviewed and found no issues
    gemini_reviewed = entity.get('gemini_reviewed', False)
    gemini_issue = entity.get('gemini_issue')
    if gemini_reviewed and gemini_issue is None:
        # Gemini reviewed and found no discrepancy - consensus achieved
        score += bonuses['vlm_consensus']

    # ISA compliant tag bonus - matches strict ISA format
    tag = entity.get('tag') or entity.get('full_tag') or ''
    if ISA_STRICT_PATTERN.match(tag.upper()):
        score += bonuses['isa_compliant_tag']

    # Context validated bonus - nearby context matched disambiguation rules
    if entity.get('context_validated'):
        score += bonuses['context_validated']

    # Tag existence validated bonus - tag found in vector text extraction
    if entity.get('tag_existence_validated'):
        score += bonuses.get('tag_existence_validated', 0.15)

    # ============================================================
    # PENALTIES
    # ============================================================

    # VLM disagree penalty - Gemini corrected Claude's classification
    if entity.get('gemini_reviewed') and entity.get('gemini_issue') == 'WRONG':
        score += penalties['vlm_disagree']

    # No bbox penalty - VLM-discovered without extraction evidence
    extraction_source = entity.get('extraction_source') or entity.get('provenance', {}).get('source_type')
    if extraction_source == 'vlm_discovered':
        score += penalties['no_bbox']

    # Ambiguous abbreviation penalty (only if NOT context-resolved)
    context_text = entity.get('context_text', '')
    is_ambiguous, _ = check_ambiguous_abbreviation(tag, context_text)
    if is_ambiguous and not entity.get('context_validated'):
        score += penalties['abbreviation_ambiguous']

    # Duplicate tag penalty
    if entity.get('is_duplicate'):
        score += penalties['duplicate_tag']

    # Tag not in vector text penalty - VLM-only identification (less corroboration)
    if entity.get('tag_not_in_vector'):
        score += penalties.get('tag_not_in_vector', -0.30)

    # Missing required field penalties (per field)
    missing_fields = entity.get('missing_fields', [])
    if missing_fields:
        score += penalties['missing_required_field'] * len(missing_fields)

    # Clamp to valid range
    return max(0.0, min(1.0, round(score, 3)))


def determine_device_class(entity: dict) -> Optional[str]:
    """Determine the device class from entity type/functions."""
    tag_type = entity.get('tag_type')
    instrument_type = entity.get('instrument_type', '').lower()
    functions = entity.get('functions', [])

    if tag_type == 'equipment':
        return None  # Equipment doesn't have device classes

    # Map instrument types to device classes
    type_map = {
        'transmitter': 'transmitter',
        'switch': 'switch',
        'gauge': 'gauge',
        'indicator': 'indicator',
        'valve': 'control_valve',
        'control valve': 'control_valve',
        'on-off valve': 'on_off_valve',
        'analyzer': 'analyzer',
    }

    for key, device_class in type_map.items():
        if key in instrument_type:
            return device_class

    # Infer from functions
    if 'T' in functions:
        return 'transmitter'
    if 'V' in functions:
        return 'control_valve'
    if 'S' in functions:
        return 'switch'
    if 'G' in functions:
        return 'gauge'
    if 'I' in functions:
        return 'indicator'

    # Check variable for analyzers
    variable = entity.get('variable', '')
    if variable == 'A':
        return 'analyzer'

    return None


def check_required_fields(entity: dict, config: dict = VALIDATION_CONFIG) -> list[str]:
    """
    Check if required fields are present for the device class.

    Returns list of missing required fields.
    """
    device_class = determine_device_class(entity)
    if not device_class:
        return []

    required = config['required_fields'].get(device_class, [])
    missing = []

    for field in required:
        value = entity.get(field)
        # Also check nested tag structure (only if tag is a dict)
        if not value:
            tag_value = entity.get('tag')
            if isinstance(tag_value, dict):
                value = tag_value.get(field)
        if not value:
            missing.append(field)

    return missing


def validate_cross_references(
    instruments: list,
    equipment_tags: set,
    warnings: list,
    line_numbers: Optional[set] = None,
    valves: Optional[list] = None
) -> None:
    """
    Validate cross-references between entities (per plan document):
    - equipment_tag in instruments must exist in equipment_list
    - line_number in valves must exist in line_list
    - loop_key devices must all exist

    Modifies instruments/valves in place to add warnings.
    """
    line_numbers = line_numbers or set()
    valves = valves or []

    # Build loop_key → devices mapping for completeness check
    loop_devices = defaultdict(list)
    for inst in instruments:
        loop_key = inst.get('loop_key')
        if loop_key:
            tag_info = inst.get('tag', {})
            full_tag = tag_info.get('full_tag') if isinstance(tag_info, dict) else inst.get('tag')
            loop_devices[loop_key].append(full_tag)

    # Validate instrument equipment_tag references
    for inst in instruments:
        equip_tag = inst.get('equipment_tag')
        if equip_tag and equip_tag not in equipment_tags:
            inst['cross_ref_warning'] = f"equipment_tag '{equip_tag}' not in equipment list"
            warnings.append({
                'type': 'invalid_cross_reference',
                'tag': inst.get('tag', {}).get('full_tag') or inst.get('tag'),
                'equipment_tag': equip_tag,
                'message': f"Referenced equipment '{equip_tag}' not found in equipment list",
            })

    # Validate valve line_number references
    for valve in valves:
        line_num = valve.get('line_number')
        if line_num and line_numbers and line_num not in line_numbers:
            valve['cross_ref_warning'] = f"line_number '{line_num}' not in line list"
            warnings.append({
                'type': 'invalid_line_reference',
                'tag': valve.get('valve_tag'),
                'line_number': line_num,
                'message': f"Valve '{valve.get('valve_tag')}' references line '{line_num}' not found in line list",
            })

    # Validate loop_key completeness (warn if loop has only one device)
    for loop_key, devices in loop_devices.items():
        if len(devices) == 1:
            # Single device loop - may be incomplete
            warnings.append({
                'type': 'incomplete_loop',
                'loop_key': loop_key,
                'devices': devices,
                'message': f"Loop '{loop_key}' has only one device ({devices[0]}) - may be incomplete",
            })


def extract_lines(extraction_results: dict, equipment_list: list) -> list:
    """
    Extract line numbers from extraction results.

    Lines are identified by the LINE_NUMBER_PATTERN: Size-Service-Seq-Class
    Example: 2-PW-001-A1 (2" Process Water line, sequence 001, class A1)
    """
    lines = []
    seen_lines = set()

    # Build equipment lookup for from/to linkage
    equipment_tags = {e.get('tag'): e for e in equipment_list}

    # Search in pages for line number candidates
    for page_data in extraction_results.get('pages', []):
        page = page_data.get('page', 0)

        for candidate in page_data.get('tag_candidates', []):
            text = candidate.get('text', '').strip().upper()
            if not text:
                continue

            # Check if matches line number pattern (using flexible patterns)
            if is_line_number(text):
                if text in seen_lines:
                    continue
                seen_lines.add(text)

                # Parse line number components
                # Remove any inch symbol for parsing
                clean_text = text.replace('"', '').replace("'", '')
                parts = clean_text.split('-')

                if len(parts) >= 3:
                    size = parts[0]
                    service_code = parts[1]
                    seq = parts[2] if len(parts) > 2 else ''
                    pipe_class = '-'.join(parts[3:]) if len(parts) > 3 else None

                    line = {
                        'line_number': text,
                        'size_nominal': f'{size}"',
                        'size_inches': float(size) if size.isdigit() else None,
                        'service_code': service_code,
                        'service': _decode_service_code(service_code),
                        'pipe_class': pipe_class,
                        'sequence': seq,
                        'from_equipment': None,  # To be determined by VLM
                        'to_equipment': None,    # To be determined by VLM
                        'page': page,
                        'provenance': {
                            'source_type': 'pdf_extracted',
                            'page': page,
                            'bbox': candidate.get('bbox'),
                            'confidence': 0.85,  # Line numbers are generally reliable
                        }
                    }
                    lines.append(line)

    # Also check classifications for line-related info
    for classification in extraction_results.get('classifications', []):
        # If VLM identified a line
        if classification.get('tag_type') == 'line':
            tag = classification.get('tag', '')
            if tag and tag.upper() not in seen_lines:
                seen_lines.add(tag.upper())
                lines.append({
                    'line_number': tag.upper(),
                    'size_nominal': classification.get('size_nominal'),
                    'service': classification.get('service'),
                    'service_code': classification.get('service_code'),
                    'from_equipment': classification.get('from_equipment'),
                    'to_equipment': classification.get('to_equipment'),
                    'pipe_class': classification.get('pipe_class'),
                    'material': classification.get('material'),
                    'page': classification.get('page'),
                    'provenance': classification.get('provenance', {
                        'source_type': 'vlm_classified',
                        'page': classification.get('page'),
                        'confidence': classification.get('confidence', 0.75),
                    })
                })

    return lines


def _decode_service_code(code: str) -> str:
    """Decode service code to description."""
    service_map = {
        'PW': 'Process Water',
        'RW': 'Raw Water',
        'CW': 'Cooling Water',
        'DW': 'Drinking Water',
        'AIR': 'Air',
        'IA': 'Instrument Air',
        'PA': 'Plant Air',
        'SL': 'Sludge',
        'EFF': 'Effluent',
        'INF': 'Influent',
        'CHEM': 'Chemical',
        'NAOH': 'Sodium Hydroxide',
        'FECL3': 'Ferric Chloride',
        'NAOCL': 'Sodium Hypochlorite',
        'PERM': 'Permeate',
        'CONC': 'Concentrate',
        'RET': 'Retentate',
        'BW': 'Backwash',
        'CIP': 'Clean In Place',
        'N2': 'Nitrogen',
        'STM': 'Steam',
        'COND': 'Condensate',
        'VENT': 'Vent',
        'DRAIN': 'Drain',
    }
    return service_map.get(code.upper(), code)


def extract_valves(instruments: list, lines: list, equipment: list = None) -> list:
    """
    Extract valve schedule from BOTH instruments AND equipment.

    Per valve-schedule.schema.yaml, ALL valves should be included:
    - Control valves (instruments with 'V' in functions): FV, LCV, XV, PCV, TCV
    - Manual/isolation valves (equipment): NRV, BV, VB, VNX, GV, BFV, etc.

    The 'actuator_type' field distinguishes Manual vs Pneumatic/Electric/etc.
    """
    valves = []
    equipment = equipment or []
    line_lookup = {l.get('line_number'): l for l in lines}
    seen_tags = set()

    # Valve equipment codes that should be included in valve schedule (manual valves)
    # Per ~/skills/shared/catalogs/valve_catalog.yaml (house codes)
    VALVE_EQUIPMENT_CODES = {
        # Ball valves
        'BV': ('Ball', 'Ball Valve'),
        'VB': ('Ball', 'Ball Valve'),
        'VNX': ('Ball', 'Isolation Valve'),
        'BLV': ('Ball', 'Ball Valve'),  # House code per valve_catalog.yaml
        # Gate valves
        'GV': ('Gate', 'Gate Valve'),
        'GTV': ('Gate', 'Gate Valve'),  # House code per valve_catalog.yaml
        'HV': ('Gate', 'Hand Valve'),   # Manual hand valve (house code)
        # Globe valves
        'GLV': ('Globe', 'Globe Valve'),
        # Butterfly valves
        'BFV': ('Butterfly', 'Butterfly Valve'),
        # Plug valves
        'PV': ('Plug', 'Plug Valve'),
        'PLV': ('Plug', 'Plug Valve'),  # House code per valve_catalog.yaml
        # Specialty valves
        'KGV': ('Knife Gate', 'Knife Gate Valve'),  # House code per valve_catalog.yaml
        'PNV': ('Pinch', 'Pinch Valve'),            # House code per valve_catalog.yaml
        'DV': ('Diaphragm', 'Diaphragm Valve'),
        'DPV': ('Diaphragm', 'Diaphragm Valve'),    # House code per valve_catalog.yaml
        'NV': ('Needle', 'Needle Valve'),
        # Check valves
        'NRV': ('Check', 'Non-Return Valve'),
        'CV': ('Check', 'Check Valve'),
        'CKV': ('Check', 'Check Valve'),            # ISA convention
        'SCHK': ('Swing Check', 'Swing Check Valve'),   # House code per valve_catalog.yaml
        'LCHK': ('Lift Check', 'Lift Check Valve'),     # House code per valve_catalog.yaml
        'DPCK': ('Dual Plate Check', 'Dual Plate Check Valve'),  # House code
        # Relief/safety valves
        'PRV': ('Relief', 'Pressure Relief Valve'),
        'PSV': ('Safety', 'Pressure Safety Valve'),
        'RV': ('Relief', 'Relief Valve'),
        'SV': ('Safety', 'Safety Valve'),
    }

    # Valve function letter mappings (for control valves)
    # Per ISA 5.1 and ~/skills/shared/catalogs/valve_catalog.yaml
    valve_function_map = {
        'V': 'Control',  # Generic valve
        # Control valves (per variable)
        'FV': 'Flow Control',
        'LV': 'Level Control',      # ISA: Level Valve
        'LCV': 'Level Control',     # Level Control Valve (with C function)
        'PV': 'Pressure Control',
        'PCV': 'Pressure Control',  # Pressure Control Valve (with C function)
        'TV': 'Temperature Control',
        'TCV': 'Temperature Control',  # Temperature Control Valve (with C function)
        # On/off and isolation
        'XV': 'On/Off',
        'HV': 'Hand',
        # Actuated valves
        'MV': 'Motor Operated',     # Motor-operated valve (house code)
        'MOV': 'Motor Operated',
        'SOV': 'Solenoid',
        # Safety/shutdown
        'BDV': 'Blowdown',
        'SDV': 'Shutdown',
        'ESV': 'Emergency Shutdown',
    }

    # === Part 1: Extract control valves from instruments ===
    for inst in instruments:
        tag_info = inst.get('tag', {})
        full_tag = tag_info.get('full_tag', '')
        functions = tag_info.get('functions', [])

        # Check if this is a valve (has V function)
        if 'V' not in functions:
            continue

        if full_tag in seen_tags:
            continue
        seen_tags.add(full_tag)

        # Determine valve classification
        function_str = tag_info.get('function', ''.join(functions))
        variable = tag_info.get('variable', '')

        # Build valve type identifier
        valve_id = variable + 'V' if variable else 'V'
        if function_str.endswith('CV') or 'C' in functions:
            valve_id = variable + 'CV' if variable else 'CV'

        valve_subtype = valve_function_map.get(valve_id, valve_function_map.get(function_str, 'Control'))

        # Determine valve body type from context or default
        body_type = 'Control'  # Default for control valves
        if 'X' in function_str or variable == 'X':
            body_type = 'Ball'  # On/off valves are typically ball
        elif 'H' in function_str or variable == 'H':
            body_type = 'Gate'  # Hand valves often gate

        valve = {
            'valve_tag': full_tag,
            'size_nominal': inst.get('size_nominal'),
            'valve_type': body_type,
            'valve_subtype': valve_subtype,
            'actuator_type': _determine_actuator_type(functions, inst),
            'fail_position': inst.get('fail_position'),
            'normal_position': inst.get('normal_position'),
            'signal_type': inst.get('primary_signal_type'),
            'service_description': inst.get('service_description'),
            'equipment_tag': inst.get('equipment_tag'),
            'loop_key': inst.get('loop_key'),
            'line_number': inst.get('line_number'),
            'page': inst.get('provenance', {}).get('page'),
            'pid_reference': None,
            'provenance': inst.get('provenance', {}),
        }

        if inst.get('review_required'):
            valve['review_required'] = True
            valve['review_reason'] = inst.get('review_reason')

        valves.append(valve)

    # === Part 2: Extract manual/isolation valves from equipment ===
    for equip in equipment:
        # Handle both dict and string tags
        if isinstance(equip, str):
            continue  # Skip malformed entries
        tag = equip.get('tag', '')
        if isinstance(tag, dict):
            tag = tag.get('full_tag', '')
        equip_type = (equip.get('equipment_type') or '').lower()
        equip_code = equip.get('equipment_code', '')

        # Skip non-valve equipment
        if 'valve' not in equip_type and equip_code not in VALVE_EQUIPMENT_CODES:
            continue

        if tag in seen_tags:
            continue
        seen_tags.add(tag)

        # Determine valve type from equipment code
        if equip_code in VALVE_EQUIPMENT_CODES:
            body_type, valve_subtype = VALVE_EQUIPMENT_CODES[equip_code]
        else:
            # Infer from equipment_type string
            body_type = 'Ball'  # Default
            valve_subtype = equip_type.title() if equip_type else 'Manual Valve'
            if 'check' in equip_type or 'non-return' in equip_type:
                body_type = 'Check'
                valve_subtype = 'Non-Return Valve'
            elif 'gate' in equip_type:
                body_type = 'Gate'
                valve_subtype = 'Gate Valve'
            elif 'butterfly' in equip_type:
                body_type = 'Butterfly'
                valve_subtype = 'Butterfly Valve'
            elif 'globe' in equip_type:
                body_type = 'Globe'
                valve_subtype = 'Globe Valve'

        valve = {
            'valve_tag': tag,
            'size_nominal': equip.get('size_nominal'),
            'valve_type': body_type,
            'valve_subtype': valve_subtype,
            'actuator_type': 'Manual',  # Equipment valves are manual
            'fail_position': None,
            'normal_position': equip.get('normal_position'),
            'signal_type': None,  # Manual valves have no signal
            'service_description': equip.get('description'),
            'equipment_tag': None,  # This IS the equipment
            'loop_key': None,
            'line_number': equip.get('line_number'),
            'page': equip.get('provenance', {}).get('page'),
            'pid_reference': None,
            'provenance': equip.get('provenance', {}),
        }

        if equip.get('review_required'):
            valve['review_required'] = True
            valve['review_reason'] = equip.get('review_reason')

        valves.append(valve)

    return valves


def _determine_actuator_type(functions: list, inst: dict) -> str:
    """Determine actuator type from functions and context."""
    # Check explicit actuator_type first
    if inst.get('actuator_type'):
        return inst['actuator_type']

    # Infer from function letters
    if 'H' in functions:
        return 'Manual'
    if 'X' in functions:
        # XV typically solenoid or pneumatic
        return 'Pneumatic'
    if 'C' in functions:
        # Control valves typically pneumatic
        return 'Pneumatic'

    # Check signal type
    signal = inst.get('primary_signal_type', '')
    if '4-20mA' in signal or 'HART' in signal:
        return 'Pneumatic'  # I/P converted
    if '24V' in signal.upper():
        return 'Electric'

    return 'Pneumatic'  # Default for control valves


def build_provenance(entity: dict, page: int, confidence: float) -> dict:
    """
    Build provenance structure with proper fragment nesting.

    If entity has 'fragments' (from clustered multi-line text), they are nested
    under provenance. The merged bbox is used as the primary bbox.

    Structure:
        provenance:
            source_type: pdf_extracted | vlm_discovered | vlm_classified
            page: int
            confidence: float
            bbox: [x1, y1, x2, y2]  # Merged bbox for clustered text
            fragments:              # Only if text was clustered
                - text: "PG"
                  bbox: [...]
                - text: "02"
                  bbox: [...]
            reconstruction_method: clustered | single_text
            extraction_source: vector | vlm_discovered
    """
    # Check if entity already has a well-formed provenance
    existing = entity.get('provenance')
    if existing and isinstance(existing, dict) and 'source_type' in existing:
        # Merge computed confidence if different
        if confidence != existing.get('confidence'):
            existing['confidence'] = confidence
        return existing

    # Build new provenance
    extraction_source = entity.get('extraction_source', 'vector')
    source_type = 'pdf_extracted'
    if extraction_source == 'vlm_discovered':
        source_type = 'vlm_discovered'
    elif extraction_source == 'vlm_classified':
        source_type = 'vlm_classified'

    provenance = {
        'source_type': source_type,
        'page': page,
        'confidence': confidence,
        'bbox': entity.get('bbox') or entity.get('merged_bbox'),
    }

    # Add fragments if text was clustered from multiple elements
    fragments = entity.get('fragments')
    if fragments and isinstance(fragments, list) and len(fragments) > 0:
        provenance['fragments'] = fragments
        provenance['reconstruction_method'] = entity.get('reconstruction_method', 'clustered')
    elif entity.get('reconstruction_method'):
        provenance['reconstruction_method'] = entity['reconstruction_method']

    # Always include extraction_source for downstream processing (per plan)
    provenance['extraction_source'] = extraction_source

    # Include tag existence validation results (hallucination detection)
    if entity.get('tag_existence_validated') is not None:
        provenance['tag_existence_validated'] = entity['tag_existence_validated']
    if entity.get('tag_not_in_vector'):
        provenance['tag_not_in_vector'] = True
    if entity.get('tag_existence_similarity'):
        provenance['tag_existence_similarity'] = entity['tag_existence_similarity']
    if entity.get('tag_vector_match'):
        provenance['tag_vector_match'] = entity['tag_vector_match']

    return provenance


def validate_extraction(
    extraction_results: dict,
    strict: bool = False,
    project_config: Optional[dict] = None,
    config: dict = VALIDATION_CONFIG
) -> dict:
    """
    Validate extraction results and produce structured output.

    Args:
        extraction_results: Output from apply_review.py or vector_extractor.py
        strict: If True, treat warnings as errors
        project_config: Project-specific configuration for disambiguation
        config: Validation configuration (defaults to VALIDATION_CONFIG)

    Returns:
        Validated results with computed confidence scores
    """
    project_config = project_config or {}

    # Detect input format
    is_merged_results = 'classifications' in extraction_results
    is_vlm_merged = extraction_results.get("vlm_merged", False) or is_merged_results

    # Confidence threshold for review flagging
    review_threshold = config.get('review_threshold', 0.80)

    validation = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_file": extraction_results.get("source_file"),
        "project_id": project_config.get("project_id"),
        "vlm_merged": is_vlm_merged,
        "vlm_models_used": [],
        "equipment": [],
        "instruments": [],
        "loops": [],
        "review_queue": [],  # VLM-discovered items needing human review
        "unknown_entities": [],  # Evidence-gated unknowns for review
        "errors": [],
        "warnings": [],
        "statistics": {
            "total_tags_processed": 0,
            "equipment_count": 0,
            "instrument_count": 0,
            "loop_count": 0,
            "unknown_tags": 0,
            "duplicates_found": 0,
            "review_required": 0,
            "high_confidence_count": 0,
            "gemini_corrections": 0,
            "manual_overrides": 0,
        }
    }

    all_entities = []
    equipment_by_page = defaultdict(list)
    equipment_tags = set()  # For cross-reference validation
    instruments = []

    # Handle merged_results format from apply_review.py
    if is_merged_results:
        classifications = extraction_results.get('classifications', [])
        review_queue_input = extraction_results.get('review_queue', [])

        # Track VLM models used
        merge_meta = extraction_results.get('merge_metadata', {})
        if merge_meta.get('gemini_corrections_applied', 0) > 0:
            validation['vlm_models_used'] = ['claude', 'gemini']
        else:
            validation['vlm_models_used'] = ['claude']

        # Apply project config overrides in order:
        # 1. Equipment abbreviations (SM=Static Mixer, EJ=Ejector)
        # 2. Project config context rules (TS near "TRQ" = Torque Switch)
        # 3. Built-in ambiguous patterns as fallback (LS, TS, SM, EJ, PS)
        # 4. Manual overrides (explicit QA corrections - highest priority)
        classifications = apply_equipment_abbreviations(classifications, project_config)
        classifications = apply_context_rules(classifications, project_config, extraction_results)
        classifications = apply_builtin_ambiguous_patterns(classifications, extraction_results)
        classifications = apply_manual_overrides(classifications, project_config)

        # 5. Tag existence validation (hallucination prevention)
        # Build vector tag set from extraction results
        if config['checks'].get('tag_existence', True):
            vector_tags = build_vector_tag_set(extraction_results)
            if vector_tags:
                classifications, tag_existence_stats = apply_tag_existence_validation(
                    classifications, vector_tags, config
                )
                validation['statistics']['tag_existence'] = tag_existence_stats
                validation['statistics']['vector_tags_count'] = len(vector_tags)
            else:
                validation['warnings'].append({
                    'type': 'no_vector_tags',
                    'message': 'No vector tags found for existence validation - skipping hallucination check',
                })

        # Count overrides applied
        validation['statistics']['manual_overrides'] = sum(
            1 for c in classifications if c.get('override_applied')
        )
        validation['statistics']['abbreviation_overrides'] = sum(
            1 for c in classifications if c.get('abbreviation_override')
        )
        validation['statistics']['context_rules_applied'] = sum(
            1 for c in classifications if c.get('context_rule_applied')
        )
        validation['statistics']['builtin_patterns_applied'] = sum(
            1 for c in classifications if c.get('builtin_pattern_applied')
        )

        # Check page coverage - ensure all P&ID pages were classified
        # Get skip_pages from project config (title pages, legends, etc.)
        skip_pages = set(project_config.get('skip_pages', []))
        coverage = check_page_coverage(extraction_results, classifications, skip_pages)

        validation['statistics']['page_coverage'] = {
            'total_pages': coverage['total_pages'],
            'classified_pages': len(coverage['classified_pages']),
            'missing_pages': sorted(coverage['missing_pages']),
            'coverage_percent': coverage['coverage_percent'],
        }

        if not coverage['is_complete']:
            missing_list = sorted(coverage['missing_pages'])
            validation['warnings'].append({
                'type': 'incomplete_page_coverage',
                'missing_pages': missing_list,
                'coverage_percent': coverage['coverage_percent'],
                'message': f"WARNING: {len(missing_list)} pages not classified by Claude. "
                           f"Pages {missing_list} have no classifications. "
                           f"This may result in inflated review queue from Gemini MISSING flags.",
            })
            print(f"WARNING: Page coverage incomplete ({coverage['coverage_percent']}%)", file=sys.stderr)
            print(f"  Missing pages: {missing_list}", file=sys.stderr)

        # Process classifications directly
        for entity in classifications:
            tag = entity.get('tag') or entity.get('original_tag')
            if not tag:
                continue

            validation['statistics']['total_tags_processed'] += 1
            tag_type = entity.get('tag_type', 'unknown')
            page = entity.get('page', 0)

            # Compute confidence score
            confidence = compute_confidence(entity, config)
            entity['computed_confidence'] = confidence

            # Check if review required
            needs_review = entity.get('review_required', False)
            if confidence < review_threshold:
                needs_review = True

            # Check required fields
            missing_fields = check_required_fields(entity, config)
            if missing_fields:
                needs_review = True
                entity['missing_fields'] = missing_fields

            # Track Gemini corrections
            if entity.get('gemini_issue') == 'WRONG':
                validation['statistics']['gemini_corrections'] += 1

            if tag_type == 'equipment':
                equipment = {
                    'tag': tag,
                    'description': entity.get('description'),
                    'area': entity.get('area'),
                    'equipment_type': entity.get('equipment_type'),
                    'equipment_code': entity.get('equipment_code'),
                    'process_unit_type': entity.get('process_unit_type'),
                    'kind': 'equipment',
                    'feeder_type': entity.get('feeder_type'),
                    'provenance': build_provenance(entity, page, confidence),
                }

                if needs_review:
                    equipment['review_required'] = True
                    equipment['review_reason'] = entity.get('review_reason', 'Low confidence or missing fields')
                    validation['statistics']['review_required'] += 1
                else:
                    validation['statistics']['high_confidence_count'] += 1

                validation['equipment'].append(equipment)
                equipment_tags.add(tag)
                equipment_by_page[page].append(entity)
                validation['statistics']['equipment_count'] += 1

            elif tag_type == 'instrument':
                # Decode tag if not already decoded
                decoded = entity.get('tag_decoded') or decode_instrument_tag(tag)
                if not decoded:
                    # Try to build from entity fields
                    decoded = {
                        'area': entity.get('area', ''),
                        'variable': entity.get('variable', ''),
                        'function': ''.join(entity.get('functions', [])),
                        'functions': entity.get('functions', []),
                        'loop_number': entity.get('loop_number', ''),
                        'suffix': entity.get('suffix', ''),
                        'full_tag': tag,
                        'loop_key': entity.get('loop_key', ''),
                    }

                instrument = {
                    'instrument_id': entity.get('id') or str(uuid.uuid4()),
                    'loop_key': decoded.get('loop_key') or entity.get('loop_key'),
                    'tag': {
                        'area': decoded.get('area'),
                        'variable': decoded.get('variable') or entity.get('variable'),
                        'function': decoded.get('function'),
                        'functions': decoded.get('functions') or entity.get('functions', []),
                        'loop_number': decoded.get('loop_number'),
                        'suffix': decoded.get('suffix'),
                        'full_tag': tag,
                        'analyte': entity.get('analyte'),
                    },
                    'equipment_tag': entity.get('equipment_tag') or entity.get('nearby_equipment'),
                    'service_description': entity.get('service_description'),
                    'instrument_type': entity.get('instrument_type'),
                    'primary_signal_type': entity.get('primary_signal_type'),
                    'provenance': build_provenance(entity, page, confidence),
                    '_bbox': entity.get('bbox'),
                    '_page': page,
                    '_functions': decoded.get('functions') or entity.get('functions', []),
                }

                if needs_review:
                    instrument['review_required'] = True
                    instrument['review_reason'] = entity.get('review_reason', 'Low confidence or missing fields')
                    validation['statistics']['review_required'] += 1
                else:
                    validation['statistics']['high_confidence_count'] += 1

                instruments.append(instrument)
                validation['statistics']['instrument_count'] += 1

            elif tag_type in ('unknown', 'noise'):
                validation['statistics']['unknown_tags'] += 1
                validation['unknown_entities'].append({
                    'tag': tag,
                    'page': page,
                    'tag_type': tag_type,
                    'review_required': True,
                    'review_reason': entity.get('review_reason', f'Classified as {tag_type}'),
                    'provenance': entity.get('provenance'),
                })

            all_entities.append(entity)

        # Add review_queue items (VLM-discovered)
        for item in review_queue_input:
            validation['review_queue'].append(item)
            validation['statistics']['review_required'] += 1

    else:
        # Legacy format: process pages with tag_candidates
        # Process all pages
        for page_data in extraction_results.get("pages", []):
            page_num = page_data.get("page", 0)

            for candidate in page_data.get("tag_candidates", []):
                text = candidate.get("text", "").strip()
                if not text:
                    continue

                validation["statistics"]["total_tags_processed"] += 1

                # Evidence gating: check VLM tag_type first
                vlm_tag_type = candidate.get("vlm_tag_type")
                vlm_classification = candidate.get("vlm_classification")

                # Treat missing VLM classification as "unknown" to enforce gating
                if vlm_classification is None and is_vlm_merged:
                    vlm_tag_type = "unknown"

                if vlm_tag_type in ("unknown", "noise"):
                    validation["statistics"]["unknown_tags"] += 1
                    validation["statistics"]["review_required"] += 1

                    unknown_entity = {
                        "tag": text,
                        "page": page_num,
                        "vlm_tag_type": vlm_tag_type,
                        "review_required": True,
                        "review_reason": candidate.get("review_reason", f"VLM classified as {vlm_tag_type}"),
                        "provenance": {
                            "source_type": "pdf_extracted",
                            "page": page_num,
                            "confidence": candidate.get("vlm_confidence", 0.0),
                            "bbox": candidate.get("bbox"),
                        }
                    }
                    validation["unknown_entities"].append(unknown_entity)

                    validation["warnings"].append({
                        "type": "vlm_evidence_gated",
                        "tag": text,
                        "page": page_num,
                        "vlm_tag_type": vlm_tag_type,
                        "message": f"VLM classified as {vlm_tag_type}: {candidate.get('review_reason', 'no evidence')}",
                    })
                    continue  # Skip equipment/instrument processing

                classified = classify_tag(text)
                classified["bbox"] = candidate.get("bbox")
                classified["page"] = page_num
                classified["font_size"] = candidate.get("font_size")

                if classified.get("tag_type") == "equipment":
                    vlm_conf = candidate.get("vlm_confidence", 0.85)
                    needs_review = candidate.get("review_required", False)

                    description = candidate.get("vlm_description") or None
                    process_unit_type = candidate.get("vlm_process_unit_type") or None
                    feeder_type = candidate.get("vlm_feeder_type") or None

                    if not description or not process_unit_type or not feeder_type:
                        needs_review = True

                    if vlm_conf < review_threshold:
                        needs_review = True

                    equipment = {
                        "tag": classified["tag"],
                        "description": description,
                        "area": classified["area"],
                        "process_unit_type": process_unit_type,
                        "kind": "equipment",
                        "feeder_type": feeder_type,
                        "provenance": {
                            "source_type": "pdf_extracted",
                            "page": page_num,
                            "confidence": round(vlm_conf, 2),
                            "bbox": classified.get("bbox"),
                        }
                    }

                    if needs_review:
                        equipment["review_required"] = True
                        equipment["review_reason"] = candidate.get(
                            "review_reason",
                            "Missing required fields or low confidence"
                        )
                        validation["statistics"]["review_required"] += 1
                    else:
                        validation["statistics"]["high_confidence_count"] += 1

                    validation["equipment"].append(equipment)
                    equipment_tags.add(classified["tag"])
                    equipment_by_page[page_num].append(classified)
                    validation["statistics"]["equipment_count"] += 1

                elif classified.get("tag_type") == "instrument":
                    vlm_conf = candidate.get("vlm_confidence", 0.85)
                    needs_review = candidate.get("review_required", False)

                    service_description = candidate.get("vlm_service_description") or None
                    primary_signal_type = candidate.get("vlm_primary_signal_type") or None
                    analyte = candidate.get("vlm_analyte") or None

                    if not service_description or not primary_signal_type:
                        needs_review = True

                    if vlm_conf < review_threshold:
                        needs_review = True

                    instrument = {
                        "instrument_id": str(uuid.uuid4()),
                        "loop_key": classified["loop_key"],
                        "tag": {
                            "area": classified["area"],
                            "variable": classified["variable"],
                            "function": classified["function"],
                            "functions": classified["functions"],
                            "loop_number": classified["loop_number"],
                            "suffix": classified.get("suffix") or None,
                            "full_tag": classified["full_tag"],
                            "analyte": analyte,
                        },
                        "equipment_tag": None,
                        "service_description": service_description,
                        "primary_signal_type": primary_signal_type,
                        "provenance": {
                            "source_type": "pdf_extracted",
                            "page": page_num,
                            "confidence": round(vlm_conf, 2),
                            "bbox": classified.get("bbox"),
                        },
                        "_bbox": classified.get("bbox"),
                        "_page": page_num,
                        "_functions": classified["functions"],
                    }

                    if needs_review:
                        instrument["review_required"] = True
                        instrument["review_reason"] = candidate.get(
                            "review_reason",
                            "Missing required fields or low confidence"
                        )
                        validation["statistics"]["review_required"] += 1
                    else:
                        validation["statistics"]["high_confidence_count"] += 1

                    instruments.append(instrument)
                    validation["statistics"]["instrument_count"] += 1

                else:
                    validation["statistics"]["unknown_tags"] += 1
                    validation["warnings"].append({
                        "type": "unknown_tag",
                        "tag": text,
                        "page": page_num,
                        "message": f"Could not parse tag format: {text}",
                    })

                all_entities.append(classified)

    # Link instruments to nearby equipment
    for inst in instruments:
        page = inst.get("_page")
        page_equipment = equipment_by_page.get(page, [])

        if page_equipment and inst.get("_bbox"):
            nearby_equip = find_nearby_equipment(
                {"bbox": inst["_bbox"]},
                page_equipment
            )
            if nearby_equip:
                inst["equipment_tag"] = nearby_equip

        # Hard validation: flag for review if no equipment linkage found
        if inst.get("equipment_tag") is None:
            if not inst.get("review_required"):
                inst["review_required"] = True
                inst["review_reason"] = "No nearby equipment found for linkage"
                validation["statistics"]["review_required"] += 1

        # Remove internal fields
        inst.pop("_bbox", None)
        inst.pop("_page", None)
        inst.pop("_functions", None)

    validation["instruments"] = instruments

    # Group into loops with devices array (per loop.schema.yaml)
    loop_data = group_into_loops([
        {
            "loop_key": i["loop_key"],
            "full_tag": i["tag"]["full_tag"],
            "functions": i["tag"]["functions"],
        }
        for i in instruments
    ])
    validation["loops"] = loop_data
    validation["statistics"]["loop_count"] = len(loop_data)

    # Extract lines from extraction results
    lines = extract_lines(extraction_results, validation["equipment"])
    validation["lines"] = lines
    validation["statistics"]["line_count"] = len(lines)

    # Extract valve schedule from instruments AND equipment (includes manual valves)
    valves = extract_valves(instruments, lines, validation["equipment"])
    validation["valves"] = valves
    validation["statistics"]["valve_count"] = len(valves)

    # Cross-reference validation (per plan document)
    # - equipment_tag in instruments must exist in equipment_list
    # - line_number in valves must exist in line_list
    # - loop_key devices must all exist
    if config['checks'].get('cross_reference', True):
        line_numbers = {l.get('line_number') for l in lines if l.get('line_number')}
        validate_cross_references(
            instruments,
            equipment_tags,
            validation["warnings"],
            line_numbers=line_numbers,
            valves=valves
        )

    # Detect and flag duplicates
    duplicates = detect_duplicates(all_entities)
    if duplicates:
        validation["statistics"]["duplicates_found"] = len(duplicates)
        for tag, locations in duplicates.items():
            # Flag the duplicate entities
            for entity in all_entities:
                entity_tag = entity.get("tag") or entity.get("full_tag")
                if entity_tag and entity_tag.upper() == tag:
                    entity["is_duplicate"] = True
                    if not entity.get("review_required"):
                        entity["review_required"] = True
                        entity["review_reason"] = f"Duplicate tag found on pages {[l['page'] for l in locations]}"
                        validation["statistics"]["review_required"] += 1

            validation["warnings"].append({
                "type": "duplicate_tag",
                "tag": tag,
                "locations": locations,
                "message": f"Tag {tag} appears {len(locations)} times - review for disambiguation",
            })

    # Check for raster pages (require manual review)
    for page_data in extraction_results.get("pages", []):
        if page_data.get("pdf_type") == "raster":
            validation["warnings"].append({
                "type": "raster_page",
                "page": page_data.get("page"),
                "message": "Raster page detected - may require OCR for complete extraction",
            })

    # Summary statistics
    validation["statistics"]["total_entities"] = (
        validation["statistics"]["equipment_count"] +
        validation["statistics"]["instrument_count"]
    )

    # Strict mode: convert warnings to errors
    if strict and validation["warnings"]:
        validation["errors"].extend(validation["warnings"])
        validation["warnings"] = []

    return validation


def output_yaml(validation: dict, output_path: Path):
    """Write validation results as YAML artifacts."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    paths = {}

    # Valve equipment codes that should go to valve-schedule.yaml, NOT equipment-list.yaml
    # Manual valves are NOT major equipment - they belong in valve schedule only
    VALVE_EQUIPMENT_CODES_FOR_FILTERING = {
        'NRV', 'BV', 'VB', 'VNX', 'BLV', 'GV', 'GTV', 'HV', 'GLV', 'BFV',
        'PV', 'PLV', 'KGV', 'PNV', 'DV', 'DPV', 'NV', 'CV', 'CKV',
        'SCHK', 'LCHK', 'DPCK', 'PRV', 'PSV', 'RV', 'SV',
    }

    # Equipment list - filter out valves (they go to valve-schedule.yaml)
    filtered_equipment = [
        equip for equip in validation["equipment"]
        if equip.get("equipment_code") not in VALVE_EQUIPMENT_CODES_FOR_FILTERING
        and 'valve' not in (equip.get("equipment_type") or "").lower()
    ]

    equipment_output = {
        "project_id": validation.get("project_id"),
        "equipment": filtered_equipment,
        "provenance": {
            "source": validation.get("source_file"),
            "extracted": validation.get("timestamp"),
            "vlm_models": validation.get("vlm_models_used", []),
        }
    }

    equipment_path = output_path.parent / "equipment-list.yaml"
    with open(equipment_path, "w", encoding="utf-8") as f:
        yaml.dump(equipment_output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    paths["equipment_path"] = str(equipment_path)

    # Instrument database (with loops embedded per schema)
    instrument_output = {
        "project_id": validation.get("project_id"),
        "revision": {
            "number": "A",
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "by": "CLAUDE",
            "description": "Initial extraction from PDF P&ID",
        },
        "source_pids": [{
            "pid_number": Path(validation.get("source_file") or "unknown").stem,
            "source_type": "pdf_extracted",
            "extraction_date": validation.get("timestamp"),
            "vlm_models": validation.get("vlm_models_used", []),
        }],
        "loops": validation["loops"],
        "instruments": validation["instruments"],
    }

    instrument_path = output_path.parent / "instrument-database.yaml"
    with open(instrument_path, "w", encoding="utf-8") as f:
        yaml.dump(instrument_output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    paths["instrument_path"] = str(instrument_path)

    # Line list (per line-list.schema.yaml)
    lines = validation.get("lines", [])
    if lines:
        line_output = {
            "project_id": validation.get("project_id"),
            "revision": {
                "number": "A",
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "by": "CLAUDE",
                "description": "Initial extraction from PDF P&ID",
            },
            "source_pids": [{
                "pid_number": Path(validation.get("source_file", "")).stem,
                "source_type": "pdf_extracted",
                "extraction_date": validation.get("timestamp"),
            }],
            "lines": lines,
        }

        line_list_path = output_path.parent / "line-list.yaml"
        with open(line_list_path, "w", encoding="utf-8") as f:
            yaml.dump(line_output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        paths["line_list_path"] = str(line_list_path)

    # Valve schedule (per valve-schedule.schema.yaml)
    valves = validation.get("valves", [])
    if valves:
        valve_output = {
            "project_id": validation.get("project_id"),
            "revision": {
                "number": "A",
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "by": "CLAUDE",
                "description": "Initial extraction from PDF P&ID",
            },
            "source_pids": [{
                "pid_number": Path(validation.get("source_file", "")).stem,
                "source_type": "pdf_extracted",
                "extraction_date": validation.get("timestamp"),
            }],
            "valves": valves,
        }

        valve_schedule_path = output_path.parent / "valve-schedule.yaml"
        with open(valve_schedule_path, "w", encoding="utf-8") as f:
            yaml.dump(valve_output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        paths["valve_schedule_path"] = str(valve_schedule_path)

    # Review queue (items needing human verification)
    review_queue = validation.get("review_queue", [])
    if review_queue:
        review_output = {
            "project_id": validation.get("project_id"),
            "review_queue": review_queue,
            "count": len(review_queue),
            "generated": validation.get("timestamp"),
        }

        review_queue_path = output_path.parent / "review-queue.yaml"
        with open(review_queue_path, "w", encoding="utf-8") as f:
            yaml.dump(review_output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        paths["review_queue_path"] = str(review_queue_path)

    # Validation report
    report = {
        "project_id": validation.get("project_id"),
        "report_date": validation["timestamp"],
        "source_file": validation.get("source_file"),
        "summary": {
            "total_pages": len(set(
                e.get("provenance", {}).get("page") or e.get("page", 0)
                for e in validation["equipment"] + validation["instruments"]
            )),
            "equipment_count": validation["statistics"]["equipment_count"],
            "instrument_count": validation["statistics"]["instrument_count"],
            "loop_count": validation["statistics"]["loop_count"],
            "vlm_models_used": validation.get("vlm_models_used", []),
            "consensus_items": validation["statistics"].get("gemini_corrections", 0),
            "review_required_count": validation["statistics"]["review_required"],
            "high_confidence_count": validation["statistics"].get("high_confidence_count", 0),
            "page_coverage": validation["statistics"].get("page_coverage"),
        },
        "process_areas": sorted(set(
            str(e.get("area", ""))
            for e in validation["equipment"]
            if e.get("area")
        )),
        "equipment_by_type": {},
        "instruments_by_variable": {},
    }

    # Count equipment by type
    for equip in validation["equipment"]:
        etype = equip.get("equipment_type") or "Unknown"
        report["equipment_by_type"][etype] = report["equipment_by_type"].get(etype, 0) + 1

    # Count instruments by variable
    for inst in validation["instruments"]:
        var = inst.get("tag", {}).get("variable") or "Unknown"
        report["instruments_by_variable"][var] = report["instruments_by_variable"].get(var, 0) + 1

    report_path = output_path.parent / "validation-report.yaml"
    with open(report_path, "w", encoding="utf-8") as f:
        yaml.dump(report, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    paths["report_path"] = str(report_path)

    return paths


def main():
    parser = argparse.ArgumentParser(
        description="Validate PID extraction results"
    )
    parser.add_argument(
        "input_json",
        type=Path,
        help="Path to merged_results.json or extraction_results.json"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=None,
        help="Output directory (default: same as input file)"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Project config YAML for disambiguation rules"
    )
    parser.add_argument(
        "--strict", "-s",
        action="store_true",
        help="Strict mode - treat warnings as errors"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output validation results as JSON"
    )
    args = parser.parse_args()

    if not args.input_json.exists():
        print(f"Error: File not found: {args.input_json}", file=sys.stderr)
        sys.exit(1)

    # Default output dir to input file's directory
    output_dir = args.output_dir or args.input_json.parent

    # Load project config if provided
    project_config = {}
    if args.config:
        project_config = load_project_config(args.config)
        if project_config:
            print(f"Loaded project config: {args.config}", file=sys.stderr)

    with open(args.input_json, encoding='utf-8') as f:
        extraction_results = json.load(f)

    validation = validate_extraction(
        extraction_results,
        strict=args.strict,
        project_config=project_config
    )

    if args.json:
        print(json.dumps(validation, indent=2, ensure_ascii=False))
    else:
        # Output YAML artifacts
        output_path = output_dir / "validation.yaml"
        paths = output_yaml(validation, output_path)

        stats = validation['statistics']
        print("Validation complete:")
        print(f"  Equipment: {stats['equipment_count']}")
        print(f"  Instruments: {stats['instrument_count']}")
        print(f"  Loops: {stats['loop_count']}")
        print(f"  Lines: {stats.get('line_count', 0)}")
        print(f"  Valves: {stats.get('valve_count', 0)}")
        print(f"  High confidence: {stats.get('high_confidence_count', 0)}")
        print(f"  Review required: {stats['review_required']}")
        if stats.get('gemini_corrections', 0) > 0:
            print(f"  Gemini corrections: {stats['gemini_corrections']}")
        if stats.get('abbreviation_overrides', 0) > 0:
            print(f"  Abbreviation overrides: {stats['abbreviation_overrides']}")
        if stats.get('context_rules_applied', 0) > 0:
            print(f"  Context rules applied: {stats['context_rules_applied']}")
        if stats.get('manual_overrides', 0) > 0:
            print(f"  Manual overrides: {stats['manual_overrides']}")
        if stats.get('duplicates_found', 0) > 0:
            print(f"  Duplicates: {stats['duplicates_found']}")

        # Tag existence validation info
        tag_existence = stats.get('tag_existence', {})
        if tag_existence:
            exact = tag_existence.get('exact_matches', 0)
            fuzzy = tag_existence.get('fuzzy_matches', 0)
            not_found = tag_existence.get('not_found', 0)
            total_validated = exact + fuzzy + not_found
            if total_validated > 0:
                print(f"\n  Tag existence validation:")
                print(f"    Vector tags available: {stats.get('vector_tags_count', 0)}")
                print(f"    Exact matches: {exact}")
                print(f"    Fuzzy matches: {fuzzy}")
                if not_found > 0:
                    print(f"    Not in vector text: {not_found} (VLM-only, no vector evidence)")

        # Page coverage info
        page_coverage = stats.get('page_coverage', {})
        if page_coverage:
            coverage_pct = page_coverage.get('coverage_percent', 0)
            total = page_coverage.get('total_pages', 0)
            classified = page_coverage.get('classified_pages', 0)
            missing = page_coverage.get('missing_pages', [])
            if coverage_pct < 100:
                print(f"\n⚠️  Page coverage: {classified}/{total} pages ({coverage_pct}%)")
                print(f"  Missing pages: {missing}")
            else:
                print(f"  Page coverage: {classified}/{total} pages (100%)")

        if validation["errors"]:
            print(f"\nErrors: {len(validation['errors'])}")
            for err in validation["errors"][:5]:
                print(f"  - {err['message']}")

        if validation["warnings"]:
            print(f"\nWarnings: {len(validation['warnings'])}")
            for warn in validation["warnings"][:5]:
                print(f"  - {warn['message']}")

        print(f"\nOutput files:")
        for name, path in paths.items():
            print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
