---
name: pid-digitization-skill
description: |
  Extract equipment, instruments, and control loops from PDF P&IDs for legacy project onboarding.
  Tier 0 foundational skill that produces YAML artifacts for downstream skills.
  Use when: (1) Digitizing legacy P&ID drawings, (2) Extracting equipment/instrument lists from PDFs,
  (3) Creating instrument database from existing drawings, (4) Onboarding projects without DEXPI XML.
  Outputs: equipment-list.yaml, instrument-database.yaml (with loops), review-queue.yaml.
---

# P&ID Digitization Skill

Extract equipment and instrument entities from PDF P&IDs using vector text extraction and VLM classification with Gemini review.

## Workflow

```
PDF P&ID (multi-page)
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 1: Vector Extraction (vector_extractor.py)│
│ - Classify PDF: vector vs raster                │
│ - Extract text labels with bboxes               │
│ - Cluster multi-line text (PG + 02 → PG-02)     │
│ - 3-tier tag pattern matching                   │
│ - Render page images for VLM                    │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 2a: Claude Classification (Agent executes)│
│ - Classify candidates from extraction           │
│ - Capture context clues (nearby text, symbols)  │
│ - Suggest canonical_tag for non-standard tags   │
│ - Output: classifications_claude.json           │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 2b: Gemini Review (Agent executes)        │
│ - Review Claude's classifications against image │
│ - Report WRONG, MISSING, CORRECTED discrepancies│
│ - Evidence-based corrections with confidence    │
│ - Output: gemini_review.json                    │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 2c: Apply Review (apply_review.py)        │
│ - Apply Gemini corrections to Claude's output   │
│ - MISSING items → review_queue                  │
│ - WRONG items → update + flag review_required   │
│ - Output: merged_results.json                   │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Stage 3: Validation (validate_extraction.py)    │
│ - Page coverage check (warns if incomplete)     │
│ - Computed confidence scoring                   │
│ - Duplicate detection and flagging              │
│ - Required field validation by device class     │
│ - Cross-reference validation                    │
│ - Project config disambiguation rules           │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────┐
│ Output: YAML Artifacts                          │
│ - equipment-list.yaml                           │
│ - instrument-database.yaml (includes loops)     │
│ - line-list.yaml (piping lines)                 │
│ - valve-schedule.yaml (control/on-off valves)   │
│ - validation-report.yaml                        │
│ - review-queue.yaml (VLM-discovered items)      │
└─────────────────────────────────────────────────┘
```

## Setup

Create skill-local venv (one-time):

```bash
cd ~/skills/pid-digitization-skill
python3 -m venv .venv
.venv/bin/pip install pdfplumber pillow pyyaml jsonschema pymupdf
```

Gemini CLI must be installed separately:
```bash
# See: https://github.com/google-gemini/gemini-cli
npm install -g @anthropic-ai/gemini-cli
```

## Quick Start

### 1. Extract from PDF

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/vector_extractor.py \
  /path/to/pid.pdf
```

Output (auto-derived from PDF path):
- `{pdf_folder}/{pdf_name}/page_NNN.png` - Page images for VLM
- `{pdf_folder}/{pdf_name}/extraction_results.json` - Text labels with bboxes

### 2. Claude Classification (Agent Executes)

**CRITICAL: Claude MUST classify ALL P&ID pages, not just a sample.**

Skipping pages results in Gemini flagging those items as MISSING, which floods the review queue with false positives. For a 23-page P&ID, Claude must read and classify all 23 pages.

**Systematic page-by-page workflow:**

1. Get page count from extraction_results.json
2. For EACH page (skip only title/legend pages 1-3):
   - Read the page image
   - Get tag_candidates from extraction_results.json for that page
   - Classify all equipment, instruments, valves, and lines
   - Add to classifications_claude.json
3. Verify coverage: every P&ID page should have classifications

```
For each P&ID page image (page_004.png through page_NNN.png):
1. Read the page image
2. Classify tag candidates from extraction_results.json for this page
3. Discover additional items (lines, valves) visible in the image
4. Follow templates/vlm-classification-prompt.md
5. Append to classifications_claude.json with page number

Output JSON structure:
{
  "classifications": [...],      // All classified equipment/instruments
  "discovered_items": [...]      // Lines, valves, missed items
}
```

**Coverage validation:** Before running Gemini review, verify that classifications_claude.json has entries for all P&ID pages. Missing pages will result in inflated review queues.

### 3. Gemini Review (Automated or Manual)

Run Gemini review on ALL pages automatically:

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/gemini_review_all.py \
  /path/to/output_dir --timeout 180
```

Or for manual single-page review:

```bash
gemini "Review Claude's P&ID classifications against this image.

Claude's classifications:
$(cat classifications_claude.json | jq '.[] | select(.page == 1)')

Report ONLY discrepancies (WRONG, MISSING, CORRECTED).
Follow templates/gemini-review-prompt.md.
Output JSON." @page_001.png > gemini_review.json
```

### 4. Apply Review

Merge Gemini's corrections into Claude's output:

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/apply_review.py \
  classifications_claude.json gemini_review.json \
  --extraction extraction_results.json \
  --output merged_results.json --stats
```

Note: The `--extraction` flag carries forward the `pages[]` structure from vector extraction, enabling line list extraction in the validation step.

### 5. Validate & Output

```bash
~/skills/pid-digitization-skill/.venv/bin/python \
  ~/skills/pid-digitization-skill/scripts/validate_extraction.py \
  merged_results.json --config project_config.yaml
```

Output:
- `equipment-list.yaml` - Equipment database
- `instrument-database.yaml` - Instrument database with loops
- `line-list.yaml` - Piping line list (if lines detected)
- `valve-schedule.yaml` - Valve schedule (control valves, on/off valves)
- `validation-report.yaml` - Summary statistics (includes page coverage)
- `review-queue.yaml` - VLM-discovered items needing human QA

Items with `review_required: true` need human verification.

**Page Coverage Check:** The validation step checks that all P&ID pages have classifications. If pages are missing, a warning is printed and included in validation-report.yaml. Configure `skip_pages` in project_config.yaml to exclude title/legend pages from this check.

## Project Configuration

Copy `project_config_template.yaml` to your output folder and customize:

```yaml
project_id: "EXAMPLE-WTP-001"

# Force abbreviations to equipment (not instrument)
equipment_abbreviations:
  - SM   # Static Mixer (not Speed Monitor)
  - EJ   # Ejector (not Expansion Joint)

# Pages to skip from coverage validation (title pages, legends)
skip_pages:
  - 1    # Title page
  - 2    # Table of contents
  - 3    # Legend/symbol key

# Context-based disambiguation
context_rules:
  - pattern: "^TS-\\d+$"
    context_check:
      nearby_text: ["TRQ", "TORQUE", "N.m"]
    if_found:
      instrument_type: "Torque Switch"
      variable: "M"
    else:
      instrument_type: "Temperature Switch"
      variable: "T"

# Manual corrections from QA review
manual_overrides:
  - original_tag: "TS-01"
    correction:
      instrument_type: "Torque Switch"
      variable: "M"
      override_reason: "QA: P&ID shows TRQ annotation"
```

## Confidence Scoring

Confidence is computed using bonuses and penalties (base: 0.50):

| Factor | Adjustment |
|--------|------------|
| Has bbox evidence | +0.20 |
| VLM consensus (Claude + Gemini agree) | +0.15 |
| Gemini verified | +0.10 |
| ISA-compliant tag | +0.10 |
| Context validated (disambiguation rule matched) | +0.05 |
| VLM disagreement (Gemini corrected) | -0.20 |
| No bbox (VLM-only) | -0.15 |
| Ambiguous abbreviation (unresolved) | -0.10 |
| Duplicate tag | -0.10 |
| Missing required field | -0.05 (per field) |

Review threshold: **0.80** - below this triggers `review_required: true`.

## Ambiguous Abbreviations

These tags require context checking:

| Pattern | Default | Context Check |
|---------|---------|---------------|
| TS-XX | Temperature Switch | If "TRQ" nearby → Torque Switch |
| SM-XX | Static Mixer (equipment) | If instrument bubble → Speed Monitor |
| EJ-XX | Ejector (equipment) | If piping context → Expansion Joint |
| PS-XX | Pressure Switch | If "POSITION" nearby → Position Switch |
| LS-XX | Level Switch | If "LIMIT" nearby → Limit Switch |

## Output Artifacts

### equipment-list.yaml

```yaml
project_id: "EXAMPLE-WTP-001"
equipment:
  - tag: "200-P-01"
    description: "Permeate Pump"
    area: 200
    equipment_type: "Pump"
    equipment_code: "P"
    kind: "equipment"
    feeder_type: "VFD"
    provenance:
      source_type: "pdf_extracted"
      page: 1
      confidence: 0.92
      bbox: [x1, y1, x2, y2]
```

### instrument-database.yaml

```yaml
project_id: "EXAMPLE-WTP-001"
revision:
  number: "A"
  date: "2026-01-05"
  by: "CLAUDE"

source_pids:
  - pid_number: "PID-001"
    source_type: "pdf_extracted"
    vlm_models: ["claude", "gemini"]

loops:
  - loop_key: "200-F-01"
    tag_area: 200
    loop_number: 1
    variable: "F"
    devices:
      - full_tag: "200-FIT-01"
        functions: ["I", "T"]
        role: "measurement"

instruments:
  - instrument_id: "uuid"
    loop_key: "200-F-01"
    tag:
      full_tag: "200-FIT-01"
      variable: "F"
      functions: ["I", "T"]
    equipment_tag: "200-P-01"
    service_description: "Permeate Flow"
    primary_signal_type: "4-20mA"
    provenance:
      confidence: 0.88
```

### line-list.yaml

Piping lines extracted from P&ID (per `line-list.schema.yaml`):

```yaml
project_id: "EXAMPLE-WTP-001"
lines:
  - line_number: "2-PW-001-A1"
    size_nominal: "2\""
    size_inches: 2.0
    service_code: "PW"
    service: "Process Water"
    pipe_class: "A1"
    from_equipment: "200-P-01"
    to_equipment: "200-TK-02"
    provenance:
      source_type: "pdf_extracted"
      page: 1
      confidence: 0.85
```

### valve-schedule.yaml

Valve schedule extracted from instruments with 'V' function (per `valve-schedule.schema.yaml`):

```yaml
project_id: "EXAMPLE-WTP-001"
valves:
  - valve_tag: "200-FV-01"
    valve_type: "Control"
    valve_subtype: "Flow Control"
    actuator_type: "Pneumatic"
    fail_position: "FC"
    signal_type: "4-20mA"
    service_description: "Permeate Flow Control"
    equipment_tag: "200-P-01"
    loop_key: "200-F-01"
    provenance:
      source_type: "pdf_extracted"
      confidence: 0.85
```

### review-queue.yaml

Items discovered by Gemini but not in vector extraction:

```yaml
review_queue:
  - tag: "PG-02"
    tag_type: "instrument"
    instrument_type: "Gauge"
    variable: "P"
    review_required: true
    review_reason: "Gemini-discovered (no vector evidence)"
    confidence: 0.55
    provenance:
      source_type: "vlm_discovered"
      discovered_by: "gemini"
```

## VLM Guidelines

### Claude (Primary Classifier)
- Classify pre-extracted candidates only
- Capture context clues (nearby text, symbols)
- Suggest canonical_tag for non-standard naming
- Emit "unknown" if insufficient evidence

### Gemini (Reviewer)
- Review Claude's work against the image
- Report ONLY discrepancies (WRONG, MISSING, CORRECTED)
- Provide evidence for each discrepancy
- Do NOT reclassify everything

See `templates/vlm-classification-prompt.md` and `templates/gemini-review-prompt.md`.

## Raster PDF Handling

If `vector_extractor.py` detects a raster/scanned page:
- Warning logged in extraction results
- Page image still rendered for VLM
- Text extraction may be incomplete
- Consider OCR preprocessing

## Integration

### Upstream
None (Tier 0 foundational skill)

### Downstream Consumers

| Skill | Consumes | Purpose |
|-------|----------|---------|
| `equipment-list-skill` | `equipment-list.yaml` | Equipment database |
| `instrument-io-skill` | `instrument-database.yaml` | IO list generation |
| `control-philosophy-skill` | Multiple artifacts | Control narratives |
| `bfd-skill` | `equipment-list.yaml` | Block flow diagrams |
| `line-list-skill` (future) | `line-list.yaml` | Piping line schedule |
| `valve-schedule-skill` (future) | `valve-schedule.yaml` | Control valve datasheets |

### Shared Schemas

| Schema | Purpose |
|--------|---------|
| `~/skills/shared/schemas/line-list.schema.yaml` | Line list extraction |
| `~/skills/shared/schemas/valve-schedule.schema.yaml` | Valve schedule extraction |
| `~/skills/shared/catalogs/equipment_designators.yaml` | Equipment codes + aliases |
| `~/skills/shared/catalogs/isa_5_1_instrument_letters.yaml` | ISA letter definitions |

### Handoff Contract

- Equipment tags: `{AREA}-{CODE}-{SEQ}` format (e.g., 200-P-01)
- Instrument tags: ISA-5.1 compliant `{AREA}-{VAR}{FUNC}-{LOOP}` (e.g., 200-FIT-01)
- Loop keys: `{AREA}-{VAR}-{LOOP}` format (e.g., 200-F-01)
- Canonical equipment codes per `equipment_designators.yaml`

## Standards Alignment

Reference standards for schema design, interoperability, and compliance:

| Standard | Application |
|----------|-------------|
| [ISA 5.1-2024](https://www.isa.org/standards-and-publications/isa-standards/isa-standards-committees/isa5-1) | Instrument identification and symbols |
| [IEC 62424](https://webstore.iec.ch/en/publication/25442) | Instrumentation documentation (P&ID representation) |
| [ISO 10628](https://www.iso.org/obp/ui/en/) | P&ID diagram conventions and requirements |
| [ISO 15926](https://15926.org/) / [DEXPI](https://reference.opcfoundation.org/DEXPI/v100/docs/7) | Lifecycle data integration for process plants |
| [PIP PIC001](https://www.document-center.com/standards/show/PIP-PIC001) | P&ID documentation criteria (EPC industry) |
| [API 6D Annex O](https://www.wermac.org/societies/api_part2.html) | Valve datasheet structure |

### ISA 5.1 Compliance

Tag formats follow ISA 5.1-2024:
- **First Letter**: Primary measured/initiating variable (F=Flow, L=Level, T=Temperature, etc.)
- **Succeeding Letters**: Readout/output functions (I=Indicate, T=Transmit, C=Control, V=Valve)
- **Tag Format**: `{AREA}-{VARIABLE}{FUNCTIONS}-{LOOP_NUMBER}` (e.g., 200-FIT-01)

Ambiguous abbreviations (TS, SM, EJ, PS, LS) are disambiguated using context rules and project configuration.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/vector_extractor.py` | PDF extraction with bbox clustering |
| `scripts/gemini_review_all.py` | Run Gemini review on all pages automatically |
| `scripts/apply_review.py` | Apply Gemini corrections to Claude output |
| `scripts/validate_extraction.py` | Validation and YAML output |
| `scripts/merge_vlm_classifications.py` | (Legacy) VLM consensus merge |

## Templates

| Template | Purpose |
|----------|---------|
| `templates/vlm-classification-prompt.md` | Claude classification prompt |
| `templates/gemini-review-prompt.md` | Gemini review prompt |

## Configuration Files

| File | Purpose |
|------|---------|
| `project_config_template.yaml` | Project-specific disambiguation rules |

## Dependencies

Skill-local venv at `.venv/`:

```bash
pdfplumber   # PDF text extraction
pillow       # Image rendering
pyyaml       # YAML output
jsonschema   # Schema validation
```

External:
- Gemini CLI for cross-validation

## Limitations

- **Graph building out of scope**: No topology/connectivity extraction
- **Raster PDFs**: Require OCR preprocessing (not implemented)
- **VLM accuracy**: ~40-55% hallucination on engineering drawings (mitigated by Gemini review + validation rules)
- **Line lists/valve schedules**: Schema defined but extraction not yet implemented
