"""
ui_theme.py — Dashboard CSS/JS theme (DESIGN.md dark theme + Material Icons fix).

Pure presentation, no logic — separated out of aegis_demo.py so the dashboard
script isn't dominated by ~250 lines of static styling (Phase 1, contract C1).
"""

DASHBOARD_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&family=Material+Icons&display=swap');
    @import url('https://fonts.googleapis.com/icon?family=Material+Icons');

    /* Reset & Base Elements */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        font-family: 'Inter', sans-serif !important;
        background-color: #080c14 !important;
        color: #e8edf5 !important;
    }

    h1, h2, h3, h4, h5, h6 {
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em !important;
        color: #e8edf5 !important;
    }

    p, span, label {
        font-family: 'Inter', sans-serif !important;
    }

    /* Material Design Icons */
    .material-icons, [class*="material-icons"] {
        font-family: 'Material Icons' !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: 24px !important;
        display: inline-block !important;
        line-height: 1 !important;
        text-transform: none !important;
        letter-spacing: normal !important;
        word-wrap: normal !important;
        white-space: nowrap !important;
        direction: ltr !important;
    }

    /* Streamlit icon buttons */
    button svg, [role="button"] svg {
        font-family: 'Material Icons' !important;
    }

    /* Force Material Icons font on all Streamlit icon elements */
    button span[class*="emotion-cache"],
    [role="button"] span[class*="emotion-cache"],
    [data-testid*="sidebar"] button span {
        font-family: 'Material Icons', 'Material Icons Outlined' !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: inherit !important;
        letter-spacing: normal !important;
        text-transform: none !important;
    }

    /* Layout: Simplified flat header layout */
    .aegis-header {
        background-color: #0f1724;
        border: 1px solid #1e2d45;
        border-radius: 2px;
        padding: 16px 24px;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }

    /* Layout: Equal width distribution for tabs */
    button[data-baseweb="tab"] {
        flex: 1;
        text-align: center;
        padding: 14px 0;
        font-family: 'Inter', sans-serif !important;
        font-size: 13px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        border-bottom: 2px solid transparent !important;
        color: #8899b4 !important;
        background-color: transparent !important;
        transition: all 150ms ease-out !important;
    }

    button[data-baseweb="tab"]:hover {
        color: #e8edf5 !important;
    }

    button[data-baseweb="tab"][aria-selected="true"] {
        color: #00d4ff !important;
        border-bottom: 2px solid #00d4ff !important;
        background-color: #0f1724 !important;
    }

    /* Container & Cards Styling */
    .metric-card {
        background-color: #0f1724;
        border: 1px solid #1e2d45;
        border-radius: 2px;
        padding: 20px;
        text-align: center;
        transition: all 150ms ease-out;
    }

    .metric-card:hover {
        transform: translateY(-1px);
        border-color: #2a4a7f;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    }

    .financial-exposure-bar {
        background-color: #0f1724;
        border: 1px solid #1e2d45;
        border-radius: 2px;
        padding: 16px 24px;
        width: 100%;
        margin-top: 16px;
    }

    /* Typography values */
    .metric-val {
        font-size: 2.2rem;
        font-weight: 700;
        margin: 8px 0;
        font-family: 'JetBrains Mono', monospace !important;
        letter-spacing: -0.01em;
    }

    .metric-val.blue {
        color: #00d4ff;
        text-shadow: 0 0 10px rgba(0, 212, 255, 0.25);
    }
    .metric-val.purple {
        color: #e8edf5;
        text-shadow: 0 0 10px rgba(232, 237, 245, 0.20);
    }
    .metric-val.red {
        color: #ff3355;
        text-shadow: 0 0 10px rgba(255, 51, 85, 0.25);
    }
    .metric-val.green {
        color: #0aff8e;
        text-shadow: 0 0 10px rgba(10, 255, 142, 0.25);
    }

    .metric-label {
        font-size: 11px;
        color: #8899b4;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-weight: 600;
    }

    /* Sidebar Styling Override */
    [data-testid="stSidebar"] {
        background-color: #080c14 !important;
        border-right: 1px solid #1e2d45 !important;
    }

    /* Sidebar Sections */
    .sidebar-section-header {
        border-bottom: 1px solid #1e2d45;
        padding-bottom: 8px;
        margin-bottom: 16px;
        margin-top: 16px;
    }

    .sidebar-section-header h3 {
        font-size: 12px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        color: #8899b4 !important;
        margin: 0 !important;
    }

    /* Buttons Customization */
    div.stButton > button {
        border-radius: 2px !important;
        border: 1px solid #1e2d45 !important;
        background-color: #0f1724 !important;
        color: #e8edf5 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 500 !important;
        font-size: 12px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        padding: 8px 16px !important;
        transition: all 150ms ease-out !important;
        width: 100%;
        cursor: pointer;
    }

    div.stButton > button:hover {
        border-color: #00d4ff !important;
        color: #00d4ff !important;
        background-color: #162032 !important;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }

    div.stButton > button:active {
        transform: translateY(0);
    }

    /* Alerts/Callouts */
    .aegis-alert {
        background-color: #162032;
        border: 1px solid #1e2d45;
        border-radius: 2px;
        padding: 16px 20px;
        margin-bottom: 16px;
    }

    /* Section Headers */
    .section-header {
        border-left: 2px solid #00d4ff;
        padding-left: 10px;
        margin: 20px 0 15px 0;
        font-weight: 600;
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: #e8edf5;
    }

    /* Custom Table styling */
    .stDataFrame table {
        border-collapse: collapse !important;
        background-color: #0f1724 !important;
    }
    .stDataFrame th {
        background-color: #162032 !important;
        color: #8899b4 !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 11px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        padding: 10px !important;
        border-bottom: 1px solid #1e2d45 !important;
    }
    .stDataFrame td {
        padding: 10px !important;
        border-bottom: 1px solid #1e2d45 !important;
        color: #e8edf5 !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 13px !important;
    }
</style>

<script>
// Fix Material Icons rendering in Streamlit UI elements
(function() {
  function fixMaterialIcons() {
    // Find all spans containing icon text names
    document.querySelectorAll('span').forEach(span => {
      const text = span.textContent.trim();
      if (text === 'keyboard_double_arrow_left' || text === 'keyboard_double_arrow_right') {
        // Apply Material Icons font
        span.style.fontFamily = "'Material Icons', sans-serif";
        span.style.fontWeight = 'normal';
        span.style.fontStyle = 'normal';
        span.style.fontSize = 'inherit';
        span.style.letterSpacing = 'normal';
        span.style.textTransform = 'none';
      }
    });
  }

  // Run on initial load
  fixMaterialIcons();

  // Also run after a short delay to catch dynamically rendered elements
  setTimeout(fixMaterialIcons, 100);
  setTimeout(fixMaterialIcons, 500);

  // Listen for DOM changes (Streamlit reruns)
  const observer = new MutationObserver(fixMaterialIcons);
  observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""

HEADER_HTML = """
<div class="aegis-header">
    <div style="display: flex; align-items: center; gap: 16px;">
        <div style="font-size: 1.5rem; line-height: 1; color: #00d4ff; font-weight: 700;">◆</div>
        <div>
            <h1 style="margin: 0; font-size: 1.35rem; font-weight: 700; color: #e8edf5; letter-spacing: -0.02em;">AEGIS // THREAT INTELLIGENCE</h1>
            <p style="margin: 0; font-size: 0.75rem; color: #8899b4; text-transform: uppercase; letter-spacing: 0.5px;">Smart City Financial & Infrastructure Cyber Risk Intelligence</p>
        </div>
    </div>
    <div style="font-size: 11px; font-weight: 600; padding: 4px 10px; border: 1px solid #1e2d45; border-radius: 2px; color: #0aff8e; letter-spacing: 0.5px;">
        STATUS: ENCRYPTED LINK SECURE
    </div>
</div>
"""
