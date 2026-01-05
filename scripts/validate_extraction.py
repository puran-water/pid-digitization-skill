#!/usr/bin/env python3
"""
Validation for PID Extraction Results.

Stage 3 of the PID digitization pipeline:
- Validates ISA tag formats
- Checks equipment-instrument linkage via spatial proximity
- Detects cross-page duplicates
- Groups instruments into loops by tag prefix
- Applies VLM classification data (from merge_vlm_classifications.py)
- Flags low-confidence items for review

Usage:
    # Without VLM merge (base extraction only)
    python validate_extraction.py extraction_results.json --output-dir ./validated/

    # With VLM merge (recommended)
    python validate_extraction.py merged_results.json --output-dir ./validated/

    python validate_extraction.py extraction_results.json --strict
"""

import argparse
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("Error: pyyaml required. Install with: pip install pyyaml")
    sys.exit(1)


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

# Tag regex patterns
INSTRUMENT_TAG_PATTERN = re.compile(r"^(\d{3})-([A-Z]+)-(\d+)([A-Z]?)$")
EQUIPMENT_TAG_PATTERN = re.compile(r"^(\d{3})-([A-Z]{1,5})-(\d+)([A-Z]?)$")
LOOP_KEY_PATTERN = re.compile(r"^(\d{3})-([A-Z])-(\d+)$")


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


def validate_extraction(extraction_results: dict, strict: bool = False) -> dict:
    """
    Validate extraction results and produce structured output.

    Args:
        extraction_results: Output from vector_extractor.py
        strict: If True, treat warnings as errors

    Returns:
        Validated results with confidence scores
    """
    # Check if this is VLM-merged input
    is_vlm_merged = extraction_results.get("vlm_merged", False)

    # Confidence threshold for review flagging
    REVIEW_THRESHOLD = 0.80

    validation = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source_file": extraction_results.get("source_file"),
        "vlm_merged": is_vlm_merged,
        "equipment": [],
        "instruments": [],
        "loops": [],
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
        }
    }

    all_entities = []
    equipment_by_page = defaultdict(list)
    instruments = []

    # Process all pages
    for page_data in extraction_results.get("pages", []):
        page_num = page_data.get("page", 0)

        for candidate in page_data.get("tag_candidates", []):
            text = candidate.get("text", "").strip()
            if not text:
                continue

            validation["statistics"]["total_tags_processed"] += 1

            # Evidence gating: check VLM tag_type first
            # If VLM classified as unknown/noise OR no VLM classification, gate it
            vlm_tag_type = candidate.get("vlm_tag_type")
            vlm_classification = candidate.get("vlm_classification")

            # Treat missing VLM classification as "unknown" to enforce gating
            if vlm_classification is None and is_vlm_merged:
                vlm_tag_type = "unknown"

            if vlm_tag_type in ("unknown", "noise"):
                validation["statistics"]["unknown_tags"] += 1
                validation["statistics"]["review_required"] += 1

                # Emit to unknown_entities for review (not just warnings)
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
                # Get VLM classification data if available
                vlm_conf = candidate.get("vlm_confidence", 0.85)
                needs_review = candidate.get("review_required", False)

                # Use VLM data or None (not empty strings)
                description = candidate.get("vlm_description") or None
                process_unit_type = candidate.get("vlm_process_unit_type") or None
                feeder_type = candidate.get("vlm_feeder_type") or None

                # Flag for review if VLM didn't provide required fields
                if not description or not process_unit_type or not feeder_type:
                    needs_review = True

                if vlm_conf < REVIEW_THRESHOLD:
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

                validation["equipment"].append(equipment)
                equipment_by_page[page_num].append(classified)
                validation["statistics"]["equipment_count"] += 1

            elif classified.get("tag_type") == "instrument":
                # Get VLM classification data if available
                vlm_conf = candidate.get("vlm_confidence", 0.85)
                needs_review = candidate.get("review_required", False)

                # Use VLM data or None (not empty strings)
                service_description = candidate.get("vlm_service_description") or None
                primary_signal_type = candidate.get("vlm_primary_signal_type") or None
                analyte = candidate.get("vlm_analyte") or None

                # Flag for review if VLM didn't provide required fields
                if not service_description or not primary_signal_type:
                    needs_review = True

                if vlm_conf < REVIEW_THRESHOLD:
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
                    "equipment_tag": None,  # Will be linked below
                    "service_description": service_description,
                    "primary_signal_type": primary_signal_type,
                    "provenance": {
                        "source_type": "pdf_extracted",
                        "page": page_num,
                        "confidence": round(vlm_conf, 2),
                        "bbox": classified.get("bbox"),
                    },
                    # Internal fields for linking
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

    # Detect duplicates
    duplicates = detect_duplicates(all_entities)
    if duplicates:
        validation["statistics"]["duplicates_found"] = len(duplicates)
        for tag, locations in duplicates.items():
            validation["warnings"].append({
                "type": "duplicate_tag",
                "tag": tag,
                "locations": locations,
                "message": f"Tag {tag} appears on multiple pages",
            })

    # Check for raster pages (require manual review)
    for page_data in extraction_results.get("pages", []):
        if page_data.get("pdf_type") == "raster":
            validation["warnings"].append({
                "type": "raster_page",
                "page": page_data.get("page"),
                "message": "Raster page detected - may require OCR for complete extraction",
            })

    # Strict mode: convert warnings to errors
    if strict and validation["warnings"]:
        validation["errors"].extend(validation["warnings"])
        validation["warnings"] = []

    return validation


def output_yaml(validation: dict, output_path: Path):
    """Write validation results as YAML artifacts."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Equipment list
    equipment_output = {
        "equipment": validation["equipment"],
        "provenance": {
            "source": validation.get("source_file"),
            "extracted": validation.get("timestamp"),
        }
    }

    equipment_path = output_path.parent / "equipment-list.yaml"
    with open(equipment_path, "w") as f:
        yaml.dump(equipment_output, f, default_flow_style=False, sort_keys=False)

    # Instrument database (with loops embedded per schema)
    instrument_output = {
        "project_id": None,  # To be filled by user
        "revision": {
            "number": "A",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "by": "CLAUDE",
            "description": "Initial extraction from PDF P&ID",
        },
        "source_pids": [{
            "pid_number": Path(validation.get("source_file", "")).stem,
            "source_type": "pdf_extracted",
            "extraction_date": validation.get("timestamp"),
        }],
        "loops": validation["loops"],
        "instruments": validation["instruments"],
    }

    instrument_path = output_path.parent / "instrument-database.yaml"
    with open(instrument_path, "w") as f:
        yaml.dump(instrument_output, f, default_flow_style=False, sort_keys=False)

    # Validation report
    report = {
        "validation": {
            "timestamp": validation["timestamp"],
            "source_file": validation.get("source_file"),
            "statistics": validation["statistics"],
            "unknown_entities": validation["unknown_entities"],  # For human review
            "errors": validation["errors"],
            "warnings": validation["warnings"],
        }
    }

    report_path = output_path.parent / "validation-report.yaml"
    with open(report_path, "w") as f:
        yaml.dump(report, f, default_flow_style=False, sort_keys=False)

    return {
        "equipment_path": str(equipment_path),
        "instrument_path": str(instrument_path),
        "report_path": str(report_path),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Validate PID extraction results"
    )
    parser.add_argument(
        "input_json",
        type=Path,
        help="Path to extraction_results.json from vector_extractor.py"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("./validated"),
        help="Output directory (default: ./validated)"
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
        print(f"Error: File not found: {args.input_json}")
        sys.exit(1)

    with open(args.input_json) as f:
        extraction_results = json.load(f)

    validation = validate_extraction(extraction_results, strict=args.strict)

    if args.json:
        print(json.dumps(validation, indent=2))
    else:
        # Output YAML artifacts
        output_path = args.output_dir / "validation.yaml"
        paths = output_yaml(validation, output_path)

        print("Validation complete:")
        print(f"  Equipment: {validation['statistics']['equipment_count']}")
        print(f"  Instruments: {validation['statistics']['instrument_count']}")
        print(f"  Loops: {validation['statistics']['loop_count']}")
        print(f"  Unknown tags: {validation['statistics']['unknown_tags']}")

        if validation["errors"]:
            print(f"\n  Errors: {len(validation['errors'])}")
            for err in validation["errors"][:5]:
                print(f"    - {err['message']}")

        if validation["warnings"]:
            print(f"\n  Warnings: {len(validation['warnings'])}")
            for warn in validation["warnings"][:5]:
                print(f"    - {warn['message']}")

        print(f"\nOutput files:")
        for name, path in paths.items():
            print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
