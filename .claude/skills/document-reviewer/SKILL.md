---
name: document-reviewer
description: Review project documentation for accuracy, consistency, and quality after code changes. Use when docs may be stale, after refactors, or before releases.
argument-hint: "[file-or-directory]"
context: fork
agent: general-purpose
allowed-tools: Read, Grep, Glob, Bash(git diff *), Bash(git log *), Bash(git show *)
---

# Documentation Reviewer

Review documentation for accuracy against the current codebase, internal consistency, and writing quality.

## Scope

**Target:** `$ARGUMENTS` (file path or directory; defaults to all committed `.md` files if omitted)

**Project context:**
- Key source files: !`ls podman-compose.yml Containerfile.scraper .env.example 2>/dev/null`
- Dashboard files: !`ls grafana/provisioning/dashboards/json/*.json 2>/dev/null`
- Documentation files: !`find . -name '*.md' -not -path './docs/plans/*' -not -path './.claude/*' -not -path './node_modules/*' | sort`
- Git status: !`git diff --name-only HEAD~5 2>/dev/null | head -20`

## What to check

### 1. Accuracy against code
- File paths, function names, CLI flags, env vars mentioned in docs -- do they exist in the code?
- Command examples -- do they match the current CLI interface?
- Architecture descriptions -- do they match the actual structure?
- Configuration options -- are all documented options real, and are real options documented?

### 2. Consistency and duplication
- Terminology used the same way throughout (don't flag correct synonyms, only confusing inconsistency)
- No contradictions between documents
- Cross-references between docs resolve correctly
- **Single source of truth**: information should live in one place. If two docs say the same thing, one is the source and the other should briefly mention it in context and link to the source rather than repeating it. Duplicated content drifts apart over time and creates contradictions. When flagging duplication, identify which doc is the natural home for that content.

### 3. Stale content
- References to removed features or old behavior
- Outdated version numbers, dates, or counts that will rot
- TODOs or placeholders that should have been resolved

### 4. Audience and scope
Every document has an intended audience and scope. Content that belongs elsewhere is clutter. Check that:
- **Architecture docs** explain big-picture design for developers and AI working with the codebase. They should NOT contain installation instructions (README), code-level detail (source code), or step-by-step procedures (CONTRIBUTING).
- **README** covers what-it-does and quickstart for new users. It should NOT contain system design rationale (ARCHITECTURE) or developer workflow (CONTRIBUTING).
- **CONTRIBUTING** covers how to build, test, and develop. It should NOT duplicate architecture or repeat the README.
- **Appendix/reference docs** cover specific technical details. They should NOT duplicate higher-level docs.

When content is in the wrong doc, suggest where it belongs rather than just flagging it.

### 5. Fragile specificity
Documentation that breaks on routine code changes is too specific. The test: if a simple refactor (renaming a function, adding a parameter, reordering steps) would make the doc wrong, it's at the wrong granularity.

Flag these patterns:
- Listing specific function/method names that will change as code evolves
- Discrete counts ("9 functions", "34 test files", "5 services") that go stale immediately
- Inline code examples that duplicate what's in the source -- prefer a reference link
- Step-by-step descriptions of implementation details that belong in code comments if anywhere

The fix is usually to describe *what* and *why* at a stable level rather than *how* at a code level. "The scraper converts every numeric field into a Prometheus metric" is stable. "The scraper calls `flatten_numeric_fields()` which yields tuples then calls `format_prometheus_line()`" will rot.

### 6. Document structure
Documents are read top-to-bottom by humans. Check that:
- **High-level first**: the document should open with context and purpose before getting into details. A reader should understand *what* and *why* before encountering *how*.
- **Related content grouped**: topics that belong together should be adjacent, not scattered across the document with unrelated sections in between.
- **No forward references**: don't reference a concept, term, or section before it's been introduced. If section 3 says "as described in the Architecture section" and that section is section 7, the reader hasn't seen it yet. Either reorder or briefly explain inline.
- **Progressive detail**: each section should go from general to specific. Summaries and overviews before implementation details and edge cases.

### 7. AI-targeted documents
Files like `CLAUDE.md`, skill definitions (`SKILL.md`), and reference appendices (`docs/appendix/`) are consumed primarily by AI. Every token in these files costs context window space and competes with the user's actual task. Review them with context engineering as the priority:

- **Density over readability**: favor compact, unambiguous statements over prose. Bullet points and terse instructions over flowing paragraphs. Humans can skim; AI processes every token.
- **Zero repetition**: duplicated instructions don't reinforce for AI, they waste context and risk contradiction when one copy gets updated and the other doesn't. If the same instruction appears twice, flag it -- even if the wording differs slightly.
- **Zero contradiction**: conflicting instructions in AI context cause unpredictable behavior. Two sections that say different things about the same topic is a high-severity issue, not a style nit.
- **Actionable over descriptive**: "use X" is better than "X is the recommended approach." "Never do Y" is better than "Y is discouraged." Direct instructions produce more reliable behavior.
- **Conditional instructions should be explicit**: "when X, do Y" is clear. "Consider doing Y" is ambiguous -- the AI doesn't know when to consider it.
- **No stale examples**: code examples in AI context files get followed literally. An outdated example produces outdated output. Prefer referencing source files over inline examples.
- **Flat structure**: AI doesn't benefit from progressive disclosure the way humans do. Front-load the most important instructions. Put constraints and rules before explanations of why.

All other checks in this skill still apply -- accuracy, consistency, scope, fragility, and structure problems are worse in AI context because there's no human judgment to compensate.

### 8. Writing quality
- Unclear or ambiguous instructions
- Missing context that a new reader would need
- Broken markdown (links, code blocks, tables)

## How to report

For each issue found, report:
- **File and line** (or section heading if line is ambiguous)
- **Category**: accuracy, consistency, stale, scope, fragility, structure, context-engineering, or quality
- **What's wrong**: brief description
- **Suggested fix**: specific text change, or "needs manual review" if you're unsure

Group findings by file. Lead with a summary count. Skip files with no issues.

## What NOT to do
- Don't rewrite docs or suggest style preferences
- Don't flag things that are subjective or taste-based
- Don't check grammar unless it changes meaning
- Don't report more than 3 low-confidence issues per file -- focus on what you're sure about
- Don't fabricate issues to fill a report -- "no issues found" is a valid outcome
