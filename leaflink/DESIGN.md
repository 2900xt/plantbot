---
name: LeafLink Doodle
description: A handwritten field notebook for recorded observations and image review.
colors:
  ink: "#263d5b"
  text: "#172538"
  muted: "#5f6873"
  paper: "#f9f6ef"
  surface: "#fffdf8"
  peach: "#ead0c4"
  blue: "#49b6e5"
  line: "#d8d2c7"
  cv: "#23759a"
  voc: "#ad5e18"
  both: "#44745a"
  sim: "#85638c"
  skull: "#b9bdc1"
  skull-outline: "#5d6570"
  skull-detail: "#4f5965"
  cv-marker: "#bce4f5"
  voc-marker: "#f3d59c"
  simulation-marker: "#d9c8e1"
typography:
  headline:
    fontFamily: "Delius, 'Segoe UI', sans-serif"
    fontSize: "34px"
    fontWeight: 400
    lineHeight: 1.2
    letterSpacing: "-.025em"
  title:
    fontFamily: "Delius, 'Segoe UI', sans-serif"
    fontSize: "28px"
    fontWeight: 400
    lineHeight: 1.25
  body:
    fontFamily: "Delius, 'Segoe UI', sans-serif"
    fontSize: "17px"
    lineHeight: 1.7
  metadata:
    fontFamily: "'JetBrains Mono', monospace"
    fontSize: "11px"
rounded:
  button: "4px 8px 5px 6px"
  tab: "8px 15px 7px 10px"
  map-panel: "10px 19px 9px 14px"
  input: "4px 7px 4px 6px"
spacing:
  map-padding: "16px"
  panel-gap: "32px"
  shell-padding: "0 40px"
components:
  button-primary:
    backgroundColor: "{colors.peach}"
    textColor: "{colors.text}"
    rounded: "{rounded.button}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.text}"
    rounded: "{rounded.button}"
    padding: "8px 16px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.input}"
    padding: "10px 12px"
  tab-active:
    backgroundColor: "{colors.peach}"
    textColor: "{colors.text}"
    rounded: "{rounded.tab}"
    padding: "11px 24px"
  map-panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.map-panel}"
    padding: "16px"
  zoom-button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    width: "48px"
    height: "48px"
---

# Design System: LeafLink Doodle

## Overview

**Creative North Star: "The handwritten field notebook"**

The user-approved paper sketch determines the workspace composition; the supplied SketchPad screenshot determines cream paper, dark ink outlines, peach controls and lightly irregular corners. The public TypeUI Doodle guide supplies handwritten type and supporting vocabulary. This describes the implemented direction, not a separate concept approval.

This system governs `app/field.css`, `app/index.html`, `app/field-map.js` and `app/field.js`. It replaces the earlier forest-green dashboard identity. Historical `/capture.html` retains independent styling. Source CSS is authoritative; the frontmatter extracts implemented values rather than defining new ramps or a universal spacing scale.

**Key Characteristics:**

- Cream dotted paper and handwritten headings.
- Dark outlined controls with peach emphasis and short solid offset shadows.
- A spacious map beside compact, independently collapsible signal lists.
- Explicit observation provenance, measurement labels and keyboard access.

## Colors

Peach emphasizes actions; warm paper and near-white surfaces carry dark text and navy drawing strokes. `blue` is the public guide's retained sky-blue token; its presence does not prescribe large-area use. Secondary text and input placeholders use `muted`.

CV uses blue marker fill and `cv` text; VOC uses amber fill and `voc` text. **Both uses a gray skull**, gray legend dot and dark facial details. The retained green `both` token and earlier green marker rule do not define the final visible skull. Purple dots and labels identify simulation, including mixed observations. Warm thin lines separate lists and metadata.

**The Observation Rule.** Empty space means unobserved. A triangle indicates one signal modality; a gray skull indicates CV and VOC in the displayed cell or grouped area. Neither marker diagnoses disease. Simulation remains explicitly marked.

## Typography

The CSS family `Delius` loads self-hosted **Delius Swash Caps** regular; JetBrains Mono carries times, status, counts, code and scale measurements. Font files and OFL licenses ship locally. Preserve normal handwritten weight 400 rather than synthesizing bold.

Page headings are 34px (29px on phones), section headings 28px and tertiary headings 23px. Body paragraphs use 17px/1.7 with at most 72ch. Tabs are 20px (18px on phones); general controls are 17px. Mono metadata varies by task: status 11px, counts and scale 12px, event times 10px. These are observed roles, not a generated type scale.

## Layout

The centered shell caps at 1550px with 40px side padding. Overview uses `minmax(0,1fr) 288px`, a 32px gap and top alignment. The map is the principal work surface; Detection % and separate VOC/CV lists sit beside it. **Field Overview and Images** are the primary tabs. Sensor ingestion and advanced rules remain secondary under Field setup; Connection stays separately available.

At 1100px, shell padding becomes 24px, sidebar 260px and gap 24px. Between 761px and 850px, overview stacks with anomaly lists side by side. At 760px and below, shell padding is 16px, overview stacks, tabs share the row, and the map uses a 400:470 aspect ratio with 365px minimum height. Gallery columns shift from three to two; candidate thumbnails shift from six to two. Dense tables scroll locally.

Desktop map height is `clamp(360px,47vh,540px)`. Fit field and visible 48px +/− controls occupy the bottom right. Support drag-pan, explicit fit and fullscreen where available; keep the useful canvas clear of secondary copy.

## Elevation & Depth

Use ink outlines and short **solid offset** shadows: primary action 2px 3px, tabs 1px 2px, map panel 2px 4px, zoom buttons 1px 2px. Avoid blur and ambient card glow. Dotted paper repeats at 20px; the map has a separate fine grid. Full-photo viewing uses a dark translucent backdrop.

## Shapes

Use the extracted asymmetric corner sets and 1.5–2px ink borders. Map corners are broader than control corners; zoom buttons have irregular rounded outlines. Keep routes, circular GPS fixes, one-signal triangles and gray Both skulls legible without extra distress.

## Components

- **Actions and inputs:** peach primary actions, paper secondary actions, persistent labels and near-white fields. General controls have 44px minimum height; zoom controls are 48px square. Hover changes the surface; active buttons move down 1px. Focus uses a 3px `cv` outline with 5px offset. Disabled actions show a waiting cursor and reduced opacity. Placeholders use `muted` for readable contrast.
- **Navigation:** native pressed buttons show peach active state, irregular borders and short shadows. Sections use the native hidden attribute. Keep setup screens secondary.
- **Map and signals:** same-device GPS trails join recorded positions and break across excessive time gaps. Missing positions stay unmapped. All/CV/VOC/Both radio filters are explicit. Markers support click details, double-click linked photo, Enter to open and Space for details, plus an Open image button. Preserve marker focus during refresh.
- **Anomaly mini-preview:** hover or keyboard focus opens a map-anchored paper-and-ink card without filtering the anomaly panels. Show the authenticated linked thumbnail, infected candidate share, VOC rise/units and time when available, plus simulation labels. Use explicit Loading photo, Photo preview unavailable and No photo linked fallbacks. On a map marker, click/tap or Space pins the preview and selects the area; entering the card or focusing its controls keeps it open. Escape/X dismiss it and return focus to the originating marker or sidebar row. Open image retains the existing map-return behavior. The card is 238px wide with 14px padding, a 116px contained thumbnail, existing solid shadow and irregular corners; maximum width/height are `calc(100% - 24px)` with internal scrolling. Position beside the marker, flip sides when needed and clamp within the map with 12px clearance. A 220ms leave delay permits movement into the card; map changes dismiss stale previews. Sidebar VOC/CV row hover or keyboard focus opens this same map preview for that exact individual event, rather than its grouped map area. Space/Enter on the row pins it without changing pan, zoom or filters. A linked CV event shows its photo and candidate share; VOC shows readings with a no-photo fallback. Missing coordinates explicitly say Location unavailable.
- **Detection and lists:** Detection % is infected candidate share, not model confidence. Default is 10%; the usual slider range is 10–50% and expands to include stored settings. Changes apply to new photos; old percentage and count snapshots remain valid. Separate native VOC/CV details retain collapsed state during polling.
- **Images and return:** gallery tiles open source photos and candidate predictions. Image-detail X, full-photo X and full-photo Escape restore previous map center, zoom, signal/area filters and collapsed panels; scroll and marker focus return when available. Geometry changes invalidate incompatible saved map views.
- **Feedback:** retain authentic disconnected, empty, running, blurry, failed, awaiting-model and missing-CUDA states. Notices use a polite status region, native progress is labelled, and errors have warm red styling. Simulation stays explicit in events, gallery and image details.

Background `.15s ease-out` transitions and tab hover rotation apply only when reduced motion is not requested. Ordinary button press translation remains a direct state cue.

## Do's and Don'ts

### Do:

- **Do** follow the cream, ink, peach and handwritten reference hierarchy.
- **Do** keep the gray Both skull distinct from one-signal triangles.
- **Do** preserve visible focus, readable placeholders and explicit simulation labels.
- **Do** preserve the previous map context when closing an image.

### Don't:

- **Don't** restore the superseded green dashboard identity.
- **Don't** describe Detection % as confidence or modify old capture snapshots.
- **Don't** present unobserved space or VOC anomalies as a disease diagnosis.
- **Don't** replace solid offset shadows with blurred elevated cards.


