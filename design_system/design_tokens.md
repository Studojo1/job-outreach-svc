# Design Tokens

## 1. Color Palette

### Primary Modules
| Alias | Hex | Name | Usage |
|-------|-----|------|-------|
| `primary-purple` | `#7C3AED` | Violet 600 | Platform branding, Main Landing, Onboarding |
| `primary-green` | `#10B981` | Emerald 500 | Careers Module, Resume Builder |
| `primary-orange` | `#F97316` | Orange 500 | Revision Module, AI Writing tools |

### Neutral System
| Alias | Hex | Name | Usage |
|-------|-----|------|-------|
| `bg-page` | `#F9FAFB` | Gray-50 | Global page background |
| `bg-card` | `#FFFFFF` | White | Content containers, Modals |
| `text-primary` | `#111827` | Slate-900 | Headings, Primary body text |
| `text-secondary` | `#4B5563` | Gray-600 | Subheadings, Labels, Helper text |
| `border-light` | `#E5E7EB` | Gray-200 | Card strokes, Separators |

### Feedback
| Alias | Hex | Name | Usage |
|-------|-----|------|-------|
| `status-success` | `#10B981` | Emerald 500 | Checkmarks, Success badges, Done state |

---

## 2. Typography

### Font Family
- **Primary**: `Inter`, `Montserrat`, or `System Sans-Serif`.

### Heading Scale
| Level | Font Size | Weight | Usage |
|-------|-----------|--------|-------|
| `H1` | `42px` | `Bold` | Page Titles, Hero Headlines |
| `H2` | `28px` | `Bold` | Section Headers |
| `H3` | `20px` | `Bold` | Component Titles, Card Headers |

### Body Scale
| Level | Font Size | Weight | Usage |
|-------|-----------|--------|-------|
| `Body-Large` | `16px` | `Regular` | Primary reading text |
| `Body-Small` | `14px` | `Regular` | Metadata, Secondary info |
| `Label` | `12px` | `Bold` | Overlines, Status badges |

---

## 3. Spacing Scale
Based on a 4px baseline.

| Token | Value | Literal |
|-------|-------|---------|
| `XS` | `4px` | `0.25rem` |
| `S` | `8px` | `0.50rem` |
| `M` | `16px`| `1.0rem` |
| `L` | `24px`| `1.5rem` |
| `XL`| `32px`| `2.0rem` |
| `XXL`| `48px`| `3.0rem` |

---

## 4. Border & Shadow

### Border Radius
- **Small (`br-s`)**: `4px` (Inputs, Small buttons)
- **Medium (`br-m`)**: `8px` (Standard components)
- **Large (`br-l`)**: `12px` - `16px` (Cards, Hero CTA buttons)

### Shadows
- **Shadow-Soft**: `0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06)`
- **Shadow-Elevated**: `0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)`
