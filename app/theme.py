"""Application theme.

Provides a single :func:`build_stylesheet` function returning a Qt Style Sheet
(QSS) string for a clean, modern light theme. Pair it with the Fusion base
style for consistent rendering across platforms.
"""

from __future__ import annotations

# Palette -------------------------------------------------------------------
BG = "#eef1f6"
SURFACE = "#ffffff"
SURFACE_ALT = "#f6f8fc"
BORDER = "#e2e6ee"
BORDER_STRONG = "#cbd2de"
TEXT = "#1d2530"
MUTED = "#69727f"
ACCENT = "#2f6df6"
ACCENT_HOVER = "#2a61dd"
ACCENT_PRESSED = "#2455c4"
ACCENT_SOFT = "#e6efff"
DANGER = "#e5484d"
DANGER_SOFT = "#fdecec"
DANGER_BORDER = "#f1c4c6"
TRACK = "#dde3ec"


def build_stylesheet() -> str:
    """Return the application-wide QSS stylesheet."""
    return f"""
    QWidget {{
        background: {BG};
        color: {TEXT};
        font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {BG}; }}
    QToolTip {{
        background: {TEXT}; color: #ffffff; border: none;
        padding: 6px 8px; border-radius: 6px;
    }}

    /* Header ------------------------------------------------------------ */
    QLabel#appTitle {{ font-size: 20px; font-weight: 700; color: {TEXT}; }}
    QLabel#appSubtitle {{ color: {MUTED}; font-size: 12px; }}
    QLabel#scannerTitle {{ font-size: 19px; font-weight: 700; color: {TEXT}; }}
    QLabel#scannerStatus {{ color: {MUTED}; font-size: 12px; }}
    QLabel#pairingCode {{
        color: {TEXT}; background: {SURFACE_ALT}; border: 1px solid {BORDER_STRONG};
        border-radius: 6px; padding: 10px; font-size: 28px; font-weight: 700;
    }}
    QLabel[chip="true"] {{
        background: {SURFACE}; border: 1px solid {BORDER};
        border-radius: 13px; padding: 5px 13px;
    }}

    /* Buttons ----------------------------------------------------------- */
    QPushButton {{
        background: {SURFACE}; border: 1px solid {BORDER_STRONG};
        border-radius: 8px; padding: 7px 14px; color: {TEXT}; font-weight: 600;
    }}
    QPushButton:hover {{ background: {SURFACE_ALT}; border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
    QPushButton:disabled {{ color: #9aa1ad; background: #f1f3f8; border-color: {BORDER}; }}
    QPushButton[variant="primary"] {{
        background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff;
    }}
    QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
    QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
    QPushButton[variant="danger"] {{
        background: {SURFACE}; border: 1px solid {DANGER_BORDER}; color: {DANGER};
    }}
    QPushButton[variant="danger"]:hover {{ background: {DANGER_SOFT}; border-color: {DANGER}; }}

    /* Inputs ------------------------------------------------------------ */
    QLineEdit, QTextEdit, QComboBox {{
        background: {SURFACE}; border: 1px solid {BORDER_STRONG};
        border-radius: 8px; padding: 6px 10px;
        selection-background-color: {ACCENT}; selection-color: #ffffff;
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {SURFACE}; border: 1px solid {BORDER_STRONG};
        border-radius: 8px; selection-background-color: {ACCENT_SOFT};
        selection-color: {TEXT}; outline: none; padding: 4px;
    }}

    /* Checkbox ---------------------------------------------------------- */
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border: 1px solid {BORDER_STRONG};
        border-radius: 5px; background: {SURFACE};
    }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    /* Tabs -------------------------------------------------------------- */
    QTabWidget::pane {{
        border: 1px solid {BORDER}; border-radius: 10px;
        background: {SURFACE}; top: -1px;
    }}
    QTabBar::tab {{
        background: transparent; color: {MUTED}; padding: 8px 18px;
        margin-right: 4px; border: 1px solid transparent;
        border-top-left-radius: 8px; border-top-right-radius: 8px; font-weight: 600;
    }}
    QTabBar::tab:selected {{
        background: {SURFACE}; color: {ACCENT};
        border: 1px solid {BORDER}; border-bottom-color: {SURFACE};
    }}
    QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

    /* Tables ------------------------------------------------------------ */
    QTableView {{
        background: {SURFACE}; alternate-background-color: {SURFACE_ALT};
        gridline-color: #edf0f5; border: 1px solid {BORDER};
        border-radius: 10px; selection-background-color: {ACCENT_SOFT};
        selection-color: {TEXT}; outline: none;
    }}
    QTableView::item {{ padding: 4px 6px; }}
    QTableView::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
    QHeaderView::section {{
        background: {SURFACE_ALT}; color: {MUTED}; padding: 8px 6px;
        border: none; border-bottom: 1px solid {BORDER_STRONG}; font-weight: 700;
    }}
    QHeaderView::section:hover {{ color: {TEXT}; }}
    QTableCornerButton::section {{ background: {SURFACE_ALT}; border: none; }}

    /* Progress ---------------------------------------------------------- */
    QProgressBar {{
        background: {TRACK}; border: none; border-radius: 8px; height: 16px;
        text-align: center; color: {TEXT}; font-weight: 600; font-size: 11px;
    }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 8px; }}

    /* Menu -------------------------------------------------------------- */
    QMenuBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
    QMenuBar::item {{ padding: 6px 12px; background: transparent; border-radius: 6px; }}
    QMenuBar::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
    QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 6px; }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 8px; }}

    /* Status bar & separators ------------------------------------------ */
    QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
    QStatusBar::item {{ border: none; }}
    QFrame[role="vline"] {{ color: {BORDER}; background: {BORDER}; max-width: 1px; }}

    /* Scrollbars -------------------------------------------------------- */
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #c4ccd9; border-radius: 6px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #aab3c2; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #c4ccd9; border-radius: 6px; min-width: 30px; }}
    QScrollBar::handle:horizontal:hover {{ background: #aab3c2; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """
