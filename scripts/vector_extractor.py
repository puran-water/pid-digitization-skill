#!/usr/bin/env python3
"""
Vector Text Extraction from PDF P&IDs.

Stage 1 of the PID digitization pipeline:
- Classifies PDF as vector-rich vs raster/scanned
- Extracts text labels with bounding boxes using pdfplumber
- Clusters multi-line instrument bubbles (PG + 02 → PG-02)
- Pre-filters for ISA-style AND descriptive tags
- Renders page images for VLM context

Usage:
    python vector_extractor.py input.pdf
    python vector_extractor.py input.pdf --output-dir ./custom_output/
    python vector_extractor.py input.pdf --pages 1,2,3

Output is written to {pdf_folder}/{pdf_name_sanitized}/ by default.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import pdfplumber
except ImportError:
    print("Error: pdfplumber required. Install with: pip install pdfplumber")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow required. Install with: pip install Pillow")
    sys.exit(1)


# =============================================================================
# TAG PATTERNS - Three tiers for comprehensive detection
# =============================================================================

# Tier 1: ISA Strict - Full canonical format with area prefix
ISA_STRICT_PATTERNS = [
    # Instrument: 200-FIT-01, 300-LIC-05A
    re.compile(r"^\d{3}-[A-Z]{2,5}-\d{1,4}[A-Z]?$"),
    # Equipment: 200-P-01, 300-TK-02A
    re.compile(r"^\d{3}-[A-Z]{1,5}-\d{1,4}[A-Z]?$"),
]

# Tier 2: ISA-Like - Missing area prefix but otherwise ISA format
ISA_LIKE_PATTERNS = [
    # Instrument without area: FIT-01, LIC-05A, PG-12
    re.compile(r"^[A-Z]{2,5}-\d{1,4}[A-Z]?$"),
    # Equipment without area: P-01, TK-02A
    re.compile(r"^[A-Z]{1,3}-\d{1,4}[A-Z]?$"),
]

# Tier 3: Descriptive - Project-specific naming
DESCRIPTIVE_PATTERNS = [
    # FINE-BAR-SCREEN, EQ-TANK, MBR-BLOWER
    re.compile(r"^[A-Z][A-Z0-9\-]{3,30}$"),
]

# Line number patterns (separate category)
# Line numbers typically: SIZE-SERVICE-SEQ[-CLASS[-SPEC]]
# Examples: 2-PW-001-A1, 30-VB-5151-CVPu-DC, 00-GF-61-05-CVPu-WT, 4"-AIR-101
LINE_PATTERNS = [
    # Standard: SIZE-SERVICE-SEQ (size 1-3 digits, service 2-6 letters)
    re.compile(r"^\d{1,3}-[A-Z]{2,6}-\d"),
    # With inch symbol: 2"-PW-001
    re.compile(r'^\d{1,3}["\']?-[A-Z]{2,6}-\d'),
    # Service code with number: H2O-001, N2-005
    re.compile(r"^[A-Z0-9]{2,4}-\d{2,4}(-[A-Z0-9]{1,6})+$"),
    # Longer service codes: NAOH-001-A1, FECL3-002
    re.compile(r"^[A-Z]{2,6}\d?-\d{2,4}"),
]

# Legacy combined patterns for backward compatibility
ISA_TAG_PATTERNS = ISA_STRICT_PATTERNS + ISA_LIKE_PATTERNS

# Minimum text character count to classify as vector PDF
VECTOR_TEXT_THRESHOLD = 100

# Clustering parameters for multi-line instrument bubbles (vertical clustering)
CLUSTER_X_TOLERANCE = 20    # Max horizontal distance to consider same cluster
CLUSTER_Y_GAP_MAX = 15      # Max vertical gap between lines in cluster

# Horizontal clustering parameters (for vertically-oriented line numbers)
HORIZONTAL_CLUSTER_Y_TOLERANCE = 10  # Max vertical distance for horizontal cluster
HORIZONTAL_CLUSTER_X_GAP_MAX = 30    # Max horizontal gap between elements


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_default_output_dir(pdf_path: Path) -> Path:
    """
    Auto-derive output directory from PDF path.
    Output goes to {pdf_folder}/{sanitized_pdf_name}/
    """
    pdf_dir = pdf_path.parent
    pdf_name = pdf_path.stem  # filename without extension

    # Sanitize: lowercase, replace special chars with hyphens
    safe_name = re.sub(r'[^\w\-]', '-', pdf_name.lower())
    safe_name = re.sub(r'-+', '-', safe_name).strip('-')

    return pdf_dir / safe_name


def union_bbox(bbox1: Optional[list], bbox2: list) -> list:
    """Compute bounding box that contains both input boxes."""
    if bbox1 is None:
        return bbox2.copy()
    return [
        min(bbox1[0], bbox2[0]),  # x0
        min(bbox1[1], bbox2[1]),  # y0
        max(bbox1[2], bbox2[2]),  # x1
        max(bbox1[3], bbox2[3]),  # y1
    ]


def get_x_center(bbox: list) -> float:
    """Get horizontal center of bounding box."""
    return (bbox[0] + bbox[2]) / 2


# =============================================================================
# TAG PATTERN MATCHING
# =============================================================================

def classify_tag_pattern(text: str) -> tuple[bool, str]:
    """
    Classify text against tag patterns.

    Returns:
        (is_candidate, pattern_tier) where pattern_tier is one of:
        - "isa_strict": Full ISA format with area
        - "isa_like": ISA format without area
        - "descriptive": Project-specific naming
        - "line_number": Piping line designation
        - None: Not a tag candidate
    """
    text = text.strip().upper()

    if any(p.match(text) for p in ISA_STRICT_PATTERNS):
        return True, "isa_strict"
    if any(p.match(text) for p in ISA_LIKE_PATTERNS):
        return True, "isa_like"
    if any(p.match(text) for p in LINE_PATTERNS):
        return True, "line_number"
    if any(p.match(text) for p in DESCRIPTIVE_PATTERNS):
        # Additional filter for descriptive: must contain hyphen or be equipment-like
        if '-' in text or len(text) >= 4:
            return True, "descriptive"

    return False, None


# =============================================================================
# BBOX CLUSTERING - Multi-line tag reconstruction
# =============================================================================

def cluster_text_elements(
    text_elements: list[dict],
    x_tolerance: float = CLUSTER_X_TOLERANCE,
    y_gap_max: float = CLUSTER_Y_GAP_MAX
) -> list[dict]:
    """
    Cluster vertically-stacked text elements in instrument bubbles.

    Instrument tags on P&IDs are often drawn as:
        PG
        02

    This function clusters such text by spatial proximity and
    reconstructs them as: PG-02

    Args:
        text_elements: List of text elements with bbox coordinates
        x_tolerance: Max horizontal distance to consider same cluster
        y_gap_max: Max vertical gap between lines

    Returns:
        List of reconstructed text elements with fragment provenance
    """
    if not text_elements:
        return []

    # Group elements by x-center proximity
    # This groups text that is vertically aligned (same instrument bubble)
    x_groups = defaultdict(list)

    for elem in text_elements:
        bbox = elem['bbox']
        x_center = get_x_center(bbox)

        # Find existing group within tolerance
        assigned = False
        for group_x in list(x_groups.keys()):
            if abs(x_center - group_x) <= x_tolerance:
                x_groups[group_x].append(elem)
                assigned = True
                break

        if not assigned:
            x_groups[x_center].append(elem)

    reconstructed = []

    for group_x, group_elems in x_groups.items():
        # Sort by y-position (top to bottom)
        sorted_elems = sorted(group_elems, key=lambda e: e['bbox'][1])

        # Cluster vertically adjacent elements
        current_cluster = []
        current_bbox = None

        for elem in sorted_elems:
            if not current_cluster:
                current_cluster = [elem]
                current_bbox = elem['bbox'].copy()
            else:
                # Check vertical gap from previous element
                prev_bottom = current_bbox[3]  # y1 of current merged bbox
                curr_top = elem['bbox'][1]     # y0 of current element
                gap = curr_top - prev_bottom

                if gap <= y_gap_max:
                    # Same cluster - merge
                    current_cluster.append(elem)
                    current_bbox = union_bbox(current_bbox, elem['bbox'])
                else:
                    # Gap too large - emit current cluster and start new
                    reconstructed.append(
                        _create_reconstructed_element(current_cluster, current_bbox)
                    )
                    current_cluster = [elem]
                    current_bbox = elem['bbox'].copy()

        # Emit final cluster
        if current_cluster:
            reconstructed.append(
                _create_reconstructed_element(current_cluster, current_bbox)
            )

    return reconstructed


def _create_reconstructed_element(cluster: list[dict], merged_bbox: list) -> dict:
    """
    Create a reconstructed text element from a cluster.

    Merges text with intelligent hyphen insertion:
    - LETTERS + DIGITS → "PG" + "02" = "PG-02"
    - LETTERS + LETTERS → "MBR" + "TANK" = "MBR-TANK"
    """
    if len(cluster) == 1:
        # Single element - no reconstruction needed
        elem = cluster[0].copy()
        elem['reconstruction_method'] = 'single_text'
        elem['fragments'] = None
        return elem

    # Sort by y-position and merge text
    sorted_cluster = sorted(cluster, key=lambda e: e['bbox'][1])
    texts = [e['text'] for e in sorted_cluster]

    merged_text = ""
    for i, text in enumerate(texts):
        if i == 0:
            merged_text = text
        else:
            prev_char = merged_text[-1] if merged_text else ''
            curr_char = text[0] if text else ''

            # Insert hyphen between letters and digits, or letters and letters
            if prev_char.isalpha() and curr_char.isdigit():
                merged_text += "-"
            elif prev_char.isalpha() and curr_char.isalpha() and '-' not in merged_text:
                merged_text += "-"

            merged_text += text

    # Build fragment provenance
    fragments = [
        {
            'text': e['text'],
            'bbox': e['bbox'],
            'font_size': e.get('font_size'),
        }
        for e in sorted_cluster
    ]

    return {
        'text': merged_text,
        'bbox': merged_bbox,
        'reconstruction_method': 'clustered',
        'fragments': fragments,
        'page': sorted_cluster[0].get('page'),
        'font_size': sorted_cluster[0].get('font_size'),
        'font_name': sorted_cluster[0].get('font_name'),
    }


def cluster_horizontal_elements(
    text_elements: list[dict],
    y_tolerance: float = HORIZONTAL_CLUSTER_Y_TOLERANCE,
    x_gap_max: float = HORIZONTAL_CLUSTER_X_GAP_MAX
) -> list[dict]:
    """
    Cluster horizontally-adjacent text elements (side-by-side).

    This handles vertically-oriented line numbers on P&IDs where text
    is written along the pipe and may be split into multiple elements.

    Example: "2" "-" "PW" "-" "001" at same Y level → "2-PW-001"

    Args:
        text_elements: List of text elements with bbox coordinates
        y_tolerance: Max vertical distance to consider same row
        x_gap_max: Max horizontal gap between adjacent elements

    Returns:
        List of reconstructed text elements
    """
    if not text_elements:
        return []

    # Group elements by y-center proximity (same horizontal row)
    y_groups = defaultdict(list)

    for elem in text_elements:
        bbox = elem['bbox']
        y_center = (bbox[1] + bbox[3]) / 2

        # Find existing group within tolerance
        assigned = False
        for group_y in list(y_groups.keys()):
            if abs(y_center - group_y) <= y_tolerance:
                y_groups[group_y].append(elem)
                assigned = True
                break

        if not assigned:
            y_groups[y_center].append(elem)

    reconstructed = []

    for group_y, group_elems in y_groups.items():
        # Sort by x-position (left to right)
        sorted_elems = sorted(group_elems, key=lambda e: e['bbox'][0])

        # Cluster horizontally adjacent elements
        current_cluster = []
        current_bbox = None

        for elem in sorted_elems:
            if not current_cluster:
                current_cluster = [elem]
                current_bbox = elem['bbox'].copy()
            else:
                # Check horizontal gap from previous element
                prev_right = current_bbox[2]  # x1 of current merged bbox
                curr_left = elem['bbox'][0]   # x0 of current element
                gap = curr_left - prev_right

                if gap <= x_gap_max:
                    # Same cluster - merge
                    current_cluster.append(elem)
                    current_bbox = union_bbox(current_bbox, elem['bbox'])
                else:
                    # Gap too large - emit current cluster and start new
                    reconstructed.append(
                        _create_horizontal_element(current_cluster, current_bbox)
                    )
                    current_cluster = [elem]
                    current_bbox = elem['bbox'].copy()

        # Emit final cluster
        if current_cluster:
            reconstructed.append(
                _create_horizontal_element(current_cluster, current_bbox)
            )

    return reconstructed


def _create_horizontal_element(cluster: list[dict], merged_bbox: list) -> dict:
    """Create a reconstructed text element from a horizontal cluster."""
    if len(cluster) == 1:
        elem = cluster[0].copy()
        elem['reconstruction_method'] = 'single_text'
        elem['fragments'] = None
        return elem

    # Sort by x-position and merge text
    sorted_cluster = sorted(cluster, key=lambda e: e['bbox'][0])
    texts = [e['text'] for e in sorted_cluster]

    # Join with hyphens if segments look like line number parts
    # Otherwise join directly
    merged_text = ""
    for i, text in enumerate(texts):
        if i == 0:
            merged_text = text
        else:
            # Check if we need a hyphen
            prev_text = texts[i-1]
            if (prev_text.isdigit() and text.isalpha()) or \
               (prev_text.isalpha() and text.isdigit()) or \
               (prev_text.isalpha() and text.isalpha() and len(prev_text) <= 3):
                merged_text += "-" + text
            elif text == "-":
                merged_text += text
            else:
                merged_text += text

    fragments = [
        {
            'text': e['text'],
            'bbox': e['bbox'],
            'font_size': e.get('font_size'),
        }
        for e in sorted_cluster
    ]

    return {
        'text': merged_text,
        'bbox': merged_bbox,
        'reconstruction_method': 'horizontal_clustered',
        'fragments': fragments,
        'page': sorted_cluster[0].get('page'),
        'font_size': sorted_cluster[0].get('font_size'),
        'font_name': sorted_cluster[0].get('font_name'),
    }


def classify_pdf_type(page) -> str:
    """
    Classify PDF page as vector-rich or raster/scanned.

    Args:
        page: pdfplumber page object

    Returns:
        "vector" if text-rich, "raster" otherwise
    """
    chars = page.chars or []
    return "vector" if len(chars) > VECTOR_TEXT_THRESHOLD else "raster"


def is_isa_tag_candidate(text: str) -> bool:
    """Check if text matches any tag patterns (legacy compatibility)."""
    is_candidate, _ = classify_tag_pattern(text)
    return is_candidate


def extract_text_labels(page, page_num: int, enable_clustering: bool = True) -> list[dict]:
    """
    Extract text labels with bounding boxes from a PDF page.

    Applies bbox clustering to reconstruct multi-line instrument tags
    (e.g., "PG" over "02" becomes "PG-02").

    Args:
        page: pdfplumber page object
        page_num: 1-indexed page number
        enable_clustering: If True, cluster vertically-adjacent text

    Returns:
        List of text label dictionaries with bbox coordinates and pattern tier
    """
    # Extract words with bounding boxes
    words = page.extract_words(
        keep_blank_chars=False,
        x_tolerance=3,
        y_tolerance=3,
        extra_attrs=["fontname", "size"]
    )

    raw_labels = []
    for word in words:
        text = word.get("text", "").strip()
        if not text:
            continue

        raw_labels.append({
            "text": text,
            "bbox": [
                round(word["x0"], 2),
                round(word["top"], 2),
                round(word["x1"], 2),
                round(word["bottom"], 2)
            ],
            "page": page_num,
            "font_size": round(word.get("size", 0), 1) if word.get("size") else None,
            "font_name": word.get("fontname"),
        })

    # Apply clustering to reconstruct multi-line tags
    if enable_clustering:
        # First pass: vertical clustering (instrument bubbles like PG over 02)
        labels = cluster_text_elements(raw_labels)
        # Second pass: horizontal clustering (line numbers along pipes)
        labels = cluster_horizontal_elements(labels)
    else:
        # Add reconstruction metadata to unclustered labels
        labels = []
        for label in raw_labels:
            label['reconstruction_method'] = 'single_text'
            label['fragments'] = None
            labels.append(label)

    # Classify each label against tag patterns
    for label in labels:
        text = label['text']
        is_candidate, pattern_tier = classify_tag_pattern(text)
        label['is_tag_candidate'] = is_candidate
        label['pattern_tier'] = pattern_tier
        label['extraction_source'] = 'vector'

    return labels


def render_page_image(page, output_path: Path, resolution: int = 150) -> Path:
    """
    Render PDF page as image for VLM processing.

    Args:
        page: pdfplumber page object
        output_path: Path for output image
        resolution: DPI for rendering

    Returns:
        Path to saved image
    """
    img = page.to_image(resolution=resolution)
    img.save(str(output_path), format="PNG")
    return output_path


def extract_page(page, page_num: int, output_dir: Path) -> dict:
    """
    Extract all data from a single PDF page.

    Args:
        page: pdfplumber page object
        page_num: 1-indexed page number
        output_dir: Directory for output files

    Returns:
        Dictionary with page extraction results including:
        - tag_candidates: All detected tags with pattern tier
        - candidates_by_tier: Breakdown by pattern tier (isa_strict, isa_like, etc.)
        - reconstructed_count: Tags that were clustered from multi-line text
    """
    pdf_type = classify_pdf_type(page)
    text_labels = extract_text_labels(page, page_num, enable_clustering=True)

    # Separate tag candidates from other text
    tag_candidates = [l for l in text_labels if l["is_tag_candidate"]]
    other_text = [l for l in text_labels if not l["is_tag_candidate"]]

    # Count by pattern tier
    tier_counts = {
        "isa_strict": 0,
        "isa_like": 0,
        "descriptive": 0,
        "line_number": 0,
    }
    reconstructed_count = 0

    for candidate in tag_candidates:
        tier = candidate.get("pattern_tier")
        if tier and tier in tier_counts:
            tier_counts[tier] += 1
        if candidate.get("reconstruction_method") == "clustered":
            reconstructed_count += 1

    # Render page image
    image_path = output_dir / f"page_{page_num:03d}.png"
    render_page_image(page, image_path)

    return {
        "page": page_num,
        "pdf_type": pdf_type,
        "dimensions": {
            "width": round(page.width, 2),
            "height": round(page.height, 2),
        },
        "text_label_count": len(text_labels),
        "tag_candidate_count": len(tag_candidates),
        "candidates_by_tier": tier_counts,
        "reconstructed_count": reconstructed_count,
        "tag_candidates": tag_candidates,
        "other_text": other_text,
        "page_image": str(image_path),
    }


def extract_pdf(
    pdf_path: Path,
    output_dir: Path,
    pages: Optional[list[int]] = None
) -> dict:
    """
    Extract text and images from PDF P&ID.

    Args:
        pdf_path: Path to input PDF
        output_dir: Directory for output files
        pages: Optional list of page numbers (1-indexed), or None for all

    Returns:
        Dictionary with extraction results including:
        - Summary with tier breakdown and reconstruction stats
        - Per-page candidates with bbox and pattern tier
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "source_file": str(pdf_path.name),
        "source_path": str(pdf_path.absolute()),
        "output_dir": str(output_dir.absolute()),
        "pages": [],
        "summary": {
            "total_pages": 0,
            "vector_pages": 0,
            "raster_pages": 0,
            "total_tag_candidates": 0,
            "candidates_by_tier": {
                "isa_strict": 0,
                "isa_like": 0,
                "descriptive": 0,
                "line_number": 0,
            },
            "reconstructed_from_multiline": 0,
        }
    }

    with pdfplumber.open(pdf_path) as pdf:
        results["summary"]["total_pages"] = len(pdf.pages)

        for i, page in enumerate(pdf.pages):
            page_num = i + 1

            # Skip if specific pages requested and this isn't one of them
            if pages and page_num not in pages:
                continue

            page_result = extract_page(page, page_num, output_dir)
            results["pages"].append(page_result)

            # Update summary
            if page_result["pdf_type"] == "vector":
                results["summary"]["vector_pages"] += 1
            else:
                results["summary"]["raster_pages"] += 1

            results["summary"]["total_tag_candidates"] += page_result["tag_candidate_count"]
            results["summary"]["reconstructed_from_multiline"] += page_result["reconstructed_count"]

            # Aggregate tier counts
            for tier, count in page_result["candidates_by_tier"].items():
                results["summary"]["candidates_by_tier"][tier] += count

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract text labels from PDF P&IDs with multi-line tag reconstruction"
    )
    parser.add_argument("pdf_path", type=Path, help="Path to input PDF")
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=None,
        help="Output directory (default: {pdf_folder}/{pdf_name}/)"
    )
    parser.add_argument(
        "--pages", "-p",
        type=str,
        help="Comma-separated page numbers to extract (1-indexed)"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output results as JSON to stdout"
    )
    parser.add_argument(
        "--no-clustering",
        action="store_true",
        help="Disable multi-line tag clustering"
    )
    args = parser.parse_args()

    if not args.pdf_path.exists():
        print(f"Error: File not found: {args.pdf_path}")
        sys.exit(1)

    # Auto-derive output directory if not specified
    if args.output_dir is None:
        output_dir = get_default_output_dir(args.pdf_path)
    else:
        output_dir = args.output_dir

    # Parse page numbers if specified
    pages = None
    if args.pages:
        pages = [int(p.strip()) for p in args.pages.split(",")]

    # Extract
    results = extract_pdf(args.pdf_path, output_dir, pages)

    # Save results
    output_json = output_dir / "extraction_results.json"
    with open(output_json, "w") as f:
        json.dump(results, f, indent=2)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        summary = results['summary']
        tiers = summary['candidates_by_tier']

        print(f"Extraction complete:")
        print(f"  Source: {results['source_file']}")
        print(f"  Output: {output_dir}")
        print(f"  Pages processed: {len(results['pages'])}")
        print(f"  Vector pages: {summary['vector_pages']}")
        print(f"  Raster pages: {summary['raster_pages']}")
        print(f"\nTag candidates found: {summary['total_tag_candidates']}")
        print(f"  ISA Strict (XXX-ABC-NN):  {tiers['isa_strict']}")
        print(f"  ISA-Like (ABC-NN):        {tiers['isa_like']}")
        print(f"  Descriptive:              {tiers['descriptive']}")
        print(f"  Line numbers:             {tiers['line_number']}")
        print(f"\nMulti-line reconstructed: {summary['reconstructed_from_multiline']}")
        print(f"  (e.g., 'PG' + '02' → 'PG-02')")
        print(f"\nResults saved to: {output_json}")

        if summary["raster_pages"] > 0:
            print("\nWarning: Raster pages detected. OCR may be needed for full extraction.")


if __name__ == "__main__":
    main()
