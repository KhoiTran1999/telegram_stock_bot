# 🎨 Design Aesthetics & The "WOW" Factor

To ensure every chart generated for the user is premium and visually stunning, you must apply the following design principles. A generic, default-looking chart is UNACCEPTABLE.

## 1. Signature Vnstock Styling (The "WOW" Factor)

When creating charts, always aim for a professional, premium "FinTech" aesthetic. 

- **Color Palettes**: Avoid basic colors like 'red' or 'blue'. Use tailored hex codes.
  - **Uptrend / Bullish**: `#00b894` (Mint Green) or `#00d2d3` (Cyan)
  - **Downtrend / Bearish**: `#ff7675` (Soft Red) or `#d63031` (Crimson)
  - **Neutral / Volume**: `#0984e3` (Vibrant Blue) or `#6c5ce7` (Purple)
  - **Background**: For dark mode themes, use `#2d3436` or `#1e272e`. For light themes, use `#f5f6fa`.
- **Typography**: If the library supports it, use modern sans-serif fonts (e.g., `Inter`, `Roboto`, `Helvetica Neue`).
- **Cleanliness**: 
  - Remove unnecessary chart borders (spines).
  - Use subtle, dashed grid lines (opacity/alpha around 0.2 - 0.3) instead of solid, heavy lines.
  - Ensure labels do not overlap. Rotate X-axis dates if necessary.

## 2. Interactive Excellence (`vnstock_ezchart`)

When using `vnstock_ezchart`:
- Always enable tooltips so the user can hover over data points to see exact values.
- Configure zooming and panning capabilities (if configurable).
- If plotting multiple metrics (e.g., Price and Volume), use subplots or dual-axis so they don't squash each other. Volume should typically be represented as a bar chart at the bottom 20% of the main chart.

## 3. Static Excellence (`matplotlib` / `seaborn`)

When falling back to `matplotlib`:
- Always use a style sheet. Before plotting, run: `plt.style.use('seaborn-v0_8-darkgrid')` or `plt.style.use('ggplot')` to instantly upgrade the look from the ugly default.
- Set a high DPI for crisp images: `plt.figure(figsize=(12, 6), dpi=120)`.
- Fill the area under line charts for a modern look using `plt.fill_between(..., alpha=0.2)`.
- Customize the spines: `ax.spines['top'].set_visible(False)` and `ax.spines['right'].set_visible(False)`.

## 4. Branding & Disclaimers (MANDATORY)

- **No Vnstock Logo:** You MUST NOT display the vnstock logo on the charts to avoid giving the impression that vnstock is providing financial advice. When using `vnstock_ezchart`, explicitly disable the logo by running `Chart.set_theme(logo_path=None)` before plotting.
- **Data Source Disclaimer:** You MUST include a small, light-colored text disclaimer clearly stating the data is extracted via the libraries, NOT that the libraries are the data source. 
  - **Exact text:** `Dữ liệu công khai trích xuất qua vnstock/vnstock_data`
  - **Placement:** On a separate line below the chart, center-aligned if possible.
  - **Matplotlib Example:** `plt.figtext(0.5, 0.01, 'Dữ liệu công khai trích xuất qua vnstock/vnstock_data', ha='center', fontsize=9, color='#94a3b8', style='italic')` (ensure `fig.subplots_adjust(bottom=...)` is used if needed to prevent overlap).
