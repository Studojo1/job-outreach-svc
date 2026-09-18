# Interaction Rules

## 1. Visual Feedbacks

### Hover States
- **Buttons**:
  - `transform: translateY(-2px)` for a subtle lift effect.
  - Background color shifts to a darker shade (`10%` reduction in lightness).
- **Cards**:
  - Border color shifts from `border-light` (`#E5E7EB`) to the module's primary color.
  - Shadow increases to `Shadow-Elevated`.

---

## 2. Core Animations

### Flashcard Flip
- **Trigger**: Hover.
- **Duration**: `600ms`.
- **Timing Function**: `Cubic-bezier(0.4, 0, 0.2, 1)`.
- **Visual**: Card rotates 180 degrees on the Y-axis to reveal the back face.

### Page Transitions
- **Onboarding Steps**: Smooth horizontal slide (`fade-in-slide-right`) when navigating between "Upload" -> "Calibrate" -> "Results".

---

## 3. Loading & Progress

### Loading Indicators
- **Type**: Pulsing skeletons or a spinning circular loader in the module's accent color.
- **Progress Bars**: Smooth, non-stepped animation that fills from left to right.

### Tool Workflow
Process indicators (1-2-3) should transition from an outlined state to a solid colored state as the user advances.
