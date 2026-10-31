"""The `align` skill: get on the same page about the why before the what."""

NAME = "align"

TEXT = r"""---
name: align
description: Get the person and the agent on the same page about the WHY before the WHAT, using plain elicitation techniques (jobs to be done, the Mom Test, pre-mortems). Run it as step 1 of any large or ambiguous piece of work (new build, redesign, architecture, release, reporting), and again when answers seem to contradict the goals. Also use it when asked to "align", "get on the same page", or "ask me the nitty gritty".
---
# Align

Most rework comes from a why hiding in the background, not a wrong what. The goal: you could make the person's next ten
decisions the way they would, and you know which decisions are theirs. They decide outcomes, users, trade-offs and the
limits of their world. You decide engineering.

## 1. Read what is written

- Sources: the goals or north star (README, the agent instructions file, the repo's register such as TODO.md, a
  decisions file), earlier answers, and the request.
- Never ask what is already answered there. If there is no north star, draft one in 7 lines or fewer.

## 2. Plan the round: decisions first, never a quota

A question exists only because a real decision depends on its answer.

1. List the **forks**: places where two answers lead to two different plans, using today's facts (numbers, evidence,
   the options actually on the table).
2. For each fork, write down the default plan and your **predicted answer**, and how sure you are.
3. **Ask only** the forks where you cannot predict the answer, or where the stakes (money, security, their time,
   something hard to undo) justify confirming even a confident prediction. **Decide the rest yourself** and list each as
   "assumed: X, because of your answer to Y", so they can veto it.
4. Shape each question for its fork: name the decision it drives, use the real specifics, offer concrete options with
   your recommendation first. Zero questions is a valid round: say what you will assume and go.

## 3. Techniques (optional; use one only when it sharpens a question)

- **Last time:** "Think of the last one you handled: what did you look at first, and what did you do next?"
- **Next action:** "After they have seen this, what do they do with it?"
- **Trigger and workaround:** "What made this worth doing now, and what do you do today instead?"
- **Why-ladder:** "If this worked perfectly, what would be different next month, and why would that matter?"
- **Solution or problem:** "Is X what you need, or a way to get something else? What is the something else?"
- **Tolerance:** "How long, how many, or how bad before it hurts?"
- **Done test:** "What would you check, and on what, to say this worked?"
- **Pre-mortem:** "It is three months on and nobody uses this: what is the most likely reason?"
- **Trade-off:** "If only one shipped first, which?" or "You said X; here is its cost: still X?"
- **Veto map:** "Who must accept this, who is affected, who can stop it?"
- **Tension:** when two answers pull against each other, ask about the tension itself.

## 4. Check every question

1. Answerable from their own job, with no technical knowledge.
2. About what happens or what they do, not what they want.
3. Names the decision it drives.
4. Not already known from the repo or earlier answers.
5. Not a solution in disguise: the options lead to different plans.
6. One thing per question; under 30 seconds to answer; no lookups.
7. Not leading, and never asks for sensitive data.
8. Not ritual: if you asked this shape of question last time, the fork must be genuinely the same.

## 5. Ask and challenge

- Ask in one batch, with previews for anything visual. Four questions at most per batch.
- If an instruction does not serve the goals, or they may not know what it entails, say so plainly with the reason and
  a better option. Accept their answer if they hold their position.
- If a sketch, a prototype or one real case walked through would be clearer than words, propose it.

## 6. Turn answers into outputs

1. **Record on receipt.** Put their words verbatim in the repo's register and commit that turn. Write each answer as a
   numbered decision saying what it changes (screen, store, test, brief), or "confirmed, no change". Update the north
   star if it moved. Nothing that matters lives only in the chat.
2. **Diff against the default plan:** scope, order, acceptance criteria, architecture, non-goals. If it is unclear
   whether a change is material, a subagent (if your runner has one) sees only the two plans and the answer, and says.
3. **Keep a small log** wherever the repo keeps notes: date, question, did it change the plan, was your prediction
   right. A high miss rate means ask more; a low one over many rounds means ask less.
4. **Check two weeks later.** Was a decision reversed? Was rework traced to a wrong assumption? Did they say "you
   should have asked X"? Log those as missed questions.

## 7. Loop until confident

If answers reveal a gap or a tension, run one more short round. Stop when the next question would not change the plan,
then decide as the person would and note the assumption.
"""
