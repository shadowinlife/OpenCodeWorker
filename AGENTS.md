# AGENTS.md

## Purpose
This repository is intended to build a specialized worker for vibe-trading using:
- opencode
- oh-my-opencode
- pluggable MCP servers and SKILLS

The core goal is to support complex quantitative research and investment analysis workflows with reusable, automation-friendly components.

## Working Mode For AI Coding Agents
- Prioritize correctness over speed for quant or investment logic.
- Keep every change auditable: clear assumptions, data source boundaries, and reproducible steps.
- Prefer small, composable modules over large one-off scripts.
- When requirements are unclear, ask for missing constraints before implementing financial logic.

## Test Environment Dependencies
Use this default environment unless the user explicitly requests otherwise.

1. OS
- linux-based environment (WSL, Docker, or native)

2. Python runtime
- Conda environment: `legonanobot`
- Run Python with: `conda run -n legonanobot python <script.py>`

3. Tooling assumptions
- zsh shell available
- Project may rely on MCP servers and skill definitions; validate availability before wiring integrations

## Basic Workflow
Follow this baseline loop for tasks in this repository.

1. Clarify task scope
- Confirm strategy goal, asset scope, time horizon, and risk constraints.

2. Inspect current workspace state
- Identify existing MCP/skills/instructions and any reusable components.

3. Implement in small increments
- Add or update one focused unit at a time.
- Avoid broad refactors unless explicitly requested.

4. Validate locally
- Run targeted checks or scripts in the `legonanobot` environment.
- Report what was validated and what was not validated.

5. Summarize outcomes
- Provide changed files, key behavior changes, and remaining risks/assumptions.

## Guardrails For Quant/Investment Tasks
- Never fabricate market data, backtest results, or performance metrics.
- Explicitly label assumptions and data limitations.
- Separate data collection, feature/signal logic, and execution/risk controls.
- Prefer deterministic scripts and config-driven parameters for repeatability.

## Expansion Guidance
As the repository grows, add focused customizations under `.github/`:
- `.github/instructions/*.instructions.md` for language or folder specific rules
- `.github/skills/<skill-name>/SKILL.md` for repeatable multi-step workflows
- `.github/agents/*.agent.md` for specialized subagents with constrained tools
