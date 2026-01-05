# VLM Classification Prompt for P&ID Entity Extraction

## Purpose

This prompt instructs VLMs (Claude, Gemini) to **classify** pre-extracted text candidates from a P&ID page. VLMs act as **labelers**, not localizers - they classify entities that have already been detected via vector text extraction.

## Critical Constraints

1. **DO NOT** discover new entities - only classify candidates provided
2. **DO NOT** guess coordinates or bounding boxes
3. **Emit "unknown"** if insufficient visual evidence exists
4. **Base classifications on visual context** from the page image

---

## Prompt Template

Use this prompt with the page image and extracted candidates:

```
You are classifying entities extracted from a P&ID (Piping & Instrumentation Diagram) for a wastewater treatment plant.

## Task

For each candidate tag below, classify it based on the visual context from the attached P&ID page image. Do NOT discover new entities - only classify the candidates provided.

## Candidates to Classify

{CANDIDATES_JSON}

## Classification Instructions

For each candidate:

### Equipment Tags (format: XXX-YY-NN)
Identify:
- **equipment_type**: Tank, Pump, Blower, Mixer, Filter, Clarifier, Screen, UV, MBR, RO, etc.
- **description**: Brief service description visible on drawing
- **feeder_type**: DOL, VFD, SOFT-STARTER (if motor-driven equipment visible)
- **process_unit_type**: Best match from taxonomy (e.g., "secondary_treatment.aerobic_biological_treatment.aeration_tank")

### Instrument Tags (format: XXX-ABC-NN)
Identify:
- **instrument_type**: Transmitter, Controller, Switch, Valve, Indicator, Analyzer
- **service_description**: What it measures/controls (e.g., "Permeate Flow")
- **primary_signal_type**: 4-20mA, 24V DC, Modbus, HART (if visible)
- **analyte**: For analyzers (A variable) - pH, DO, Conductivity, TSS, Turbidity

### Unknown Tags
If you cannot determine the tag type or meaning from visual context:
- **tag_type**: "unknown"
- **reason**: Brief explanation

## Output Format

Return JSON array:

```json
[
  {
    "tag": "200-P-01",
    "tag_type": "equipment",
    "equipment_type": "Pump",
    "description": "Permeate Pump",
    "feeder_type": "VFD",
    "process_unit_type": "secondary_treatment.aerobic_biological_treatment.secondary_clarification.submerged_membrane_bioreactor",
    "confidence": "high"
  },
  {
    "tag": "200-FIT-01",
    "tag_type": "instrument",
    "instrument_type": "Transmitter",
    "service_description": "Permeate Flow",
    "primary_signal_type": "4-20mA",
    "confidence": "high"
  },
  {
    "tag": "200-XYZ-01",
    "tag_type": "unknown",
    "reason": "Cannot determine function from visual context",
    "confidence": "low"
  }
]
```

## Confidence Levels

- **high**: Clear visual evidence on diagram
- **medium**: Partial evidence, some inference required
- **low**: Minimal evidence, classification uncertain

## Evidence Gating

If NO visual evidence exists for a candidate (e.g., tag appears to be noise, unrelated text, or document metadata):
- Set tag_type to "noise" and explain
- Do NOT attempt to classify without evidence

```

---

## Usage in SKILL.md

### For Claude (Native VLM)

Claude Code reads the page image directly and receives candidates from vector extraction:

```markdown
Look at the attached P&ID page image.

Classify these extracted tag candidates:
{paste candidates JSON here}

Follow the classification instructions in templates/vlm-classification-prompt.md.
Output JSON array of classifications.
```

### For Gemini CLI (Cross-Validation)

```bash
gemini -p "$(cat templates/vlm-classification-prompt.md)

Candidates to classify:
$(cat extracted/tag_candidates.json)

Analyze the image and output JSON classifications." @extracted/page_001.png
```

---

## Process Unit Type Taxonomy Reference

For `process_unit_type`, use hierarchical paths from the process unit taxonomy:

| Category | Example Types |
|----------|---------------|
| preliminary_treatment | headworks.coarse_screening, headworks.grit_removal |
| primary_treatment | primary_clarification.gravity, primary_clarification.daf |
| secondary_treatment | aerobic_biological_treatment.aeration_tank, aerobic_biological_treatment.secondary_clarification.submerged_membrane_bioreactor |
| tertiary_treatment | filtration.ultrafiltration, filtration.reverse_osmosis |
| disinfection | uv, chlorination |
| sludge_treatment | thickening.gravity_thickener, dewatering.belt_filter_press |

---

## Feeder Type Reference

For motor-driven equipment:

| Feeder Type | Description |
|-------------|-------------|
| DOL | Direct On-Line starter |
| VFD | Variable Frequency Drive |
| SOFT-STARTER | Soft starter |
| VENDOR_PANEL | Vendor-supplied control panel |

---

## Notes

- This prompt is designed for **labeling, not localization**
- VLMs should not be asked to find new entities or determine precise geometry
- All candidates come from `vector_extractor.py` output
- Confidence scoring enables review queue filtering
