# Gemini P&ID Review Prompt

## Purpose

This prompt instructs Gemini to **review** Claude's P&ID classifications against the actual page image. Gemini acts as a **quality reviewer**, not an independent classifier - it catches errors and omissions in Claude's work.

## Role: Reviewer, Not Classifier

Gemini's job is to:
1. **Verify** each of Claude's classifications against the image
2. **Report discrepancies** (wrong, missing, corrected)
3. **Capture evidence** for disagreements
4. **NOT** reclassify everything from scratch

This is more efficient and catches errors that consensus would miss.

---

## Prompt Template

```
# P&ID Classification Review Task

You are reviewing Claude's P&ID entity classifications against this page image.
Your role is REVIEWER - find errors and omissions, don't reclassify everything.

## Claude's Classifications for Page {PAGE_NUMBER}

{CLAUDE_CLASSIFICATIONS_JSON}

## Your Review Task

Compare each of Claude's classifications against what you see in the image.
Report ONLY discrepancies - do not repeat correct classifications.

### Discrepancy Types

1. **WRONG**: Claude's classification is incorrect
   - Wrong tag_type (equipment vs instrument)
   - Wrong equipment_type or instrument_type
   - Wrong variable or functions
   - Context mismatch (e.g., TS labeled "Temperature" but "TRQ" visible nearby)

2. **MISSING**: Tag visible in image but NOT in Claude's list
   - Only report if clearly visible as equipment or instrument tag
   - Include your classification for the missing tag

3. **MISSING_LINE**: Piping line number visible but not in Claude's list
   - Line numbers in format: SIZE-SERVICE-SEQ-CLASS (e.g., "2-PW-001-A1")
   - Include from/to equipment if visible

4. **MISSING_VALVE_SYMBOL**: Valve symbol visible without tag in Claude's list
   - Ball valves, gate valves, check valves, butterfly valves
   - Note valve type and associated piping/equipment

5. **CORRECTED**: Minor correction needed
   - Better description available from callout text
   - Nearby equipment association visible
   - Additional context clues found

### Output Format

Return JSON:

```json
{
  "page": {PAGE_NUMBER},
  "review_summary": {
    "total_in_claude_list": 15,
    "verified_correct": 12,
    "discrepancies_found": 3
  },
  "discrepancies": [
    {
      "tag": "TS-01",
      "issue": "WRONG",
      "claude_said": {
        "tag_type": "instrument",
        "instrument_type": "Temperature Switch",
        "variable": "T"
      },
      "correction": {
        "tag_type": "instrument",
        "instrument_type": "Torque Switch",
        "variable": "M",
        "service_description": "Clarifier Bridge Torque"
      },
      "evidence": "Text 'TRQ' visible next to tag bubble in image",
      "confidence": "high"
    },
    {
      "tag": "SM-02",
      "issue": "WRONG",
      "claude_said": {
        "tag_type": "instrument",
        "instrument_type": "Speed Monitor",
        "variable": "S"
      },
      "correction": {
        "tag_type": "equipment",
        "equipment_type": "Static Mixer",
        "equipment_code": "AG",
        "description": "In-line static mixer"
      },
      "evidence": "Symbol is clearly a static mixer element, not an instrument bubble",
      "confidence": "high"
    },
    {
      "tag": "PG-02",
      "issue": "MISSING",
      "claude_said": null,
      "correction": {
        "tag_type": "instrument",
        "instrument_type": "Gauge",
        "variable": "P",
        "functions": ["G"],
        "service_description": "Discharge Pressure",
        "nearby_equipment": "EQ-WW-PUMP"
      },
      "evidence": "Visible in image at pump discharge - pressure gauge bubble with PG over 02",
      "confidence": "high"
    },
    {
      "tag": "MBR-BLOWER",
      "issue": "CORRECTED",
      "claude_said": {
        "description": "Blower"
      },
      "correction": {
        "description": "MBR Air Blowers, Positive displacement (lobe), 2 Nos: 1W + 1S"
      },
      "evidence": "Equipment callout box shows full specification",
      "confidence": "high"
    },
    {
      "tag": "2-PW-001-A1",
      "issue": "MISSING_LINE",
      "claude_said": null,
      "correction": {
        "tag_type": "line",
        "line_number": "2-PW-001-A1",
        "size_nominal": "2\"",
        "service_code": "PW",
        "service": "Process Water",
        "pipe_class": "A1",
        "from_equipment": "200-P-01",
        "to_equipment": "200-TK-02"
      },
      "evidence": "Line number visible on piping run between pump and tank",
      "confidence": "medium"
    },
    {
      "tag": "BV-UNNAMED-01",
      "issue": "MISSING_VALVE_SYMBOL",
      "claude_said": null,
      "correction": {
        "tag_type": "valve",
        "valve_type": "Ball",
        "valve_subtype": "Isolation Valve",
        "actuator_type": "Manual",
        "on_line": "2-PW-001-A1",
        "nearby_equipment": "200-P-01"
      },
      "evidence": "Ball valve symbol visible on pump discharge line, no tag",
      "confidence": "low"
    }
  ]
}
```

## Review Guidelines

### Context Checks (CRITICAL)
For these ambiguous tags, always check nearby context:

| Tag Pattern | Check For | If Found |
|------------|-----------|----------|
| TS-XX | "TRQ", "TORQUE", "N.m" | Torque Switch, not Temperature |
| SM-XX | Static mixer symbol | Equipment, not Speed Monitor |
| EJ-XX | Dosing/injection context | Ejector equipment |
| PS-XX | "PRESS", "PSI", "BAR" | Pressure Switch |

### What NOT to Report
- Correct classifications (don't repeat them)
- Minor formatting differences
- Differences in canonical_tag suggestions (not a discrepancy)
- Tags that are genuinely unclear in the image

### Confidence Levels
- **high**: Clear evidence in image
- **medium**: Reasonable inference from context
- **low**: Uncertain, needs human review

### If No Discrepancies Found
Return:
```json
{
  "page": {PAGE_NUMBER},
  "review_summary": {
    "total_in_claude_list": 15,
    "verified_correct": 15,
    "discrepancies_found": 0
  },
  "discrepancies": []
}
```
```

---

## CLI Usage

### Basic Review (Single Page)

```bash
# Review Claude's classifications for page 1
gemini "Review Claude's P&ID classifications against this image.

Claude's classifications:
$(cat classifications_claude.json | jq '.[] | select(.page == 1)')

Report ONLY discrepancies (WRONG, MISSING, CORRECTED).
Output JSON with discrepancies array." @page_001.png -o json > gemini_review_page_001.json
```

### Full Review (All Pages)

```bash
# Loop through all pages
for page in page_*.png; do
  page_num=$(echo $page | grep -o '[0-9]\+')
  gemini "Review Claude's P&ID classifications for page $page_num.

Claude's classifications:
$(cat classifications_claude.json | jq ".[] | select(.page == $page_num)")

Report discrepancies only. Output JSON." @$page -o json > gemini_review_$page_num.json
done

# Merge reviews
jq -s 'add' gemini_review_*.json > gemini_review.json
```

---

## Integration with apply_review.py

The Gemini review output feeds into `scripts/apply_review.py`:

```python
# apply_review.py loads both files and merges:
# - WRONG: Updates Claude's entry with correction, flags review_required
# - MISSING: Adds to review_queue (not main output)
# - CORRECTED: Updates Claude's entry (no review flag)

merged = apply_gemini_review(
    claude_data=load_json("classifications_claude.json"),
    gemini_review=load_json("gemini_review.json")
)
```

---

## Why Reviewer Instead of Independent Classifier?

1. **Efficiency**: Gemini only reports differences, not everything
2. **Catches Edge Cases**: Gemini can catch what Claude missed
3. **No Consensus Paradox**: Independent classifiers often disagree on different things, making merge complex
4. **Evidence Trail**: Every correction has explicit evidence
5. **Review Queue**: Missing items go to human review, not auto-accepted

---

## Expected Improvements

With Gemini as reviewer, we catch:
- TS-01 = Torque Switch (TRQ visible)
- SM-02 = Static Mixer (equipment symbol)
- EJ-01/02 = Ejector (dosing context)
- PG-02, PG-12, TIT-01, PSH-01 = Missing tags
- Duplicate tag detection (different context on each occurrence)
