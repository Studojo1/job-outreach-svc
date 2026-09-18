# Layout Rules

## 1. Grid & Containers

### Page Container
- **Max Width**: `1280px`.
- **Horizontal Centering**: All main content should be centered on the viewport.
- **Onboarding Screens**: Use a centered container constrained to `800px` for focus.

### Content Spacing
- **Vertical Stack**: Use `L` (`24px`) or `XL` (`32px`) spacing between major sections.
- **Section Grouping**: Use `bg-page` to separate the header/footer from the main body content.

---

## 2. Dashboard Structure

### Column System
The internal application uses a flexible 2-column dashboard layout:
- **Left/Center Column**: Primary workspace area (e.g., Resume Editor, Lead List). Width: `Auto/Flex`.
- **Right Column**: Tool panel for AI features and contextual help. Width: `320px` - `400px`.

### Card Grids
- **Lead Results**: Displayed in a vertical list or a 2-column grid of cards on larger screens.
- **Gutter**: `16px` (`M`) between cards.

---

## 3. Navigation & Overlays

### Header & Footer
- **Navigation Bar**: Fixed to the top with high z-index. Height: `64px`.
- **Footer**: Multi-tiered. Promo cards at the top of the footer, followed by link columns.

### Modal Overlays
- **Backdrop**: Black with `50%` opacity (`rgba(0,0,0,0.5)`).
- **Surface**: Centered white card with `XXL` (`48px`) vertical padding for important modals.
- **Close Behavior**: Closing triggers via "X" icon in top-right or clicking the backdrop.
