COLORS = {
    "bg": "#F5F6FA",
    "bg2": "#F8F9FB",
    "surface": "#FFFFFF",
    "surface_warm": "#FFFFFF",
    "primary": "#C42D7B",
    "primary_dark": "#A91F67",
    "primary_soft": "#FCE8F1",
    "text": "#2C3E50",
    "muted": "#6C7A89",
    "line": "#D3D8DE",
    "green": "#27AE60",
    "yellow": "#F5CB55",
    "red": "#E74C3C",
    "info": "#3498DB",
    "blue": "#4A90E2",
    "pink_card": "#FCE8EA",
    "green_card": "#EEF6E9",
    "yellow_card": "#FFF1D1",
    "gray": "#F2F2F2",
    "white": "#FFFFFF",
    "blue_soft": "#E7EEF9",
    "purple_soft": "#EFE5F3",
}

STATUS_COLORS = {
    "Agendado": ("#FFF1D1", COLORS["text"]),
    "Confirmado": ("#EEF6E9", COLORS["green"]),
    "Atendido": ("#E7EEF9", COLORS["text"]),
    "Falta com aviso": ("#FFE5E3", COLORS["red"]),
    "Falta sem aviso": ("#EFE5F3", COLORS["primary_dark"]),
    "Cancelado": ("#F2F2F2", COLORS["muted"]),
}


APP_STYLESHEET = f"""
QWidget {{
    background: {COLORS["bg"]};
    color: {COLORS["text"]};
    font-family: Segoe UI, Arial;
    font-size: 14px;
}}
QMainWindow {{
    background: {COLORS["bg"]};
}}
QLabel#Title {{
    background: transparent;
    font-size: 34px;
    font-weight: 800;
    color: {COLORS["text"]};
}}
QLabel#HeroText {{
    background: transparent;
    font-size: 15px;
    color: {COLORS["muted"]};
    line-height: 140%;
}}
QLabel#PageTitle {{
    background: transparent;
    font-size: 26px;
    font-weight: 800;
    color: {COLORS["text"]};
}}
QLabel#Muted {{
    background: transparent;
    color: {COLORS["muted"]};
}}
QLabel#Badge {{
    background: {COLORS["primary_soft"]};
    color: {COLORS["primary_dark"]};
    border-radius: 8px;
    padding: 5px 9px;
    font-weight: 700;
}}
QLabel#StatNumber {{
    background: transparent;
    font-size: 34px;
    font-weight: 800;
}}
QLabel#StatLabel {{
    background: transparent;
    color: {COLORS["muted"]};
    font-weight: 700;
}}
QLineEdit, QTextEdit, QComboBox, QDateEdit, QTimeEdit {{
    background: {COLORS["bg2"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
    padding: 9px 10px;
    min-height: 28px;
    selection-background-color: {COLORS["primary_soft"]};
    selection-color: {COLORS["text"]};
}}
QDateEdit#AgendaDate {{
    background: {COLORS["primary_soft"]};
    color: {COLORS["primary_dark"]};
    border: 1px solid #E9B3CF;
    font-weight: 800;
    min-width: 150px;
}}
QComboBox#AgendaProfessional {{
    min-width: 220px;
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus {{
    border: 2px solid {COLORS["primary"]};
    padding: 8px 9px;
}}
QPushButton {{
    background: {COLORS["primary"]};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {COLORS["primary_dark"]};
}}
QPushButton:disabled {{
    background: #D8CFD3;
    color: #766D72;
}}
QPushButton[secondary="true"] {{
    background: {COLORS["surface"]};
    color: {COLORS["primary"]};
    border: 1px solid {COLORS["primary"]};
}}
QPushButton[secondary="true"]:hover {{
    background: {COLORS["pink_card"]};
}}
QPushButton[danger="true"] {{
    background: {COLORS["red"]};
}}
QFrame#Card {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame#PageCard {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame#AgendaCanvas {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame#AgendaToolbar {{
    background: {COLORS["surface_warm"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame#LoginPanel {{
    background: {COLORS["surface_warm"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame#TopHeader {{
    background: {COLORS["surface"]};
    border-bottom: 1px solid {COLORS["line"]};
}}
QFrame#TopHeader QLabel {{
    background: {COLORS["surface"]};
}}
QFrame#TopNav {{
    background: {COLORS["surface"]};
    border-bottom: 1px solid {COLORS["line"]};
}}
QFrame#TopNav QPushButton {{
    background: transparent;
    color: {COLORS["text"]};
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 10px 14px;
    font-weight: 700;
}}
QFrame#TopNav QPushButton:hover {{
    background: {COLORS["primary_soft"]};
    color: {COLORS["primary_dark"]};
}}
QFrame#TopNav QPushButton[active="true"] {{
    background: {COLORS["primary"]};
    color: #FFFFFF;
}}
QFrame#Sidebar {{
    background: {COLORS["primary_dark"]};
}}
QFrame#Sidebar QLabel {{
    background: {COLORS["primary_dark"]};
    color: #FFFFFF;
}}
QFrame#Sidebar QPushButton {{
    background: transparent;
    color: #FFFFFF;
    text-align: left;
    border-radius: 8px;
    padding: 11px 12px;
    font-weight: 700;
}}
QFrame#Sidebar QPushButton:hover {{
    background: rgba(255, 255, 255, 0.12);
}}
QFrame#Sidebar QPushButton[active="true"] {{
    background: #FFFFFF;
    color: {COLORS["primary_dark"]};
}}
QTableWidget {{
    background: {COLORS["surface"]};
    alternate-background-color: #FBFCFE;
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
    gridline-color: #F0E7DB;
    selection-background-color: {COLORS["primary_soft"]};
    selection-color: {COLORS["text"]};
}}
QHeaderView::section {{
    background: #F3F5F8;
    color: {COLORS["text"]};
    border: none;
    padding: 8px;
    font-weight: 700;
}}
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    margin: 5px 0;
}}
QDialog {{
    background: {COLORS["bg"]};
}}
QTabWidget::pane {{
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
    background: {COLORS["surface"]};
    top: -1px;
}}
QTabBar::tab {{
    background: {COLORS["primary_soft"]};
    color: {COLORS["primary_dark"]};
    border: 1px solid {COLORS["line"]};
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 10px 16px;
    margin-right: 4px;
    font-weight: 700;
}}
QTabBar::tab:selected {{
    background: {COLORS["primary"]};
    color: #FFFFFF;
}}
QTabBar::tab:hover {{
    background: {COLORS["pink_card"]};
}}
"""
