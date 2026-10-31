"""The `taste-gate` skill: one approved page before any redesign fan-out."""

NAME = "taste-gate"

TEXT = r"""---
name: taste-gate
description: Build ONE representative page or component of a redesign, show it, and get approval before changing anything else. Use whenever a redesign, restyle, new layout, density change or dashboard change would touch more than one page - before handing the rest to other agents or a batch.
---
# Taste gate

A redesign applied to twelve pages before anyone has liked one page is twelve pages of rework. Build one, get a yes,
then spread it.

1. **Gather the brief.** Check the repo's instructions, register (TODO.md) and any earlier design notes for rulings that
   already settle the direction. If it is not settled, ask the design questions in one batch. For a dashboard, first ask
   for the 3-5 charts or views the person actually uses; research shapes the details but never replaces their list.
2. **Write the brief in their words.** Quote what they said, and keep every image or example they reacted to. Set the
   fidelity bar with a reference image, not adjectives. For a highly visual look, study the best existing work, reuse
   only what its licence allows, build a first pass outside the product on real data, and compare screenshots to the
   reference for at least three rounds. Offer options for the person to pick. Never call a look beautiful yourself: their
   eye decides.
3. **Build the most representative page** fully, with real data and real interactions.
4. **Screenshot it** at a wide desktop width (about 1440 px) and a phone width (about 390 px). Look for what is cramped,
   misaligned or noisy, and fix what you agree with. If your runner has a design or density critic subagent, run it;
   otherwise check any charts against the `dataviz` skill's checklist.
5. **Show the person** the screenshots and 3-5 lines on the choices made. **Stop and wait for approval.**
6. **On approval,** write the approved rules as a short "visual kit" section in the repo's design doc or instructions
   file (colours, type scale, spacing, density, chart rules) so anyone applying them to the remaining pages can do it
   mechanically. Then do the other pages.
"""
