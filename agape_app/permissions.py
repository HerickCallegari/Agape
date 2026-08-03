ROLE_LABELS = {
    "admin": "Administrador",
    "reception": "Recepção",
    "professional": "Profissional",
}


class Permission:
    MANAGE_PATIENTS = "manage_patients"
    MANAGE_PROFESSIONALS = "manage_professionals"
    MANAGE_AGENDA = "manage_agenda"
    CHANGE_STATUS = "change_status"
    WRITE_SESSION_NOTE = "write_session_note"
    VIEW_FINANCIALS = "view_financials"
    MANAGE_FINANCIALS = "manage_financials"
    VIEW_SETTINGS = "view_settings"


ROLE_PERMISSIONS = {
    "admin": {
        Permission.MANAGE_PATIENTS,
        Permission.MANAGE_PROFESSIONALS,
        Permission.MANAGE_AGENDA,
        Permission.CHANGE_STATUS,
        Permission.WRITE_SESSION_NOTE,
        Permission.VIEW_FINANCIALS,
        Permission.MANAGE_FINANCIALS,
        Permission.VIEW_SETTINGS,
    },
    "reception": {
        Permission.MANAGE_PATIENTS,
        Permission.MANAGE_AGENDA,
        Permission.CHANGE_STATUS,
        Permission.VIEW_FINANCIALS,
        Permission.MANAGE_FINANCIALS,
        Permission.VIEW_SETTINGS,
    },
    "professional": {
        Permission.CHANGE_STATUS,
        Permission.WRITE_SESSION_NOTE,
    },
}


def can(profile: dict, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(profile.get("role"), set())
