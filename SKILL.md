---
name: pid-digitization-skill
description: |
  Extract equipment, instruments, and control loops from PDF P&IDs for legacy project onboarding.
  Tier 0 foundational skill that produces YAML artifacts for downstream skills.
  Use when: (1) Digitizing legacy P&ID drawings, (2) Extracting equipment/instrument lists from PDFs,
  (3) Creating instrument database from existing drawings, (4) Onboarding projects without DEXPI XML.
  Outputs: equipment-list.yaml, instrument-database.yaml (with loops).
---

# P&ID Digitization Skill

Extract equipment and instrument entities from PDF P&IDs using vector text extraction and VLM classification.

## Workflow

```
PDF P&ID (multi-page)
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 1: Vector Extraction (vector_extractor.py)│
│ - Classify PDF: vector vs raster                │
│ - Extract text labels with bboxes               │
│ - Pre-filter ISA tag candidates                 │
│ - Render page images for VLM                    │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 2a: VLM Classification (Agent executes)   │
│ - Claude (native): classify candidates          │
│ - Gemini (CLI): cross-validate                  │
│ - Output: JSON classification files             │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 2b: Merge (merge_vlm_classifications.py)  │
│ - Ingest VLM JSON outputs                       │
│ - Apply multi-model consensus                   │
│ - Evidence gating: no evidence → review_required│
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 3: Validation (validate_extraction.py)    │
│ - ISA tag format validation                     │
│ - Equipment-instrument spatial linking          │
│ - Loop grouping with devices array              │
│ - Confidence-based review flagging              │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Output: Tier 1 YAML Artifacts                   │
│ - equipment-list.yaml                           │
│ - instrument-database.yaml (includes loops)     │
│ - validation-report.yaml                        │
└─────────────────────────────────────────────────┘
```

## Setup

Create skill-local venv (one-time):

```bash
cd ~/skills/pid-digitization-skill
python3 -m venv .venv
.venv/bin/pip install pdfplumber pillow pyyaml jsonschema
```

## Quick Start

### 1. Extract from PDF

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/vector_extractor.py \
  /path/to/pid.pdf --output-dir ./extracted/
```

Output:
- `extracted/page_NNN.png` - Page images for VLM
- `extracted/extraction_results.json` - Text labels with bboxes

### 2. VLM Classification (Agent executes)

Read the page images and classify extracted candidates using the prompt in `templates/vlm-classification-prompt.md`.

**Claude (native)** - save output to `classifications_claude.json`:
```
Look at the attached P&ID page image (extracted/page_001.png).
Classify these tag candidates from extraction_results.json.
Output JSON array per templates/vlm-classification-prompt.md.
```

**Gemini (cross-validation)** - save output to `classifications_gemini.json`:
```bash
gemini -p "Classify P&ID tags from this image. \
  Candidates: $(jq '.pages[0].tag_candidates' extracted/extraction_results.json)" \
  @extracted/page_001.png > classifications_gemini.json
```

### 3. Merge VLM Classifications

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/merge_vlm_classifications.py \
  extracted/extraction_results.json \
  --claude classifications_claude.json \
  --gemini classifications_gemini.json \
  --output merged_results.json
```

### 4. Validate & Output

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/validate_extraction.py \
  merged_results.json --output-dir ./validated/
```

Output:
- `validated/equipment-list.yaml`
- `validated/instrument-database.yaml`
- `validated/validation-report.yaml`

Items with `review_required: true` need human verification.

## Output Artifacts

### equipment-list.yaml

Aligned with `equipment-list-skill` schema:

```yaml
equipment:
  - tag: "200-P-01"
    description: "Permeate Pump"
    area: 200
    process_unit_type: "secondary_treatment.aerobic_biological_treatment.secondary_clarification.submerged_membrane_bioreactor"
    kind: "equipment"
    feeder_type: "VFD"
    provenance:
      source_type: "pdf_extracted"
      page: 1
      confidence: 0.92
      bbox: [x1, y1, x2, y2]
```

### instrument-database.yaml

Aligned with `instrument-io-skill` schema:

```yaml
project_id: null  # Fill after extraction
revision:
  number: "A"
  date: "2025-01-04"
  by: "CLAUDE"

source_pids:
  - pid_number: "PID-001"
    source_type: "pdf_extracted"
    extraction_date: "2025-01-04T12:00:00Z"

loops:
  - loop_key: "200-F-01"
    tag_area: 200
    loop_number: 1
    variable: "F"
    devices:
      - full_tag: "200-FIT-01"
        functions: ["I", "T"]
        role: "measurement"
      - full_tag: "200-FV-01"
        functions: ["V"]
        role: "final_element"

instruments:
  - instrument_id: "uuid"
    loop_key: "200-F-01"
    tag:
      area: "200"
      variable: "F"
      function: "IT"
      functions: ["I", "T"]
      loop_number: "01"
      full_tag: "200-FIT-01"
    equipment_tag: "200-P-01"
    service_description: "Permeate Flow"
    primary_signal_type: "4-20mA"
    provenance:
      source_type: "pdf_extracted"
      page: 1
      confidence: 0.88
```

## VLM Classification Guidelines

VLMs act as **labelers, not localizers**:
- Classify pre-extracted candidates only
- Do NOT discover new entities
- Emit "unknown" if no visual evidence
- Use multi-model consensus (Claude + Gemini)

See `templates/vlm-classification-prompt.md` for full prompt.

### Confidence Scoring & Review Flagging

| Confidence | Meaning |
|------------|---------|
| high (>0.85) | Clear visual evidence, validated tag format |
| medium (0.6-0.85) | Partial evidence, some inference |
| low (<0.6) | Minimal evidence, requires review |

Items are flagged with `review_required: true` when:
- Confidence < 0.8
- VLM models disagree on classification
- Required fields (service_description, primary_signal_type) not provided
- VLM classified as "unknown" or "noise"

## Raster PDF Handling

If `vector_extractor.py` detects a raster/scanned page:
- Warning logged in extraction results
- Page image still rendered for VLM
- Manual review recommended for text extraction

## Integration

### Upstream
None (Tier 0 foundational skill)

### Downstream Consumers

| Skill | Consumes | Purpose |
|-------|----------|---------|
| `equipment-list-skill` | `equipment-list.yaml` | Equipment database |
| `instrument-io-skill` | `instrument-database.yaml` | IO list generation |
| `control-philosophy-skill` | Both artifacts | Control narratives |
| `bfd-skill` | `equipment-list.yaml` | Block flow diagrams |

### Handoff Contract

- Equipment tags: `{AREA}-{CODE}-{SEQ}` format
- Instrument tags: ISA-5.1 compliant `{AREA}-{VAR}{FUNC}-{LOOP}`
- Loop keys: `{AREA}-{VAR}-{LOOP}` format
- All process_unit_type values must resolve in taxonomy

## Dependencies

Skill-local venv at `.venv/` (see Setup section):

```bash
pdfplumber   # PDF text extraction
pillow       # Image rendering
pyyaml       # YAML output
jsonschema   # Schema validation
```

Gemini CLI must be installed separately for cross-validation.

## Bundled Resources

### Scripts
- `scripts/vector_extractor.py` - PDF text extraction with bboxes (pdfplumber)
- `scripts/merge_vlm_classifications.py` - Merge VLM outputs with consensus
- `scripts/validate_extraction.py` - Tag validation and YAML output

### Templates
- `templates/vlm-classification-prompt.md` - VLM classification prompt

### Schemas
Reuses shared schemas (no new schemas created):
- `~/skills/shared/schemas/loop.schema.yaml`
- `~/skills/instrument-io-skill/schemas/instrument-database.schema.yaml`

## Limitations

- **Graph building out of scope**: No topology/connectivity extraction
- **Raster PDFs**: Require OCR (not implemented)
- **VLM accuracy**: ~40-55% hallucination rate on engineering drawings (mitigated by multi-model consensus + hard validators)
