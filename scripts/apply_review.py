#!/usr/bin/env python3
"""
Apply Gemini's review corrections to Claude's P&ID classifications.

This script implements the "Gemini as Reviewer" workflow where Gemini
reviews Claude's work and reports discrepancies (WRONG, MISSING, CORRECTED).

Usage:
    python apply_review.py classifications_claude.json gemini_review.json \
        --output merged_results.json

Input Files:
    - classifications_claude.json: Claude's primary classifications
    - gemini_review.json: Gemini's review with discrepancies array

Output:
    - merged_results.json: Claude's classifications with Gemini's corrections applied
    - Includes review_queue for MISSING items that need human verification
"""

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import uuid


def load_json(path: Path) -> dict:
    """Load JSON file with error handling."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)


def find_item_by_tag(items: list, tag: str) -> tuple[Optional[dict], int]:
    """
    Find an item by tag in a list of classifications.
    Returns (item, index) or (None, -1) if not found.
    """
    for i, item in enumerate(items):
        # Check various tag field names
        item_tag = item.get('tag') or item.get('original_tag') or item.get('full_tag')
        if item_tag == tag:
            return item, i
    return None, -1


def apply_discrepancy(item: dict, discrepancy: dict) -> dict:
    """
    Apply a discrepancy correction to an item.
    Returns the updated item.
    """
    issue = discrepancy.get('issue', 'UNKNOWN')
    # Use 'or {}' to handle both missing key and null value
    correction = discrepancy.get('correction') or {}
    evidence = discrepancy.get('evidence', '')
    confidence = discrepancy.get('confidence', 'medium')

    # Map confidence string to numeric penalty
    confidence_penalty = {
        'high': 0.15,
        'medium': 0.20,
        'low': 0.25
    }.get(confidence, 0.20)

    if issue == 'WRONG':
        # Update with correction, flag for review
        item.update(correction)
        item['review_required'] = True
        item['review_reason'] = f"Gemini correction: {evidence}"

        # Apply confidence penalty
        current_confidence = item.get('confidence', 0.8)
        if isinstance(current_confidence, str):
            current_confidence = {'high': 0.9, 'medium': 0.7, 'low': 0.5}.get(current_confidence, 0.7)
        item['confidence'] = max(0.3, current_confidence - confidence_penalty)
        item['gemini_reviewed'] = True
        item['gemini_issue'] = 'WRONG'

    elif issue == 'CORRECTED':
        # Minor correction, no review flag needed
        item.update(correction)
        item['gemini_reviewed'] = True
        item['gemini_issue'] = 'CORRECTED'
        item['correction_note'] = evidence

    return item


def create_missing_item(discrepancy: dict, page: int) -> dict:
    """
    Create a new item from a MISSING discrepancy.
    These go to review_queue for human verification.
    """
    # Use 'or {}' to handle both missing key and null value
    correction = discrepancy.get('correction') or {}
    evidence = discrepancy.get('evidence', '')
    confidence = discrepancy.get('confidence', 'medium')

    # Map confidence string to numeric value (lower for VLM-only discoveries)
    confidence_value = {
        'high': 0.65,
        'medium': 0.55,
        'low': 0.45
    }.get(confidence, 0.55)

    item = {
        'tag': discrepancy.get('tag'),
        'page': page,
        **correction,
        'review_required': True,
        'review_reason': f"Gemini-discovered (no vector evidence): {evidence}",
        'confidence': confidence_value,
        'extraction_source': 'vlm_discovered',
        'gemini_reviewed': True,
        'gemini_issue': 'MISSING',
        'provenance': {
            'source_type': 'vlm_discovered',
            'discovered_by': 'gemini',
            'evidence': evidence,
            'page': page
        }
    }

    # Generate unique ID
    item['id'] = str(uuid.uuid4())

    return item


def apply_gemini_review(claude_data: dict, gemini_review: dict) -> dict:
    """
    Apply Gemini's review corrections to Claude's classifications.

    Args:
        claude_data: Claude's primary classifications (list or dict with 'classifications' key)
        gemini_review: Gemini's review with discrepancies (single page or list of pages)

    Returns:
        Merged results with corrections applied and review_queue for MISSING items
    """
    # Normalize input format
    discovered_items = []
    if isinstance(claude_data, list):
        classifications = claude_data
    elif isinstance(claude_data, dict):
        classifications = claude_data.get('classifications', claude_data.get('items', []))
        # Extract VLM-discovered items (lines, valves, instruments not in candidates)
        discovered_items = claude_data.get('discovered_items', [])
        if not classifications and 'pages' in claude_data:
            # Flatten page-based structure
            classifications = []
            for page_data in claude_data.get('pages', []):
                page_items = page_data.get('classifications', page_data.get('items', []))
                for item in page_items:
                    item['page'] = page_data.get('page', 0)
                classifications.extend(page_items)
                # Also collect discovered items per page
                page_discovered = page_data.get('discovered_items', [])
                for item in page_discovered:
                    item['page'] = page_data.get('page', 0)
                discovered_items.extend(page_discovered)
    else:
        classifications = []

    # Deep copy to avoid mutating input
    merged = deepcopy(classifications)
    review_queue = []
    applied_corrections = []

    # Process Claude's discovered items - add to review queue
    for item in discovered_items:
        tag = item.get('tag', '')
        tag_type = item.get('tag_type', 'unknown')

        # Map confidence string to numeric value (lower for VLM-only discoveries)
        confidence = item.get('confidence', 'medium')
        confidence_value = {
            'high': 0.65,
            'medium': 0.55,
            'low': 0.45
        }.get(confidence, 0.55) if isinstance(confidence, str) else confidence

        discovered_item = {
            **item,
            'review_required': True,
            'review_reason': f"Claude-discovered (not in vector extraction): {item.get('evidence', '')}",
            'confidence': confidence_value,
            'extraction_source': 'vlm_discovered',
            'discovered_by': 'claude',
            'provenance': {
                'source_type': 'vlm_discovered',
                'discovered_by': 'claude',
                'evidence': item.get('evidence', ''),
                'page': item.get('page', 0)
            },
            'id': str(uuid.uuid4())
        }
        review_queue.append(discovered_item)

    # Normalize Gemini review format
    if isinstance(gemini_review, list):
        reviews = gemini_review
    elif isinstance(gemini_review, dict):
        if 'discrepancies' in gemini_review:
            # Single page review
            reviews = [gemini_review]
        elif 'pages' in gemini_review:
            reviews = gemini_review.get('pages', [])
        else:
            reviews = [gemini_review]
    else:
        reviews = []

    # Track which pages were reviewed and which tags had discrepancies
    reviewed_pages = set()
    tags_with_discrepancies = set()

    # Process each page's review
    for review in reviews:
        page = review.get('page', 0)
        reviewed_pages.add(page)
        discrepancies = review.get('discrepancies', [])

        for discrepancy in discrepancies:
            tag = discrepancy.get('tag')
            issue = discrepancy.get('issue')

            if not tag or not issue:
                continue

            tags_with_discrepancies.add(tag.upper())

            if issue in ('WRONG', 'CORRECTED'):
                # Find and update existing item
                item, idx = find_item_by_tag(merged, tag)
                if item:
                    merged[idx] = apply_discrepancy(item, discrepancy)
                    applied_corrections.append({
                        'tag': tag,
                        'issue': issue,
                        'page': page
                    })
                else:
                    # Tag not found in Claude's list - treat as MISSING
                    print(f"WARNING: {issue} discrepancy for '{tag}' but not in Claude's list. "
                          f"Adding to review queue.", file=sys.stderr)
                    review_queue.append(create_missing_item(discrepancy, page))

            elif issue == 'MISSING':
                # Add to review queue (not directly to output)
                review_queue.append(create_missing_item(discrepancy, page))
                applied_corrections.append({
                    'tag': tag,
                    'issue': issue,
                    'page': page
                })

            elif issue == 'MISSING_LINE':
                # Discovered line number - add to review queue with tag_type='line'
                line_item = create_missing_item(discrepancy, page)
                line_item['tag_type'] = 'line'
                review_queue.append(line_item)
                applied_corrections.append({
                    'tag': tag,
                    'issue': issue,
                    'page': page
                })

            elif issue == 'MISSING_VALVE_SYMBOL':
                # Discovered valve symbol - add to review queue with tag_type='valve'
                valve_item = create_missing_item(discrepancy, page)
                valve_item['tag_type'] = 'valve'
                review_queue.append(valve_item)
                applied_corrections.append({
                    'tag': tag,
                    'issue': issue,
                    'page': page
                })

    # Mark items on reviewed pages as gemini_reviewed=True for VLM consensus bonus
    # Items WITHOUT discrepancies get gemini_reviewed=True but NO gemini_issue,
    # triggering the vlm_consensus bonus in compute_confidence()
    verified_count = 0
    for item in merged:
        item_page = item.get('page', 0)
        if item_page in reviewed_pages:
            item_tag = item.get('tag') or item.get('original_tag') or item.get('full_tag') or ''
            if item_tag.upper() not in tags_with_discrepancies:
                # Gemini reviewed this page and found no issue with this item
                if not item.get('gemini_reviewed'):
                    item['gemini_reviewed'] = True
                    # Note: NO gemini_issue field = consensus achieved
                    verified_count += 1

    # Count discovered items by source
    claude_discovered_count = sum(1 for item in review_queue if item.get('discovered_by') == 'claude')
    gemini_discovered_count = sum(1 for item in review_queue if item.get('discovered_by') == 'gemini')

    # Build result structure
    result = {
        'merge_metadata': {
            'merge_date': datetime.now().isoformat(),
            'claude_count': len(classifications),
            'claude_discovered': claude_discovered_count,
            'gemini_corrections_applied': len(applied_corrections),
            'gemini_verified_correct': verified_count,
            'gemini_discovered': gemini_discovered_count,
            'review_queue_count': len(review_queue),
            'pages_reviewed': sorted(reviewed_pages),
            'corrections': applied_corrections
        },
        'classifications': merged,
        'review_queue': review_queue
    }

    return result


def compute_statistics(result: dict) -> dict:
    """Compute summary statistics for the merged result."""
    classifications = result.get('classifications', [])
    review_queue = result.get('review_queue', [])

    # Count by type
    equipment_count = sum(1 for c in classifications if c.get('tag_type') == 'equipment')
    instrument_count = sum(1 for c in classifications if c.get('tag_type') == 'instrument')
    unknown_count = sum(1 for c in classifications if c.get('tag_type') in ('unknown', 'noise', None))

    # Count review required
    review_required_count = sum(1 for c in classifications if c.get('review_required'))

    # Count by Gemini issue type
    wrong_count = sum(1 for c in classifications if c.get('gemini_issue') == 'WRONG')
    corrected_count = sum(1 for c in classifications if c.get('gemini_issue') == 'CORRECTED')
    # Count items Gemini verified correct (gemini_reviewed=True but NO gemini_issue)
    verified_count = sum(1 for c in classifications
                         if c.get('gemini_reviewed') and not c.get('gemini_issue'))

    # Confidence distribution - handle both numeric and string confidence values
    # String values: "high" -> 0.9, "medium" -> 0.7, "low" -> 0.5
    confidence_map = {'high': 0.9, 'medium': 0.7, 'low': 0.5}
    confidences = []
    for c in classifications:
        conf = c.get('confidence')
        if isinstance(conf, (int, float)):
            confidences.append(conf)
        elif isinstance(conf, str) and conf.lower() in confidence_map:
            confidences.append(confidence_map[conf.lower()])
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0

    # Count discovered items by source and type
    claude_discovered = sum(1 for q in review_queue if q.get('discovered_by') == 'claude')
    gemini_discovered = sum(1 for q in review_queue if q.get('discovered_by') == 'gemini')
    lines_discovered = sum(1 for q in review_queue if q.get('tag_type') == 'line')
    valves_discovered = sum(1 for q in review_queue if q.get('tag_type') == 'valve')
    instruments_discovered = sum(1 for q in review_queue
                                 if q.get('tag_type') == 'instrument'
                                 and q.get('extraction_source') == 'vlm_discovered')

    return {
        'total_classifications': len(classifications),
        'equipment_count': equipment_count,
        'instrument_count': instrument_count,
        'unknown_count': unknown_count,
        'review_required_count': review_required_count,
        'review_queue_count': len(review_queue),
        'gemini_wrong_corrections': wrong_count,
        'gemini_minor_corrections': corrected_count,
        'gemini_verified_correct': verified_count,
        'gemini_missing_found': gemini_discovered,
        'claude_discovered': claude_discovered,
        'discovered_lines': lines_discovered,
        'discovered_valves': valves_discovered,
        'discovered_instruments': instruments_discovered,
        'average_confidence': round(avg_confidence, 3)
    }


def main():
    parser = argparse.ArgumentParser(
        description="Apply Gemini's review corrections to Claude's P&ID classifications."
    )
    parser.add_argument(
        'claude_file',
        type=Path,
        help="Claude's classifications JSON file"
    )
    parser.add_argument(
        'gemini_file',
        type=Path,
        help="Gemini's review JSON file with discrepancies"
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        default=Path('merged_results.json'),
        help="Output merged results file (default: merged_results.json)"
    )
    parser.add_argument(
        '--review-queue', '-r',
        type=Path,
        help="Separate output file for review queue items (optional)"
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help="Print statistics to stderr"
    )
    parser.add_argument(
        '--extraction', '-e',
        type=Path,
        help="Raw extraction_results.json to carry forward pages[] for line extraction"
    )

    args = parser.parse_args()

    # Load input files
    print(f"Loading Claude classifications from: {args.claude_file}", file=sys.stderr)
    claude_data = load_json(args.claude_file)

    print(f"Loading Gemini review from: {args.gemini_file}", file=sys.stderr)
    gemini_review = load_json(args.gemini_file)

    # Apply review
    result = apply_gemini_review(claude_data, gemini_review)

    # Load and carry forward extraction data (pages[], summary, etc.) for validation
    extraction_data = None
    if args.extraction and args.extraction.exists():
        print(f"Loading extraction results from: {args.extraction}", file=sys.stderr)
        extraction_data = load_json(args.extraction)
    elif args.extraction:
        print(f"WARNING: Extraction file not found: {args.extraction}", file=sys.stderr)

    # Carry forward extraction data fields for downstream validation (extract_lines, etc.)
    if extraction_data:
        # These fields enable extract_lines() and other validation functions
        result['pages'] = extraction_data.get('pages', [])
        result['summary'] = extraction_data.get('summary', {})
        result['source_file'] = extraction_data.get('source_file')
        result['source_path'] = extraction_data.get('source_path')
        result['output_dir'] = extraction_data.get('output_dir')
        print(f"  Carried forward {len(result['pages'])} pages with tag candidates", file=sys.stderr)
    else:
        # Try to extract from claude_data if it has these fields
        if 'pages' in claude_data:
            result['pages'] = claude_data.get('pages', [])
        if 'source_file' in claude_data:
            result['source_file'] = claude_data.get('source_file')
        if 'source_path' in claude_data:
            result['source_path'] = claude_data.get('source_path')
        if 'output_dir' in claude_data:
            result['output_dir'] = claude_data.get('output_dir')

    # Add statistics
    result['statistics'] = compute_statistics(result)

    # Write main output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote merged results to: {args.output}", file=sys.stderr)

    # Optionally write review queue separately
    if args.review_queue and result['review_queue']:
        with open(args.review_queue, 'w', encoding='utf-8') as f:
            json.dump({
                'review_queue': result['review_queue'],
                'count': len(result['review_queue'])
            }, f, indent=2, ensure_ascii=False)
        print(f"Wrote review queue to: {args.review_queue}", file=sys.stderr)

    # Print statistics
    if args.stats:
        stats = result['statistics']
        print("\n=== Merge Statistics ===", file=sys.stderr)
        print(f"Total classifications: {stats['total_classifications']}", file=sys.stderr)
        print(f"  Equipment: {stats['equipment_count']}", file=sys.stderr)
        print(f"  Instruments: {stats['instrument_count']}", file=sys.stderr)
        print(f"  Unknown/Noise: {stats['unknown_count']}", file=sys.stderr)
        print(f"\nGemini review results:", file=sys.stderr)
        print(f"  Verified correct (consensus): {stats['gemini_verified_correct']}", file=sys.stderr)
        print(f"  WRONG (major fixes): {stats['gemini_wrong_corrections']}", file=sys.stderr)
        print(f"  CORRECTED (minor): {stats['gemini_minor_corrections']}", file=sys.stderr)
        print(f"  MISSING (discovered): {stats['gemini_missing_found']}", file=sys.stderr)
        print(f"\nReview required: {stats['review_required_count']}", file=sys.stderr)
        print(f"Average confidence: {stats['average_confidence']:.1%}", file=sys.stderr)

    # Return counts for scripting
    return 0


if __name__ == '__main__':
    sys.exit(main())
