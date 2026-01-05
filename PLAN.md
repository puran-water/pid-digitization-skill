# Plan: pid-digitization-skill

## Summary

Create a Tier 0 foundational skill that extracts process engineering data from PDF P&IDs using multi-model VLM consensus (Claude + Gemini). Outputs all Tier 1 YAML artifacts for legacy project onboarding.

## OSS Research Findings (Extended)

### P&ID-Specific Tools Found

| Tool | Source | Features | Maturity |
|------|--------|----------|----------|
| [Azure P&ID Digitization](https://github.com/Azure-Samples/digitization-of-piping-and-instrument-diagrams) | Microsoft ISE | YOLOv5 symbol (50+), Hough lines, NetworkX graphs, ~80% accuracy | Reference impl |
| [pid_reader](https://github.com/jgaz/pid_reader) | jgaz | YOLO symbols, DEXPI export, Graph DB | Early stage |
| [PID_Symbol_Detection](https://github.com/mgupta70/PID_Symbol_Detection) | mgupta70 | Two-stage YOLO + few-shot, SAHI for large images | Published 2024 |
| [Automated-PnID-Symbol-Detection](https://github.com/aneeshbhattacharya/Automated-PnID-Symbol-Detection-and-Labelling) | aneeshbhattacharya | 6 symbol classes, EAST+MMOCR, PDF reports | Prototype |
| [Roboflow PID Connect](https://universe.roboflow.com/pid-connect/p-id-symbols) | Roboflow | 1065 images, pre-trained model API | Dataset+API |

### Vector PDF Parsing Tools

| Tool | Stars | Capabilities | P&ID Value |
|------|-------|--------------|------------|
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 9,448 | Lines, rects, curves, text with bboxes | **High** - extract tags/lines from vector PDFs |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | 6,000+ | `get_drawings()`, `cluster_drawings()`, vector paths | **High** - comprehensive vector extraction |

### Research & Datasets

| Resource | Description |
|----------|-------------|
| [PIDQA Dataset](https://github.com/mgupta70/PIDQA) | 64,000 QA pairs, 500 P&ID sheets, 32 symbols |
| Digitize-PID (Microsoft) | Synthetic dataset, 50+ symbols with annotations |

### Codex Critique Summary

**Critical Finding**: Docling is strong for document text/layout but **does NOT extract P&ID symbols, line connectivity, or loop logic**. For diagrammatics you need:
1. Symbol detection (YOLO-based or VLM)
2. Line/pipe detection (Hough transform or vector parsing)
3. Graph reconstruction (NetworkX + Shapely)

**Conclusion**: Replace docling-centric approach with **hybrid vector-first + VLM architecture**:
1. **pdfplumber/PyMuPDF** for vector PDF text/line extraction
2. **Claude VLM** for semantic understanding and symbol recognition
3. **Gemini CLI** for cross-validation
4. **NetworkX + Shapely** for graph construction
5. **DEXPI validation** via existing engineering-mcp-server

## Architecture (Revised per Codex + OSS Research)

```
PDF P&ID (multi-page)
        │
        ▼
┌───────────────────────────────────────────────────────┐
│  Stage 1: Vector-First Extraction (pdfplumber/PyMuPDF)│
│  - Extract text labels with bboxes                    │
│  - Extract line/curve primitives (vector PDFs)        │
│  - Render page images for raster processing           │
└───────────────────────────────────────────────────────┘
        │
        ├──────────────────────────────┐
        ▼                              ▼
┌───────────────────────┐     ┌───────────────────────┐
│  Stage 2a: Claude VLM │     │  Stage 2b: Gemini CLI │
│  (Native - no script) │     │  (gemini -p "@page")  │
│  - Symbol recognition │     │  - Cross-validation   │
│  - Tag-equipment map  │     │  - Disambiguation     │
└───────────────────────┘     └───────────────────────┘
        │                              │
        ▼                              ▼
┌─────────────────────────────────────────────────────┐
│  Stage 3: Graph Construction (NetworkX + Shapely)    │
│  - Symbols → Nodes                                   │
│  - Lines/pipes → Edges                               │
│  - Spatial proximity matching                        │
│  - Flow direction propagation                        │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  Stage 4: Validation (Hard Rules)                    │
│  - ISA tag regex (scripts/decode_isa_tag.py)         │
│  - DEXPI schema validation                           │
│  - Cross-page duplicate detection                    │
│  - Equipment-instrument linkage checks               │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  Stage 5: Review Queue (Obsidian)                    │
│  - Structured payload: {page, bbox, crop, models}    │
│  - Validator failures                                │
│  - Model disagreements                               │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  Output: YAML Artifacts with Provenance              │
│  - equipment-list.yaml  (source_page, bbox, conf)    │
│  - instrument-database.yaml                          │
│  - process-topology.yaml (graph edges)               │
│  - loops.yaml                                        │
└─────────────────────────────────────────────────────┘
```

### Key Architecture Decisions (per Codex)

| Decision | Recommendation | Rationale |
|----------|----------------|-----------|
| **Standalone vs MCP** | MCP-dependent with fallback | Reuse existing infra, avoid 2GB+ duplication |
| **Scripts vs Markdown** | **Hybrid** | Scripts for deterministic steps, SKILL.md for orchestration |
| **VLM execution** | **Claude native + gemini CLI** | Claude Code IS the consumer - use native VLM directly |
| **Confidence scoring** | Hard validators, not model self-reports | Tag regex, DEXPI checks, cross-page consistency |

## Output Artifacts (Tier 1)

| Artifact | Schema Source | Consuming Skills |
|----------|---------------|------------------|
| `equipment-list.yaml` | equipment-list-skill | instrument-io, control-philosophy |
| `instrument-database.yaml` | instrument-io-skill | control-philosophy, io-list |
| `process-topology.yaml` | New schema | bfd-skill, mass-balance-skill |
| `loops.yaml` | instrument-io-skill | control-philosophy |

## Implementation Steps

### 1. Create skill directory structure (Revised)
```
~/skills/pid-digitization-skill/
├── SKILL.md                              # Orchestration + VLM prompts
├── scripts/
│   ├── vector_extractor.py               # pdfplumber/PyMuPDF extraction
│   ├── graph_builder.py                  # NetworkX + Shapely graphs
│   ├── validate_extraction.py            # ISA tag + DEXPI validation
│   └── review_queue_exporter.py          # Obsidian export
├── schemas/
│   ├── extraction-result.schema.yaml     # Output with provenance
│   └── process-topology.schema.yaml      # Graph edges schema
├── templates/
│   ├── vlm-extraction-prompt.md          # Claude/Gemini prompt
│   └── review-queue-template.md          # Obsidian format
├── references/
│   ├── isa-symbol-catalog.yaml           # ISA 5.1 symbols
│   └── extraction-patterns.md            # P&ID extraction guidance
└── assets/
    └── catalogs/
        └── equipment-symbols.yaml        # Equipment type mappings
```

### 2. Core Scripts (Hybrid Approach)

**vector_extractor.py** - Stage 1: Vector-first extraction
- Uses pdfplumber or PyMuPDF (fitz)
- Extracts text labels with precise bboxes
- Extracts line/curve primitives from vector PDFs
- Renders page images for VLM processing
- Returns structured anchors:
  ```python
  {
    "page": 1,
    "text_labels": [
      {"text": "200-P-01", "bbox": [x1, y1, x2, y2], "font_size": 8},
      {"text": "PERMEATE PUMP", "bbox": [x1, y1, x2, y2]}
    ],
    "lines": [
      {"start": [x1, y1], "end": [x2, y2], "stroke_width": 2}
    ],
    "page_image": "path/to/page_001.png"
  }
  ```

**graph_builder.py** - Stage 3: Graph construction
- Uses NetworkX for graph structure
- Uses Shapely for spatial proximity matching
- Connects symbols (nodes) via lines (edges)
- Propagates flow direction from arrows
- Outputs topology as edge list

**validate_extraction.py** - Stage 4: Hard validation
- ISA tag regex validation (reuse decode_isa_tag.py)
- DEXPI schema checks
- Cross-page duplicate detection
- Equipment-instrument linkage verification
- Returns pass/fail per entity with error details

**review_queue_exporter.py** - Stage 5: Structured review
- Generates Obsidian note with structured payloads
- Includes page crops, model outputs, validation errors
- Markdown checklist format for human review

### VLM Execution (Markdown Instructions in SKILL.md)

Per Codex recommendation: **No claude_extractor.py script needed**. Since Claude Code IS the consumer:
- SKILL.md contains structured prompts for Claude's native VLM
- Claude Code reads page images directly via `Read` tool
- For Gemini cross-validation, use bash:
  ```bash
  gemini -p "Extract equipment and instruments from this P&ID page. Output JSON. @page_001.png"
  ```

### 3. Extraction prompt structure

```yaml
# For each P&ID page, extract:
equipment:
  - tag: "200-P-01"
    type: "centrifugal_pump"
    description: "Permeate Pump"
    location_bbox: [x1, y1, x2, y2]
    confidence: 0.95

instruments:
  - tag: "200-FIT-01"
    variable: "F"
    functions: ["I", "T"]
    loop_number: 1
    equipment_tag: "200-P-01"  # Associated equipment
    confidence: 0.92

piping:
  - from_equipment: "200-P-01"
    to_equipment: "200-TK-01"
    line_number: "200-PW-4\"-SS"
    confidence: 0.88

control_loops:
  - loop_key: "200-F-01"
    instruments: ["200-FIT-01", "200-FIC-01", "200-FV-01"]
    confidence: 0.85
```

### 4. Integration with existing skills

**Upstream**: None (Tier 0 - foundational)

**Downstream consumers**:
- `equipment-list-skill` - consumes `equipment-list.yaml`
- `instrument-io-skill` - consumes `instrument-database.yaml`
- `bfd-skill` - consumes `process-topology.yaml`
- `control-philosophy-skill` - consumes all artifacts

**Handoff contract**:
- All tags follow ISA 5.1 conventions
- Equipment types map to `feeder_types.yaml` catalog
- Instruments include `loop_key` for IO pattern generation

### 5. Review queue workflow

Items with confidence < 0.8 written to Obsidian note:
```markdown
# P&ID Extraction Review - [Project Name]

## Low Confidence Items

### Equipment
- [ ] **200-P-01** (conf: 0.72) - Type unclear: pump or compressor?
  - Claude: centrifugal_pump
  - Gemini: reciprocating_compressor
  - Context: [link to page image]

### Instruments
- [ ] **200-AIT-01** (conf: 0.65) - Analyte unclear: pH or conductivity?
  - Claude: pH
  - Gemini: conductivity
```

## Files to Create (Revised)

| File | Purpose |
|------|---------|
| `~/skills/pid-digitization-skill/SKILL.md` | Skill documentation + VLM prompts + orchestration |
| `~/skills/pid-digitization-skill/scripts/vector_extractor.py` | pdfplumber/PyMuPDF text+line extraction |
| `~/skills/pid-digitization-skill/scripts/graph_builder.py` | NetworkX + Shapely graph construction |
| `~/skills/pid-digitization-skill/scripts/validate_extraction.py` | ISA tag + DEXPI validation |
| `~/skills/pid-digitization-skill/scripts/review_queue_exporter.py` | Obsidian structured review |
| `~/skills/pid-digitization-skill/schemas/extraction-result.schema.yaml` | Output schema with provenance |
| `~/skills/pid-digitization-skill/schemas/process-topology.schema.yaml` | Graph edge schema |
| `~/skills/pid-digitization-skill/templates/vlm-extraction-prompt.md` | VLM prompt (used by Claude + Gemini) |
| `~/skills/pid-digitization-skill/templates/review-queue-template.md` | Obsidian review format |

## Dependencies (Revised)

```bash
pip install pdfplumber pymupdf pillow pyyaml jsonschema networkx shapely
# Gemini CLI must be installed and configured
```

**Existing infrastructure to leverage**:
- `~/processeng/engineering-mcp-server/` - DEXPI tools for validation
- `~/skills/instrument-io-skill/scripts/decode_isa_tag.py` - ISA tag validation
- Obsidian MCP - Review queue integration

**Optional (for YOLO-based symbol detection)**:
```bash
pip install ultralytics  # YOLOv8
pip install sahi         # Sliced inference for large images
```

## Why Not Docling?

Per Codex critique: Docling is excellent for **document AI** (text, tables, layout) but **not designed for engineering diagrams**:
- ❌ Does not extract P&ID symbols (valves, instruments, equipment)
- ❌ Does not detect line connectivity or piping networks
- ❌ Does not understand ISA symbology
- ❌ Cannot build process topology graphs

**Better alternatives**:
- **pdfplumber/PyMuPDF**: Extract vector text + lines directly (no ML needed for vector PDFs)
- **Claude/Gemini VLM**: Semantic understanding of P&ID symbols and relationships
- **NetworkX + Shapely**: Graph construction from spatial relationships

## Validation

After extraction:
1. Run `validate_extraction.py` to check tag formats
2. Cross-reference equipment tags with instruments
3. Verify loop_key references resolve
4. Check for orphaned instruments (no equipment association)

## Risks and Mitigations (per Codex)

| Risk | Severity | Mitigation |
|------|----------|------------|
| VLMs converge on wrong inference | High | Use hard validators + disagreement-first logic |
| Topology unreliable without graph reconstruction | High | Implement NetworkX + Shapely graph building |
| No evaluation metrics | High | Create labeled test set from Kalol P&ID |
| Sensitive data to external APIs | Medium | Use Claude native (no external), Gemini only for validation |
| VLM input strategy unclear for tiny text | Medium | Use pdfplumber text extraction first |
| Multi-model VLM per page is expensive | Medium | Skip Gemini when validators pass |

## Next Steps After Approval

1. Create skill directory structure at `~/skills/pid-digitization-skill/`
2. Implement `vector_extractor.py` using pdfplumber/PyMuPDF
3. Write SKILL.md with VLM prompts for Claude native + Gemini CLI
4. Implement `graph_builder.py` with NetworkX + Shapely
5. Implement `validate_extraction.py` (reuse decode_isa_tag.py)
6. Create review queue exporter for Obsidian
7. **Test on Kalol P&ID as reference case** - establish baseline accuracy

## Sources

- [Microsoft ISE P&ID Digitization Blog](https://devblogs.microsoft.com/ise/engineering-document-pid-digitization/)
- [Azure P&ID Digitization Sample](https://github.com/Azure-Samples/digitization-of-piping-and-instrument-diagrams)
- [PID_Symbol_Detection (mgupta70)](https://github.com/mgupta70/PID_Symbol_Detection)
- [pdfplumber](https://github.com/jsvine/pdfplumber)
- [PyMuPDF Drawing Extraction](https://pymupdf.readthedocs.io/en/latest/recipes-drawing-and-graphics.html)
- [Roboflow P&ID Symbols Dataset](https://universe.roboflow.com/pid-connect/p-id-symbols)
