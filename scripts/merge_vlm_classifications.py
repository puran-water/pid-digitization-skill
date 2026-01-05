#!/usr/bin/env python3
"""
Merge VLM Classifications into Extraction Results.

Stage 2b of the PID digitization pipeline:
- Ingests VLM classification JSON (from Claude native or Gemini CLI)
- Merges classifications into extracted tag candidates
- Applies evidence gating (no evidence → unknown + review_required)
- Handles multi-model consensus (Claude + Gemini)
- Flags disagreements and low confidence for human review

Usage:
    # Single model (Claude or Gemini)
    python merge_vlm_classifications.py extraction_results.json \
        --claude classifications_claude.json \
        --output merged_results.json

    # Multi-model consensus
    python merge_vlm_classifications.py extraction_results.json \
        --claude classifications_claude.json \
        --gemini classifications_gemini.json \
        --output merged_results.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


# Confidence thresholds
HIGH_CONFIDENCE_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.80
LOW_CONFIDENCE_VALUE = 0.50
MEDIUM_CONFIDENCE_VALUE = 0.75
HIGH_CONFIDENCE_VALUE = 0.92


def confidence_to_score(confidence_str: str) -> float:
    """Convert VLM confidence string to numeric score."""
    mapping = {
        "high": HIGH_CONFIDENCE_VALUE,
        "medium": MEDIUM_CONFIDENCE_VALUE,
        "low": LOW_CONFIDENCE_VALUE,
    }
    return mapping.get(confidence_str.lower(), LOW_CONFIDENCE_VALUE)


def merge_single_model(
    extraction_results: dict,
    classifications: list[dict]
) -> dict:
    """Merge classifications from a single VLM model."""
    # Index classifications by tag
    class_by_tag = {c["tag"].upper(): c for c in classifications if "tag" in c}

    merged = extraction_results.copy()
    merged["vlm_merged"] = True
    merged["models_used"] = []

    for page_data in merged.get("pages", []):
        for candidate in page_data.get("tag_candidates", []):
            tag = candidate.get("text", "").strip().upper()
            if not tag:
                continue

            classification = class_by_tag.get(tag)

            if classification:
                # Merge VLM classification into candidate
                candidate["vlm_classification"] = classification
                candidate["vlm_tag_type"] = classification.get("tag_type")
                candidate["vlm_confidence"] = confidence_to_score(
                    classification.get("confidence", "low")
                )

                # Copy enriched fields
                for field in ["equipment_type", "description", "feeder_type",
                              "process_unit_type", "instrument_type",
                              "service_description", "primary_signal_type",
                              "analyte", "reason"]:
                    if field in classification:
                        candidate[f"vlm_{field}"] = classification[field]

                # Evidence gating: noise/unknown types
                if classification.get("tag_type") in ["noise", "unknown"]:
                    candidate["review_required"] = True
                    candidate["review_reason"] = classification.get(
                        "reason", "VLM classified as unknown/noise"
                    )

                # Low confidence flagging
                if candidate["vlm_confidence"] < REVIEW_THRESHOLD:
                    candidate["review_required"] = True
                    candidate["review_reason"] = candidate.get(
                        "review_reason", f"Low confidence: {candidate['vlm_confidence']:.2f}"
                    )
            else:
                # No VLM classification - evidence gating
                candidate["vlm_classification"] = None
                candidate["vlm_confidence"] = 0.0
                candidate["review_required"] = True
                candidate["review_reason"] = "No VLM classification provided"

    return merged


def apply_consensus(
    merged_claude: dict,
    classifications_gemini: list[dict]
) -> dict:
    """Apply multi-model consensus between Claude and Gemini."""
    # Index Gemini classifications by tag
    gemini_by_tag = {
        c["tag"].upper(): c for c in classifications_gemini if "tag" in c
    }

    merged = merged_claude.copy()
    merged["models_used"].append("gemini")

    for page_data in merged.get("pages", []):
        for candidate in page_data.get("tag_candidates", []):
            tag = candidate.get("text", "").strip().upper()
            if not tag:
                continue

            gemini_class = gemini_by_tag.get(tag)
            claude_class = candidate.get("vlm_classification")

            if gemini_class and claude_class:
                # Both models provided classification - check consensus
                candidate["gemini_classification"] = gemini_class

                claude_type = claude_class.get("tag_type")
                gemini_type = gemini_class.get("tag_type")

                if claude_type == gemini_type:
                    # Agreement - boost confidence
                    candidate["consensus"] = "agree"
                    candidate["vlm_confidence"] = min(
                        candidate.get("vlm_confidence", 0.5) + 0.1,
                        0.98
                    )
                else:
                    # Disagreement - flag for review
                    candidate["consensus"] = "disagree"
                    candidate["review_required"] = True
                    candidate["review_reason"] = (
                        f"Model disagreement: Claude={claude_type}, Gemini={gemini_type}"
                    )
                    # Use lower confidence on disagreement
                    gemini_conf = confidence_to_score(
                        gemini_class.get("confidence", "low")
                    )
                    candidate["vlm_confidence"] = min(
                        candidate.get("vlm_confidence", 0.5),
                        gemini_conf
                    ) - 0.1

            elif gemini_class and not claude_class:
                # Only Gemini classified - use Gemini
                candidate["gemini_classification"] = gemini_class
                candidate["vlm_classification"] = gemini_class
                candidate["vlm_tag_type"] = gemini_class.get("tag_type")
                candidate["vlm_confidence"] = confidence_to_score(
                    gemini_class.get("confidence", "low")
                )
                candidate["consensus"] = "gemini_only"

    return merged


def merge_classifications(
    extraction_results: dict,
    claude_classifications: Optional[list[dict]] = None,
    gemini_classifications: Optional[list[dict]] = None,
) -> dict:
    """
    Merge VLM classifications into extraction results.

    Args:
        extraction_results: Output from vector_extractor.py
        claude_classifications: VLM output from Claude (optional)
        gemini_classifications: VLM output from Gemini CLI (optional)

    Returns:
        Merged extraction results with VLM classifications
    """
    if not claude_classifications and not gemini_classifications:
        # No VLM classifications - mark all for review
        merged = extraction_results.copy()
        merged["vlm_merged"] = False
        merged["models_used"] = []

        for page_data in merged.get("pages", []):
            for candidate in page_data.get("tag_candidates", []):
                candidate["review_required"] = True
                candidate["review_reason"] = "No VLM classification available"

        return merged

    # Start with Claude (primary) or Gemini (if Claude not provided)
    primary_classifications = claude_classifications or gemini_classifications
    primary_model = "claude" if claude_classifications else "gemini"

    merged = merge_single_model(extraction_results, primary_classifications)
    merged["models_used"] = [primary_model]

    # Apply Gemini consensus if both provided
    if claude_classifications and gemini_classifications:
        merged = apply_consensus(merged, gemini_classifications)

    # Calculate summary statistics
    total_candidates = 0
    review_required_count = 0
    high_confidence_count = 0
    low_confidence_count = 0

    for page_data in merged.get("pages", []):
        for candidate in page_data.get("tag_candidates", []):
            total_candidates += 1
            if candidate.get("review_required"):
                review_required_count += 1
            conf = candidate.get("vlm_confidence", 0)
            if conf >= HIGH_CONFIDENCE_THRESHOLD:
                high_confidence_count += 1
            elif conf < REVIEW_THRESHOLD:
                low_confidence_count += 1

    merged["vlm_summary"] = {
        "total_candidates": total_candidates,
        "review_required": review_required_count,
        "high_confidence": high_confidence_count,
        "low_confidence": low_confidence_count,
        "models_used": merged["models_used"],
    }

    return merged


def load_json(path: Path) -> Optional[list[dict]]:
    """Load JSON file, handling both array and object formats."""
    if not path.exists():
        return None

    with open(path) as f:
        data = json.load(f)

    # Handle both array format and object with classifications key
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        return data.get("classifications", data.get("entities", []))

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Merge VLM classifications into extraction results"
    )
    parser.add_argument(
        "extraction_json",
        type=Path,
        help="Path to extraction_results.json from vector_extractor.py"
    )
    parser.add_argument(
        "--claude", "-c",
        type=Path,
        help="Path to Claude VLM classification JSON"
    )
    parser.add_argument(
        "--gemini", "-g",
        type=Path,
        help="Path to Gemini CLI classification JSON"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("merged_results.json"),
        help="Output path for merged results (default: merged_results.json)"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output merged results as JSON to stdout"
    )
    args = parser.parse_args()

    if not args.extraction_json.exists():
        print(f"Error: File not found: {args.extraction_json}")
        sys.exit(1)

    if not args.claude and not args.gemini:
        print("Error: At least one of --claude or --gemini must be provided")
        sys.exit(1)

    # Load extraction results
    with open(args.extraction_json) as f:
        extraction_results = json.load(f)

    # Load VLM classifications
    claude_classifications = load_json(args.claude) if args.claude else None
    gemini_classifications = load_json(args.gemini) if args.gemini else None

    if args.claude and not claude_classifications:
        print(f"Warning: Could not load Claude classifications from {args.claude}")
    if args.gemini and not gemini_classifications:
        print(f"Warning: Could not load Gemini classifications from {args.gemini}")

    # Merge
    merged = merge_classifications(
        extraction_results,
        claude_classifications,
        gemini_classifications
    )

    # Output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(merged, f, indent=2)

    if args.json:
        print(json.dumps(merged, indent=2))
    else:
        summary = merged.get("vlm_summary", {})
        print("VLM merge complete:")
        print(f"  Models used: {', '.join(summary.get('models_used', []))}")
        print(f"  Total candidates: {summary.get('total_candidates', 0)}")
        print(f"  High confidence: {summary.get('high_confidence', 0)}")
        print(f"  Low confidence: {summary.get('low_confidence', 0)}")
        print(f"  Review required: {summary.get('review_required', 0)}")
        print(f"  Output: {args.output}")


if __name__ == "__main__":
    main()
