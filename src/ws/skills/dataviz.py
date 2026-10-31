"""The `dataviz` skill: dense, honest, accessible charts and tables. Written for ws; not a copy of anyone else's."""

NAME = "dataviz"

TEXT = r"""---
name: dataviz
description: Make charts, tables, dashboards and stat tiles that read fast and tell the truth - Tufte-style density, direct labels, one accent per meaning, light and dark, colour-blind safe. Use before writing any plotting code, choosing chart colours, laying out a dashboard or KPI row, or reviewing a chart someone else made.
---
# Dataviz

A chart earns its place by answering one question faster than a sentence or a table would. Start from the question,
then pick the form, then take out everything that is not data.

## 1. Pick the form from the question

| The question | Use | Avoid |
| --- | --- | --- |
| How much, compared across a few things? | Horizontal bars, sorted, from zero | Pie, 3D, gauges |
| How has it moved over time? | Line (a few series) or a sparkline in a table row | Bars for dense time series |
| Is it ahead or behind a target? | Bar or dot with a target tick, or the number with a delta | Speedometer dials |
| How are the parts split? | One stacked bar, or a sorted bar of the parts | Donut with more than 4 slices |
| What is the spread or the odd one out? | Dot strip, histogram or box plot | Mean alone |
| Do two measures move together? | Scatter, with a labelled outlier or two | Dual-axis lines |
| Many categories over time, or many groups? | Small multiples with a shared scale | One chart with 12 lines |
| I need the exact values | A table | A chart with every value labelled |

Fewer than about 20 numbers and no pattern to show? Write them as a table or a sentence.

## 2. Make it dense and quiet

- **Data-ink first.** Delete anything that carries no information: borders, background fills, gridlines you do not
  read off, tick marks on every value, shadows, gradients, 3D, a legend that repeats the labels.
- **Label directly.** Put the series name at the end of its line and the value on the bar. A legend means the eye
  travels and returns.
- **Titles say the finding** ("Spend is 40% over plan since June"), not the contents ("Spend by month"). The subtitle
  gives the unit and the period.
- **Small multiples** beat overlays: same axes and scale in every panel, ordered by something meaningful.
- **Axes:** bars start at zero; lines may not, but say so. Never truncate or break an axis to flatter a result. Do not
  use a second y-axis. Use plain units (k, M, %) and few ticks.
- **Tables:** right-align numbers, use the same decimals down a column, units in the header not in every cell, light
  row rules or none, sort by the column that matters, bold at most one thing per row. Sparklines belong beside numbers.
- **Annotate** the one or two events that explain the shape (a launch, an outage, a rule change), at the point.
- **Show uncertainty** when it exists: an interval, a range or "n = 14". Mark estimates and forecasts differently
  from actuals (dashed, lighter).

## 3. Colour: one accent per meaning

- Use grey for context and one accent colour for the thing the reader should look at. A second hue only when it means
  something different (a target, a forecast, a comparison group). Keep the same meaning the same colour everywhere.
- Colour that carries meaning also needs a second channel: a label, a position, a line style or a marker shape. About
  1 in 12 men cannot tell red from green; never use that pair as the only signal.
- For categories, use at most 5-6 hues that stay distinct in a colour-blind simulation and in greyscale. Beyond that,
  group, filter or use small multiples.
- Sequential data: one hue, light to dark (dark = more). Diverging data (above and below a midpoint): two hues meeting
  at a light neutral, centred on the real midpoint. Do not use a rainbow scale.
- Check contrast: text and thin lines at least 4.5:1 against the background (3:1 for large text and bars).
- **Light and dark:** define colours as named roles (text, muted, grid, accent, positive, negative) and give each a
  light and a dark value; do not invert by filter. On dark backgrounds use slightly lighter, less saturated accents,
  thinner grid lines, and off-white text rather than pure white.

## 4. Type and layout

- One typeface; tabular (fixed-width) digits for numbers so columns line up. Labels at least 11 px (8 pt on paper).
- Align to a grid; keep gutters equal. Put the most important number top left, big, with its label and delta in
  smaller text under it.
- Stat tile: the value, the unit, the comparison (vs last period or target) and its direction. No icon, no frame
  unless the tile is clickable.
- A dashboard answers 3-5 questions the owner asks every week. If a panel does not change a decision, remove it.
- Make it work at a phone width: stack panels, drop gridlines, keep direct labels.

## 5. Pre-flight checklist

Before you show a chart or table, tick each of these:

1. [ ] I can state the one question it answers, and the title states the answer.
2. [ ] The form matches the question (section 1); a table would not be better.
3. [ ] Bars start at zero; no dual axis; no truncated or broken axis.
4. [ ] Units, period and source are on the chart; estimates are marked as such.
5. [ ] Series and key values are labelled directly; no legend where labels would do.
6. [ ] No chartjunk: no 3D, shadows, gradients, heavy borders or decorative icons.
7. [ ] One accent colour for the point; the same colour means the same thing throughout.
8. [ ] It reads in greyscale and under red-green colour-blindness; contrast is at least 4.5:1 for text.
9. [ ] It looks right in light and dark, and at phone width.
10. [ ] Numbers are right-aligned with consistent decimals; the table is sorted on the column that matters.
11. [ ] Uncertainty or sample size is shown where it exists.
12. [ ] I opened the real rendered output (not just the code) and read it as a stranger would.

## 6. Code notes (any library)

- Set the roles once (a small theme: font, sizes, colours for text, muted, grid, accent, positive, negative) and use it
  for every chart, so they read as one system.
- Export vector (SVG or PDF) for documents and a 2x PNG for slides; set the size you need rather than scaling after.
- Keep the data in a file or a query the chart is rebuilt from; never hand-edit the picture.
- Give the chart alt text that states the finding in one sentence.
"""
