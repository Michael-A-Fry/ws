---
status: accepted
date: 2026-10-31
decision-makers: maintainer
---

# Detect the operating system and ask as little as possible at install

## Context and Problem Statement

A person installing ws has a macOS, Linux or Windows machine. Asking which one, or asking many setup questions up front,
loses people before they see a result.

## Considered Options

- Ask the person for their system and every preference during install.
- Detect the system, install with what's there (uv or pipx), and ask a few questions afterwards in `ws setup`.

## Decision Outcome

Chosen option: detect the system. ws supports macOS and Linux, and Windows through WSL2; native Windows isn't
supported, and `ws doctor` says so and points at WSL2. The installer takes the latest release, checks its checksum and
refuses to install when a check fails. `ws setup` asks the few questions that change what ws writes, and shows the plan
before anything is written.

### Consequences

- Good: one command gets a working install, and a failed check stops it.
- Good: a new machine reaches `ws doctor` in minutes.
- Bad: native Windows users must install WSL2 first.

### Confirmation

`tests/test_install_sh.py` runs the installer's own checks. Doctor check WS001 reports the platform (see
[Doctor checks](../reference/doctor-checks.md)). `tests/test_windows_import.py` keeps every module importable on native
Windows, so the doctor can run and say what to do.
