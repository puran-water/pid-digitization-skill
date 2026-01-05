#!/usr/bin/env python3
"""
Vector Text Extraction from PDF P&IDs.

Stage 1 of the PID digitization pipeline:
- Classifies PDF as vector-rich vs raster/scanned
- Extracts text labels with bounding boxes using pdfplumber
- Pre-filters for ISA-style tags using regex
- Renders page images for VLM context

Usage:
    python vector_extractor.py input.pdf --output-dir ./extracted/
    python vector_extractor.py input.pdf --pages 1,2,3 --output-dir ./extracted/
"""

import argparse
import json
import re
import sys
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


# ISA tag patterns for pre-filtering
ISA_TAG_PATTERNS = [
    # Instrument tags: 200-FIT-01, 300-LIC-05A
    re.compile(r"^\d{3}-[A-Z]{2,5}-\d{1,4}[A-Z]?$"),
    # Equipment tags: 200-P-01, 300-TK-02A
    re.compile(r"^\d{3}-[A-Z]{1,5}-\d{1,4}[A-Z]?$"),
    # Line numbers: 200-PW-4"-SS (may have special chars)
    re.compile(r"^\d{3}-[A-Z]{2,3}-\d"),
]

# Minimum text character count to classify as vector PDF
VECTOR_TEXT_THRESHOLD = 100


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
    """Check if text matches ISA tag patterns."""
    text = text.strip().upper()
    return any(p.match(text) for p in ISA_TAG_PATTERNS)


def extract_text_labels(page, page_num: int) -> list[dict]:
    """
    Extract text labels with bounding boxes from a PDF page.

    Args:
        page: pdfplumber page object
        page_num: 1-indexed page number

    Returns:
        List of text label dictionaries with bbox coordinates
    """
    labels = []

    # Extract words with bounding boxes
    words = page.extract_words(
        keep_blank_chars=False,
        x_tolerance=3,
        y_tolerance=3,
        extra_attrs=["fontname", "size"]
    )

    for word in words:
        text = word.get("text", "").strip()
        if not text:
            continue

        label = {
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
            "is_tag_candidate": is_isa_tag_candidate(text),
        }
        labels.append(label)

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
        Dictionary with page extraction results
    """
    pdf_type = classify_pdf_type(page)
    text_labels = extract_text_labels(page, page_num)

    # Separate tag candidates from other text
    tag_candidates = [l for l in text_labels if l["is_tag_candidate"]]
    other_text = [l for l in text_labels if not l["is_tag_candidate"]]

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
        Dictionary with extraction results
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "source_file": str(pdf_path),
        "pages": [],
        "summary": {
            "total_pages": 0,
            "vector_pages": 0,
            "raster_pages": 0,
            "total_tag_candidates": 0,
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

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract text labels from PDF P&IDs"
    )
    parser.add_argument("pdf_path", type=Path, help="Path to input PDF")
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("./extracted"),
        help="Output directory (default: ./extracted)"
    )
    parser.add_argument(
        "--pages", "-p",
        type=str,
        help="Comma-separated page numbers to extract (1-indexed)"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output results as JSON"
    )
    args = parser.parse_args()

    if not args.pdf_path.exists():
        print(f"Error: File not found: {args.pdf_path}")
        sys.exit(1)

    # Parse page numbers if specified
    pages = None
    if args.pages:
        pages = [int(p.strip()) for p in args.pages.split(",")]

    # Extract
    results = extract_pdf(args.pdf_path, args.output_dir, pages)

    # Save results
    output_json = args.output_dir / "extraction_results.json"
    with open(output_json, "w") as f:
        json.dump(results, f, indent=2)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"Extraction complete:")
        print(f"  Source: {results['source_file']}")
        print(f"  Pages processed: {len(results['pages'])}")
        print(f"  Vector pages: {results['summary']['vector_pages']}")
        print(f"  Raster pages: {results['summary']['raster_pages']}")
        print(f"  Tag candidates found: {results['summary']['total_tag_candidates']}")
        print(f"  Results saved to: {output_json}")

        if results["summary"]["raster_pages"] > 0:
            print("\nWarning: Raster pages detected. OCR may be needed for full extraction.")


if __name__ == "__main__":
    main()
