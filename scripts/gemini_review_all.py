#!/usr/bin/env python3
"""Run Gemini review on all pages of a P&ID classification.

This script:
1. Reads classifications_claude.json
2. For each page image, sends the relevant classifications to Gemini for review
3. Collects discrepancies from all pages
4. Outputs combined gemini_review.json
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def get_page_classifications(classifications_data: dict, page_num: int) -> list:
    """Get all classifications and discovered items for a specific page."""
    items = []
    for c in classifications_data.get('classifications', []):
        if c.get('page') == page_num:
            items.append(c)
    for d in classifications_data.get('discovered_items', []):
        if d.get('page') == page_num:
            items.append(d)
    return items


def build_review_prompt(page_num: int, classifications_json: str) -> str:
    """Build the Gemini review prompt for a page."""
    return f"""# P&ID Classification Review Task

You are reviewing Claude's P&ID entity classifications against this page image.
Your role is REVIEWER - find errors and omissions, don't reclassify everything.

## Claude's Classifications for Page {page_num}

{classifications_json}

## Your Review Task

Compare each of Claude's classifications against what you see in the image.
Report ONLY discrepancies - do not repeat correct classifications.

### Discrepancy Types
1. **WRONG**: Claude's classification is incorrect (wrong tag_type, equipment_type, instrument_type, variable, functions)
2. **MISSING**: Tag visible in image but NOT in Claude's list
3. **MISSING_LINE**: Piping line number visible but not in Claude's list
4. **MISSING_VALVE_SYMBOL**: Valve symbol visible without tag in Claude's list
5. **CORRECTED**: Minor correction needed (better description, nearby equipment, context)

### Output Format

Return ONLY valid JSON (no markdown, no explanation):
{{
  "page": {page_num},
  "review_summary": {{
    "total_in_claude_list": <count>,
    "verified_correct": <count>,
    "discrepancies_found": <count>
  }},
  "discrepancies": [
    {{
      "tag": "<tag>",
      "issue": "WRONG|MISSING|MISSING_LINE|MISSING_VALVE_SYMBOL|CORRECTED",
      "claude_said": {{ ... }} or null,
      "correction": {{ ... }},
      "evidence": "<what you see in image>",
      "confidence": "high|medium|low"
    }}
  ]
}}

If no discrepancies found, return empty discrepancies array.
If this page has no P&ID content (title page, legend, etc.), return discrepancies_found: 0."""


def run_gemini_review(page_image: Path, prompt: str, timeout: int = 120) -> dict:
    """Run Gemini CLI to review a page."""
    cmd = [
        'gemini',
        prompt,
        f'@{page_image.name}',
        '-o', 'json'
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=page_image.parent,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            print(f"  Gemini error: {result.stderr}", file=sys.stderr)
            return None

        # Handle empty output
        if not result.stdout or not result.stdout.strip():
            print(f"  Empty response from Gemini", file=sys.stderr)
            return None

        # Parse Gemini's JSON output
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"  Failed to parse Gemini output as JSON: {e}", file=sys.stderr)
            return None

        response = output.get('response', '')
        if not response:
            print(f"  No 'response' field in Gemini output", file=sys.stderr)
            return None

        # Try to extract JSON from response
        # Look for JSON object in response
        try:
            # Try to find JSON in response
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                review_json = json.loads(response[start:end])
                # Validate expected fields
                if 'page' not in review_json:
                    review_json['page'] = None  # Will be set by caller
                if 'discrepancies' not in review_json:
                    review_json['discrepancies'] = []
                if 'review_summary' not in review_json:
                    review_json['review_summary'] = {
                        'total_in_claude_list': 0,
                        'verified_correct': 0,
                        'discrepancies_found': len(review_json.get('discrepancies', []))
                    }
                return review_json
        except json.JSONDecodeError as e:
            print(f"  Failed to parse JSON from response: {e}", file=sys.stderr)

        # If no valid JSON, return error indicator
        print(f"  Could not parse JSON from Gemini response", file=sys.stderr)
        return None

    except subprocess.TimeoutExpired:
        print(f"  Gemini timed out after {timeout}s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description='Run Gemini review on all P&ID pages')
    parser.add_argument('output_dir', help='Directory with page images and classifications_claude.json')
    parser.add_argument('--timeout', type=int, default=120, help='Timeout per page in seconds')
    parser.add_argument('--pages', help='Comma-separated page numbers to review (default: all)')
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    classifications_file = output_dir / 'classifications_claude.json'

    if not classifications_file.exists():
        print(f"Error: {classifications_file} not found", file=sys.stderr)
        sys.exit(1)

    with open(classifications_file) as f:
        classifications_data = json.load(f)

    # Find all page images
    page_images = sorted(output_dir.glob('page_*.png'))
    if not page_images:
        print(f"Error: No page images found in {output_dir}", file=sys.stderr)
        sys.exit(1)

    # Filter pages if specified
    if args.pages:
        selected_pages = set(int(p) for p in args.pages.split(','))
        page_images = [p for p in page_images if int(p.stem.split('_')[1]) in selected_pages]

    print(f"Reviewing {len(page_images)} pages...")

    all_reviews = []
    total_discrepancies = 0

    for page_image in page_images:
        page_num = int(page_image.stem.split('_')[1])
        print(f"Page {page_num:03d}...", end=' ', flush=True)

        # Get classifications for this page
        page_items = get_page_classifications(classifications_data, page_num)

        if not page_items:
            # No classifications for this page, but still review for missing items
            classifications_json = "[]"
        else:
            classifications_json = json.dumps(page_items, indent=2)

        prompt = build_review_prompt(page_num, classifications_json)
        review = run_gemini_review(page_image, prompt, args.timeout)

        if review:
            discrepancy_count = review.get('review_summary', {}).get('discrepancies_found', 0)
            total_discrepancies += discrepancy_count
            all_reviews.append(review)
            print(f"{discrepancy_count} discrepancies")
        else:
            # Create placeholder for failed review
            all_reviews.append({
                'page': page_num,
                'review_summary': {
                    'total_in_claude_list': len(page_items),
                    'verified_correct': 0,
                    'discrepancies_found': 0,
                    'error': 'review_failed'
                },
                'discrepancies': []
            })
            print("FAILED")

    # Save combined review
    output_file = output_dir / 'gemini_review.json'
    with open(output_file, 'w') as f:
        json.dump(all_reviews, f, indent=2)

    print(f"\nTotal discrepancies found: {total_discrepancies}")
    print(f"Review saved to: {output_file}")


if __name__ == '__main__':
    main()
