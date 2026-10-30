---
schema: clarification-answers/v1
status: submitted
generated_at: "2026-10-28T17:09:00.237Z"
scope: [frontend, generic]
questions_file: clarification-questions.json
---

# Rearchitecture Clarification Answers

## 🖥️ Frontend

- **F1** Confirm the frontend framework and version to retain for this modernization.
  - answer: React (existing project version)
  - source: user
- **F2** Which component/UI library should the refreshed frontend use? You may choose to retain the project's existing component and CSS approach rather than add a library.
  - answer: Retain existing project components and CSS; add no UI library
  - source: user
- **F3** Provide screenshot/screen-recording paths or URLs for the current UI, or confirm that the implementation should use the existing frontend itself as its visual reference.
  - answer: Use the existing frontend UI/CSS in the workspace as the reference; no screenshot files supplied.
  - source: user
- **F4** Specify the design-system/tokens source, or confirm the default: retain and refine existing CSS custom properties and visual language.
  - answer: Refine the existing CSS/custom properties into a cohesive, polished CampusGo visual system without replacing existing branding or inventing features.
  - source: user
- **F5** Which accessibility standard should the refreshed UI target?
  - answer: WCAG 2.1 AA (including reduced-motion behavior, keyboard focus, and accessible states)
  - source: user
- **F6** Which browsers/runtime versions should be supported?
  - answer: modern evergreen (Chrome, Firefox, Safari, Edge — latest 2 major versions)
  - source: user
- **F7** Which responsive/adaptive strategy should the UI follow?
  - answer: Responsive across desktop, tablet, and mobile, with existing breakpoints retained where appropriate
  - source: user
- **F8** Which locales are in scope?
  - answer: preserve current locales; keep existing i18n library if present
  - source: default applied
- **F9** State-management preference, if any?
  - answer: Preserve existing data/state integrations and patterns; do not introduce backend behavior changes.
  - source: user
- **F10** Routing library/strategy preference, if any?
  - answer: Preserve existing routes and URL behavior.
  - source: user

## 📋 General

- **G1** What defines completion?
  - answer: Deliver the requested frontend modernization while preserving actual backend APIs, authentication, payments, business behavior, and existing authorized role features; run the frontend build and backend tests.
  - source: user
- **G2** List explicit exclusions.
  - answer: Backend/API/business logic changes, invented shops or features, unauthorized role-specific actions, payment/auth changes, and commits.
  - source: user
- **G3** What is the must-pass policy for existing tests?
  - answer: Frontend build and backend tests must pass, as explicitly requested.
  - source: user
- **G5** Any additional constraints, dependencies, or operational/compliance rules?
  - answer: Do not commit. Return exact changed-file list and validation results. No assessment or planning phase.
  - source: user
