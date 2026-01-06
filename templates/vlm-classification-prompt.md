# VLM Classification Prompt for P&ID Entity Extraction

## Purpose

This prompt instructs VLMs (Claude, Gemini) to **classify** pre-extracted text candidates from a P&ID page. VLMs act as **labelers**, not localizers - they classify entities that have already been detected via vector text extraction.

## Critical Constraints

1. **Primary task**: Classify pre-extracted candidates (equipment, instruments)
2. **Secondary task**: Discover ADDITIONAL items missed by vector extraction (lines, valves)
3. **DO NOT** guess coordinates or bounding boxes for discovered items
4. **Emit "unknown"** if insufficient visual evidence exists for classification
5. **Capture context clues** (nearby text like "TRQ", equipment callouts)
6. **Suggest canonical tags** for non-standard naming

---

## Prompt Template

Use this prompt with the page image and extracted candidates:

```
You are classifying entities extracted from a P&ID (Piping & Instrumentation Diagram) for a wastewater treatment plant.

## Task

**Part 1: Classification**
For each candidate tag below, classify it based on the visual context from the attached P&ID page image.

**Part 2: Discovery**
After classifying all candidates, scan the image for ADDITIONAL items not in the candidates list:
- **Line numbers**: Piping line designations (e.g., "2-PW-001-A1", "4"-RW-005-B2")
- **Valve symbols**: Manual valves shown as symbols but NOT tagged in candidates (ball, gate, check valves)
- **Missed instruments**: Any instruments with visible tags not in candidates list

Add discovered items to a separate `discovered_items` array (see output format below).

## Candidates to Classify

{CANDIDATES_JSON}

## Classification Instructions

For each candidate:

### Equipment Tags
Identify:
- **tag_type**: "equipment"
- **original_tag**: Exactly as shown on P&ID
- **canonical_tag**: Suggested house-compliant format (see Canonical Codes below)
- **equipment_type**: Tank, Pump, Blower, Mixer, Filter, Clarifier, Screen, UV, MBR, RO, etc.
- **equipment_code**: From canonical codes (P, B, TK, SC, CL, AG, etc.)
- **description**: Brief service description visible on drawing
- **feeder_type**: DOL, VFD, SOFT-STARTER (if motor-driven equipment visible)

### Instrument Tags
Identify:
- **tag_type**: "instrument"
- **original_tag**: Exactly as shown on P&ID
- **canonical_tag**: Suggested house-compliant format (see Canonical Codes below)
- **instrument_type**: Transmitter, Controller, Switch, Valve, Indicator, Gauge, Analyzer
- **variable**: ISA first letter (F, L, P, T, A, etc.)
- **functions**: Array of succeeding letters (I, T, C, V, S, etc.)
- **service_description**: What it measures/controls (e.g., "Permeate Flow")
- **primary_signal_type**: 4-20mA, 24V DC, Modbus, HART (if visible)
- **analyte**: For analyzers (A variable) - pH, DO, Conductivity, TSS, Turbidity

### Context Capture (IMPORTANT)
For EVERY classification, note any contextual clues visible near the tag:
- **context_text**: Nearby text that helps identify function (e.g., "TRQ", "TORQUE", "FLOW", "LEVEL")
- **nearby_equipment**: Equipment tag this instrument is associated with
- **symbol_type**: Bubble (circle), hexagon, equipment symbol, etc.

### Unknown Tags
If you cannot determine the tag type or meaning from visual context:
- **tag_type**: "unknown"
- **reason**: Brief explanation

## Canonical Tag Codes

### Equipment Format: {area}-{code}-{seq}{suffix}
Pattern: `^[0-9]{3}-[A-Z]{1,5}-[0-9]{2,4}[A-Z]?$`
Example: 200-P-01, 100-SC-01, 300-TK-05

| Code | Name | Aliases |
|------|------|---------|
| P | Pump | PU |
| B | Blower | BL |
| AG | Agitator/Mixer | MX, AGT |
| TK | Tank | T |
| SC | Screen | SCR |
| CL | Clarifier | CLR |
| FLT | Filter | F, FILT |
| MBR | Membrane Bioreactor | - |
| RO | Reverse Osmosis | - |
| EJ | Ejector/Eductor | - |
| HX | Heat Exchanger | HE |
| V | Vessel | VES |

### Instrument Format: {area}-{letters}-{loop}{suffix}
Pattern: `^[0-9]{3}-[A-Z]{2,5}-[0-9]{2,4}[A-Z]?$`
Example: 200-FIT-01, 200-LT-05, 200-AIT-03

**First Letters (Measured Variable):**
| Letter | Variable |
|--------|----------|
| F | Flow |
| L | Level |
| P | Pressure |
| T | Temperature |
| A | Analysis (pH, DO, etc.) |
| S | Speed |
| H | Hand/Manual |
| X | Unclassified (on/off) |
| Z | Position |

**Succeeding Letters (Functions):**
| Letter | Function |
|--------|----------|
| I | Indicator |
| T | Transmitter |
| C | Controller |
| V | Valve |
| S | Switch |
| G | Gauge |
| A | Alarm |

**Switch Modifiers:**
| Modifier | Meaning |
|----------|---------|
| H | High |
| L | Low |
| HH | High-High |
| LL | Low-Low |

## Output Format

Return JSON object with TWO arrays:

```json
{
  "classifications": [
    {
      "tag": "200-P-01",
      "tag_type": "equipment",
      "original_tag": "200-P-01",
      "canonical_tag": "200-P-01",
      "equipment_type": "Pump",
      "equipment_code": "P",
      "description": "Permeate Pump",
      "feeder_type": "VFD",
      "context_text": null,
      "nearby_equipment": null,
      "confidence": "high"
    },
    {
      "tag": "PG-02",
      "tag_type": "instrument",
      "original_tag": "PG-02",
      "canonical_tag": "200-PG-02",
      "instrument_type": "Gauge",
      "variable": "P",
      "functions": ["G"],
      "service_description": "Discharge Pressure",
      "context_text": null,
      "nearby_equipment": "200-P-01",
      "confidence": "high"
    }
  ],
  "discovered_items": [
    {
      "tag": "2-PW-001-A1",
      "tag_type": "line",
      "line_number": "2-PW-001-A1",
      "size_nominal": "2\"",
      "service_code": "PW",
      "service": "Process Water",
      "pipe_class": "A1",
      "from_equipment": "200-P-01",
      "to_equipment": "200-TK-02",
      "evidence": "Line number visible on piping between pump and tank",
      "confidence": "medium"
    },
    {
      "tag": "BV-UNLABELED-01",
      "tag_type": "valve",
      "valve_type": "Ball",
      "valve_subtype": "Isolation Valve",
      "actuator_type": "Manual",
      "on_line": "2-PW-001-A1",
      "nearby_equipment": "200-P-01",
      "evidence": "Ball valve symbol visible on discharge line, no tag visible",
      "confidence": "low"
    },
    {
      "tag": "PG-05",
      "tag_type": "instrument",
      "instrument_type": "Gauge",
      "variable": "P",
      "functions": ["G"],
      "service_description": "Header Pressure",
      "nearby_equipment": "200-B-01",
      "evidence": "Pressure gauge visible on air header, not in candidates",
      "confidence": "medium"
    }
  ]
}
```

### Discovery Guidelines

**Line Numbers**: Look for text along piping runs in format like:
- `SIZE-SERVICE-SEQ-CLASS` (e.g., "2-PW-001-A1", "4-RW-005-B2")
- Size can have inch symbol: "2\"-PW-001"
- Common service codes: PW, RW, CW, AIR, SL, EFF, PERM, CONC

**Valve Symbols**: Look for valve symbols without tags:
- Ball valves (circle with line)
- Gate valves (triangle pair)
- Check/NRV valves (triangle with stem)
- Butterfly valves (circle with perpendicular line)

**Missed Instruments**: Any tagged instruments NOT in candidates list

## Confidence Levels

- **high**: Clear visual evidence on diagram
- **medium**: Partial evidence, some inference required
- **low**: Minimal evidence, classification uncertain

## Evidence Gating

If NO visual evidence exists for a candidate (e.g., tag appears to be noise, unrelated text, or document metadata):
- Set tag_type to "noise" and explain
- Do NOT attempt to classify without evidence

## Special Cases

### Ambiguous Abbreviations
Some abbreviations have multiple meanings. Check context:

| Tag | If Context Shows | Classification |
|-----|------------------|----------------|
| TS-XX | "TRQ", "TORQUE", "N.m" | Torque Switch (variable: M) |
| TS-XX | "TEMP", temperature symbol | Temperature Switch (variable: T) |
| SM-XX | Static mixer symbol | Equipment: Static Mixer |
| SM-XX | Instrument bubble | Speed Monitor (variable: S) |
| EJ-XX | Dosing/injection context | Equipment: Ejector |
| PS-XX | "PRESS", "PSI", "BAR" | Pressure Switch (variable: P) |
| PS-XX | Position/limit context | Position Switch (variable: Z) |

Always note the context that led to your classification.
```

---

## Usage

### For Claude (Native VLM)

Claude Code reads the page image directly and receives candidates from vector extraction:

```markdown
Look at the attached P&ID page image.

Classify these extracted tag candidates:
{paste candidates JSON here}

Follow the classification instructions in templates/vlm-classification-prompt.md.
Output JSON array of classifications with canonical_tag suggestions.
```

### For Gemini CLI

```bash
gemini "$(cat templates/vlm-classification-prompt.md)

Candidates to classify:
$(cat extracted/tag_candidates.json)

Analyze the image and output JSON classifications." @extracted/page_001.png -o json
```

---

## Notes

- This prompt is designed for **labeling, not localization**
- VLMs should not be asked to find new entities or determine precise geometry
- All candidates come from `vector_extractor.py` output (with multi-line clustering)
- Confidence scoring enables review queue filtering
- Context capture helps catch disambiguation errors
- Canonical tag suggestions enable downstream standardization
