# EventLens Web Design System

## 0. Research Log

- Embedded refs: shortlisted Claude, Wired, ClickHouse; picked `taste-skill` + Claude because EventLens starts as an article-contract product and benefits from warm editorial clarity.
- Lazyweb: skipped because task restricts tools and asks for scaffold, not final UI research.
- Imagen drafts: skipped because task restricts tools and this shell is not reference-fidelity work.

## 1. Atmosphere & Identity

Quiet editorial infrastructure. Warm paper surfaces make event data feel legible rather than mechanical. Signature: thin terracotta rules marking cited evidence.

## 2. Color

| Role | Token | Value | Usage |
|---|---|---|---|
| Page | `--color-canvas` | `#f3f1e8` | Main background |
| Surface | `--color-surface` | `#faf9f5` | Content panels |
| Ink | `--color-ink` | `#191916` | Primary text |
| Muted | `--color-muted` | `#66645d` | Supporting text |
| Faint | `--color-faint` | `#8b887f` | Metadata |
| Border | `--color-border` | `#dcd8cc` | Dividers |
| Accent | `--color-accent` | `#b94f32` | Active foundation state |
| Accent soft | `--color-accent-soft` | `#ead7ce` | Accent backing |
| Placeholder | `--color-placeholder` | `#b5b0a4` | Not-yet-connected state |
| Focus | `--color-focus` | `#1769aa` | Keyboard focus |

Accent signals current state only. No decorative color additions.

## 3. Typography

| Level | Token | Size | Weight | Line Height | Usage |
|---|---|---|---|---|---|
| Display | `--type-display` | `clamp(2.75rem, 8vw, 6rem)` | 500 | 0.96 | Product name |
| H2 | `--type-heading` | `1.5rem` | 500 | 1.2 | Section titles |
| Body large | `--type-body-large` | `1.125rem` | 400 | 1.6 | Intro |
| Body | `--type-body` | `1rem` | 400 | 1.6 | Copy |
| Small | `--type-small` | `0.875rem` | 500 | 1.45 | Metadata |
| Label | `--type-label` | `0.75rem` | 600 | 1.3 | Status labels |

- Display: Georgia, Cambria, serif.
- Body: Avenir Next, Avenir, sans-serif.
- Mono: SFMono-Regular, Consolas, monospace.

## 4. Spacing & Layout

Base unit: 4px.

| Token | Value |
|---|---|
| `--space-1` | `0.25rem` |
| `--space-2` | `0.5rem` |
| `--space-3` | `0.75rem` |
| `--space-4` | `1rem` |
| `--space-6` | `1.5rem` |
| `--space-8` | `2rem` |
| `--space-12` | `3rem` |
| `--space-16` | `4rem` |

Content max width: `72rem`. Desktop uses asymmetric two-column grid; below `48rem`, one column. Page gutters use fluid browser mechanics bounded by spacing tokens.

## 5. Components

### Query Panel

- **Structure**: display heading + labelled `textarea` + hint + submit button inside a `<form>`.
- **States**: idle; invalid (inline `role="alert"`, `aria-invalid`, described-by link, no request sent); loading (button reads "Briefing…").
- **Layout**: asymmetric 3fr/2fr grid; one column below `48rem`.

### Briefing Result

- **Structure**: outcome tag row, coverage warning, answer (or abstention notice), ordered claims with nested citations, uncertainty list, retrieval audit `<dl>`.
- **Variants**: `answered`, `abstained_no_evidence`, `abstained_by_model`. Abstentions never render under the "Answer" heading.
- **States**: loading (`role="status"`, `aria-busy`), failed (`role="alert"` with code/HTTP/attempt and Retry when retryable), malformed response (error only; nothing rendered as an answer).
- **Accessibility**: every state tag carries text plus a glyph (✓ ! ×); accent color only reinforces it.

### Citation

- **Structure**: source title, name · domain · published time, cited excerpt `<blockquote>`, "Inspect source" toggle (`aria-pressed`, `aria-controls`), external "Open original" link.

### Source Viewer

- **Structure**: `<aside id="source-viewer">` beside the result on desktop (sticky), stacked below on narrow screens.
- **States**: loading, open (cited span in `<mark>` with screen-reader start/end markers), failed (`source_not_found`, `source_stale`, others).
- **Keyboard**: focus moves to the viewer heading on open; Escape or "Close source" closes and returns focus to the citation button.

### Accent usage

Accent marks the current/attention state: active citation, warning/error tags, cited span underline, claim index.

## 6. Motion & Interaction

Only motion: the loading bar slide (`background-position`), disabled under `prefers-reduced-motion`.

## 7. Depth & Surface

Borders-only strategy. `--border-thin: 1px`; `--radius-panel: 1.5rem`; `--radius-pill: 999px`. No shadows.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA, body contrast at least 4.5:1, semantic landmarks, no color-only state.
- Responsive content must remain readable at 320px without horizontal page scroll.

### Accepted Debt

None.
