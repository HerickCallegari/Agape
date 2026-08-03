from __future__ import annotations

import mimetypes
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from supabase import Client, create_client

from .config import Settings


class AppError(Exception):
    pass


class SupabaseRepository:
    DEFAULT_PROFESSIONAL_SPECIALTY = "Outra"
    DOCUMENTS_BUCKET = "professional-documents"
    MAX_DOCUMENT_SIZE_BYTES = 20 * 1024 * 1024
    SUPABASE_IN_FILTER_BATCH_SIZE = 75
    NON_CHARGEABLE_APPOINTMENT_STATUSES = {"Cancelado", "Falta com aviso"}
    NON_CHARGEABLE_FINANCIAL_STATUSES = {"Cortesia", "Cancelado"}
    DOCUMENT_MIME_TYPES = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
        ".rtf": "application/rtf",
        ".csv": "text/csv",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".zip": "application/zip",
        ".rar": "application/vnd.rar",
        ".7z": "application/x-7z-compressed",
    }
    EMPLOYEE_PROFILE_FIELDS = [
        "cpf",
        "rg",
        "phone",
        "zip_code",
        "street",
        "address_number",
        "address_complement",
        "neighborhood",
        "city",
        "state",
        "can_attend",
        "specialty",
    ]

    def __init__(self, settings: Settings):
        if not settings.is_configured:
            raise AppError("Configure SUPABASE_URL e SUPABASE_ANON_KEY no arquivo .env.")
        self.settings = settings
        self.client: Client = create_client(settings.supabase_url, settings.supabase_anon_key)
        self.user = None
        self.access_token: str | None = None
        self.profile: dict[str, Any] | None = None

    def _batched_ids(self, values: list[str]) -> list[list[str]]:
        cleaned = list(dict.fromkeys(value for value in values if value))
        return [
            cleaned[index : index + self.SUPABASE_IN_FILTER_BATCH_SIZE]
            for index in range(0, len(cleaned), self.SUPABASE_IN_FILTER_BATCH_SIZE)
        ]

    def login(self, email: str, password: str) -> dict[str, Any]:
        try:
            auth = self.client.auth.sign_in_with_password({"email": email, "password": password})
            self.user = auth.user
            self.access_token = auth.session.access_token if auth.session else None
            profile = (
                self.client.table("profiles")
                .select("*")
                .eq("auth_user_id", auth.user.id)
                .eq("is_active", True)
                .single()
                .execute()
                .data
            )
        except Exception as exc:
            raise AppError("Não foi possível entrar. Confira e-mail, senha e perfil ativo.") from exc
        if not profile:
            raise AppError("Este usuário não possui perfil ativo no sistema.")
        self.profile = profile
        return profile

    def logout(self) -> None:
        self.client.auth.sign_out()
        self.user = None
        self.access_token = None
        self.profile = None

    def patients(self, search: str = "", active_only: bool = True) -> list[dict[str, Any]]:
        query = self.client.table("patients").select("*").order("full_name")
        if active_only:
            query = query.eq("is_active", True)
        if search:
            query = query.ilike("full_name", f"%{search}%")
        return query.execute().data or []

    def save_patient(self, values: dict[str, Any], record_id: str | None = None) -> None:
        if not values.get("full_name", "").strip():
            raise AppError("Preencha o nome do paciente antes de salvar.")
        if record_id:
            self.client.table("patients").update(values).eq("id", record_id).execute()
        else:
            self.client.table("patients").insert(values).execute()

    def set_patient_active(self, record_id: str, active: bool) -> None:
        self.client.table("patients").update({"is_active": active}).eq("id", record_id).execute()

    def delete_patient(self, record_id: str) -> None:
        appointments = self.client.table("appointments").select("id").eq("patient_id", record_id).limit(1).execute().data or []
        recurring = self.client.table("recurring_schedules").select("id").eq("patient_id", record_id).limit(1).execute().data or []
        if appointments or recurring:
            raise AppError("Este paciente possui atendimentos vinculados. Inative o paciente para manter o histórico.")
        self.client.table("patients").delete().eq("id", record_id).execute()

    def patient_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        return (
            self.client.table("appointments")
            .select("appointment_date, start_time, end_time, status, professionals(full_name)")
            .eq("patient_id", patient_id)
            .order("appointment_date", desc=True)
            .order("start_time", desc=True)
            .execute()
            .data
            or []
        )

    def patient_session_exports(self, patient_id: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        query = (
            self.client.table("appointments")
            .select("id, appointment_date, start_time, end_time, status, professionals(full_name, profile_id), session_notes(note)")
            .eq("patient_id", patient_id)
            .gte("appointment_date", start_date.isoformat())
            .lte("appointment_date", end_date.isoformat())
            .order("appointment_date")
            .order("start_time")
        )
        rows = query.execute().data or []
        if self.profile and self.profile.get("role") == "professional":
            rows = [row for row in rows if (row.get("professionals") or {}).get("profile_id") == self.profile["id"]]
        return rows

    def profile_appointments(self, profile_id: str) -> list[dict[str, Any]]:
        professionals = self.client.table("professionals").select("id").eq("profile_id", profile_id).execute().data or []
        rows: list[dict[str, Any]] = []
        for professional in professionals:
            rows.extend(
                self.client.table("appointments")
                .select("appointment_date, start_time, end_time, status, patients(full_name)")
                .eq("professional_id", professional["id"])
                .order("appointment_date", desc=True)
                .order("start_time", desc=True)
                .execute()
                .data
                or []
            )
        return rows

    def professionals(self, search: str = "", active_only: bool = True) -> list[dict[str, Any]]:
        self.sync_professionals_from_profiles()
        query = self.client.table("professionals").select("*, profiles(full_name, auth_user_id, role, can_attend)").order("full_name")
        if active_only:
            query = query.eq("is_active", True)
        if search:
            query = query.ilike("full_name", f"%{search}%")
        rows = query.execute().data or []
        filtered = []
        seen_profiles = set()
        for row in rows:
            profile = row.get("profiles") or {}
            profile_id = row.get("profile_id")
            if not profile.get("can_attend"):
                continue
            if profile_id in seen_profiles:
                continue
            seen_profiles.add(profile_id)
            filtered.append(row)
        return filtered

    def sync_professionals_from_profiles(self) -> None:
        profiles = (
            self.client.table("profiles")
            .select("id, full_name, role, is_active, phone, can_attend, specialty")
            .eq("can_attend", True)
            .execute()
            .data
            or []
        )
        for profile in profiles:
            self.ensure_professional_for_profile(profile)

    def ensure_professional_for_profile(self, profile: dict[str, Any]) -> None:
        if not profile.get("can_attend"):
            self.client.table("professionals").update({"is_active": False}).eq("profile_id", profile["id"]).execute()
            return

        rows = self.client.table("professionals").select("id").eq("profile_id", profile["id"]).execute().data or []
        payload = {
            "full_name": profile.get("full_name", "").strip() or "Profissional",
            "specialty": profile.get("specialty") or self.DEFAULT_PROFESSIONAL_SPECIALTY,
            "phone": profile.get("phone", "") or "",
            "is_active": profile.get("is_active", True) and profile.get("can_attend", False),
        }
        if rows:
            self.client.table("professionals").update(payload).eq("profile_id", profile["id"]).execute()
            return

        payload.update(
            {
                "profile_id": profile["id"],
                "specialty": profile.get("specialty") or self.DEFAULT_PROFESSIONAL_SPECIALTY,
                "phone": profile.get("phone", "") or "",
            }
        )
        self.client.table("professionals").insert(payload).execute()

    def save_professional(self, values: dict[str, Any], record_id: str | None = None) -> None:
        if not values.get("full_name", "").strip():
            raise AppError("Preencha o nome do profissional antes de salvar.")
        profile_id = values.get("profile_id")
        if not profile_id:
            raise AppError("Vincule o profissional a um usuário com permissão Profissional.")
        profile = self.client.table("profiles").select("can_attend, is_active").eq("id", profile_id).single().execute().data
        if not profile or not profile.get("can_attend") or not profile.get("is_active"):
            raise AppError("O usuário vinculado precisa estar ativo e marcado como capaz de realizar atendimentos.")
        if record_id:
            self.client.table("professionals").update(values).eq("id", record_id).execute()
        else:
            self.client.table("professionals").insert(values).execute()

    def set_professional_active(self, record_id: str, active: bool) -> None:
        self.client.table("professionals").update({"is_active": active}).eq("id", record_id).execute()

    def profiles(self) -> list[dict[str, Any]]:
        return self.client.table("profiles").select("*").order("full_name").execute().data or []

    def document_professionals(self) -> list[dict[str, Any]]:
        profiles = (
            self.client.table("profiles")
            .select("*")
            .eq("can_attend", True)
            .eq("is_active", True)
            .order("full_name")
            .execute()
            .data
            or []
        )
        if self.profile and self.profile.get("role") == "professional":
            return [profile for profile in profiles if profile.get("id") == self.profile.get("id")]
        return profiles

    def _can_manage_document(self, document: dict[str, Any]) -> bool:
        if not self.profile:
            return False
        if self.profile.get("role") in {"admin", "reception"}:
            return True
        return document.get("professional_id") == self.profile.get("id")

    def documents(self, search: str = "", professional_id: str | None = None) -> list[dict[str, Any]]:
        if not self.profile:
            return []
        query = self.client.table("documents").select("*").order("created_at", desc=True)
        if self.profile.get("role") == "professional":
            query = query.eq("professional_id", self.profile.get("id"))
        elif professional_id:
            query = query.eq("professional_id", professional_id)
        rows = query.execute().data or []
        profiles = {profile.get("id"): profile for profile in self.profiles()}
        for row in rows:
            professional = profiles.get(row.get("professional_id"), {})
            uploader = profiles.get(row.get("uploaded_by"), {})
            row["professional_name"] = professional.get("full_name", "")
            row["uploaded_by_name"] = uploader.get("full_name", "")
        if search:
            needle = search.strip().lower()
            rows = [
                row
                for row in rows
                if needle in (row.get("file_name") or "").lower()
                or needle in (row.get("description") or "").lower()
                or needle in (row.get("professional_name") or "").lower()
            ]
        return rows

    def document_by_id(self, document_id: str) -> dict[str, Any]:
        row = self.client.table("documents").select("*").eq("id", document_id).single().execute().data
        if not row or not self._can_manage_document(row):
            raise AppError("Documento não encontrado ou sem permissão para acessar.")
        return row

    def upload_document(self, local_path: str, professional_id: str, description: str = "") -> None:
        if not self.profile:
            raise AppError("Entre no sistema antes de enviar documentos.")
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise AppError("Selecione um arquivo válido para enviar.")
        if not professional_id:
            raise AppError("Selecione o profissional responsável pelo documento.")
        file_size = path.stat().st_size
        if file_size > self.MAX_DOCUMENT_SIZE_BYTES:
            raise AppError("O arquivo deve ter no maximo 20 MB para manter o sistema leve.")
        if self.profile.get("role") == "professional" and professional_id != self.profile.get("id"):
            raise AppError("Profissionais so podem enviar documentos para o proprio perfil.")
        safe_name = "".join(char if char.isalnum() or char in "._- " else "_" for char in path.name).strip()
        storage_path = f"{professional_id}/{uuid4().hex}_{safe_name}"
        content_type = self.document_content_type(path)
        try:
            self.client.storage.from_(self.DOCUMENTS_BUCKET).upload(
                storage_path,
                path.read_bytes(),
                {"content-type": content_type, "upsert": False},
            )
        except Exception as exc:
            raise AppError(
                "Não foi possível enviar o arquivo. Confira se o bucket 'professional-documents' existe no Supabase Storage."
            ) from exc
        payload = {
            "professional_id": professional_id,
            "uploaded_by": self.profile.get("id"),
            "file_name": path.name,
            "file_path": storage_path,
            "file_type": content_type,
            "file_size": file_size,
            "description": description.strip(),
        }
        self.client.table("documents").insert(payload).execute()

    def document_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in self.DOCUMENT_MIME_TYPES:
            return self.DOCUMENT_MIME_TYPES[suffix]
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def download_document(self, document_id: str, target_path: str) -> None:
        document = self.document_by_id(document_id)
        try:
            data = self.client.storage.from_(self.DOCUMENTS_BUCKET).download(document["file_path"])
        except Exception as exc:
            raise AppError("Não foi possível baixar o arquivo no Supabase Storage.") from exc
        Path(target_path).write_bytes(data)

    def delete_document(self, document_id: str) -> None:
        document = self.document_by_id(document_id)
        try:
            self.client.storage.from_(self.DOCUMENTS_BUCKET).remove([document["file_path"]])
        except Exception as exc:
            raise AppError("Não foi possível remover o arquivo no Supabase Storage.") from exc
        self.client.table("documents").delete().eq("id", document_id).execute()

    def available_professional_profiles(self, include_profile_id: str | None = None) -> list[dict[str, Any]]:
        profiles = self.client.table("profiles").select("*").eq("can_attend", True).eq("is_active", True).order("full_name").execute().data or []
        professionals = self.client.table("professionals").select("profile_id").execute().data or []
        used = {row.get("profile_id") for row in professionals if row.get("profile_id")}
        return [profile for profile in profiles if profile["id"] not in used or profile["id"] == include_profile_id]

    def create_system_user(self, values: dict[str, Any]) -> dict[str, Any]:
        email = values.get("email", "").strip()
        password = values.get("password", "")
        full_name = values.get("full_name", "").strip()
        role = values.get("role", "")
        if not full_name:
            raise AppError("Preencha o nome do usuário antes de salvar.")
        if not email:
            raise AppError("Preencha o e-mail do usuário antes de salvar.")
        if len(password) < 6:
            raise AppError("A senha deve ter pelo menos 6 caracteres.")
        if role not in {"admin", "reception", "professional"}:
            raise AppError("Escolha uma permissão válida para o usuário.")

        try:
            response = httpx.post(
                f"{self.settings.supabase_url}/auth/v1/signup",
                headers={
                    "apikey": self.settings.supabase_anon_key,
                    "Authorization": f"Bearer {self.settings.supabase_anon_key}",
                    "Content-Type": "application/json",
                },
                json={"email": email, "password": password},
                timeout=20,
            )
            data = response.json()
        except Exception as exc:
            raise AppError("Não foi possível criar o login no Supabase Auth.") from exc

        if response.status_code >= 400:
            message = data.get("msg") or data.get("message") or "Não foi possível criar o usuário."
            raise AppError(message)

        auth_user = data.get("user") or data
        auth_user_id = auth_user.get("id")
        if not auth_user_id:
            raise AppError("O Supabase não retornou o ID do usuário criado.")

        profile_payload = {
            "auth_user_id": auth_user_id,
            "full_name": full_name,
            "email": email,
            "role": role,
            "is_active": values.get("is_active", True),
        }
        profile_payload.update(self.employee_profile_payload(values))
        try:
            created = self.client.table("profiles").insert(profile_payload).execute().data
        except Exception as exc:
            raise AppError("Login criado, mas não foi possível criar o perfil de permissões.") from exc
        profile = created[0] if created else profile_payload
        if profile.get("can_attend"):
            self.ensure_professional_for_profile(profile)
        return profile

    def send_password_reset(self, email: str) -> None:
        email = email.strip()
        if not email:
            raise AppError("Preencha o e-mail de login antes de enviar a redefinição de senha.")
        try:
            response = httpx.post(
                f"{self.settings.supabase_url}/auth/v1/recover",
                headers={
                    "apikey": self.settings.supabase_anon_key,
                    "Authorization": f"Bearer {self.settings.supabase_anon_key}",
                    "Content-Type": "application/json",
                },
                json={"email": email},
                timeout=20,
            )
            data = response.json() if response.content else {}
        except Exception as exc:
            raise AppError("Não foi possível enviar a redefinição de senha.") from exc

        if response.status_code >= 400:
            message = data.get("msg") or data.get("message") or "Não foi possível enviar a redefinição de senha."
            raise AppError(message)

    def update_user_password(self, auth_user_id: str, password: str) -> None:
        if not auth_user_id:
            raise AppError("Usuário sem Auth ID vinculado.")
        if len(password) < 6:
            raise AppError("A nova senha deve ter pelo menos 6 caracteres.")
        if not self.access_token:
            raise AppError("Sessão expirada. Entre novamente antes de trocar a senha.")

        try:
            response = httpx.post(
                f"{self.settings.supabase_url}/functions/v1/reset-user-password",
                headers={
                    "apikey": self.settings.supabase_anon_key,
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={"auth_user_id": auth_user_id, "password": password},
                timeout=20,
            )
            data = response.json() if response.content else {}
        except Exception as exc:
            raise AppError("Não foi possível trocar a senha pelo aplicativo.") from exc

        if response.status_code >= 400:
            message = data.get("error") or data.get("message") or "Não foi possível trocar a senha."
            raise AppError(message)
        if data.get("ok") is not True:
            raise AppError(data.get("error") or "A função não confirmou a troca da senha.")

    def update_profile_permissions(self, profile_id: str, values: dict[str, Any]) -> None:
        full_name = values.get("full_name", "").strip()
        role = values.get("role", "")
        if not full_name:
            raise AppError("Preencha o nome do usuário antes de salvar.")
        if role not in {"admin", "reception", "professional"}:
            raise AppError("Escolha uma permissão válida para o usuário.")
        payload = {
            "full_name": full_name,
            "email": values.get("email", "").strip(),
            "role": role,
            "is_active": values.get("is_active", True),
        }
        payload.update(self.employee_profile_payload(values))
        self.client.table("profiles").update(payload).eq("id", profile_id).execute()
        self.ensure_professional_for_profile(
            {
                "id": profile_id,
                "full_name": full_name,
                "role": role,
                "is_active": values.get("is_active", True),
                **self.employee_profile_payload(values),
            }
        )

    def delete_profile(self, profile_id: str) -> None:
        if self.profile and self.profile.get("id") == profile_id:
            raise AppError("Você não pode apagar o próprio usuário enquanto está logado.")
        self.client.table("profiles").delete().eq("id", profile_id).execute()

    def employee_profile_payload(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = {field: values.get(field, "") for field in self.EMPLOYEE_PROFILE_FIELDS}
        payload["can_attend"] = bool(values.get("can_attend", False))
        if payload["can_attend"] and not payload.get("specialty"):
            payload["specialty"] = self.DEFAULT_PROFESSIONAL_SPECIALTY
        return payload

    def appointments(self, selected_date: date, professional_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            self.client.table("appointments")
            .select("*, patients(full_name, guardian_name, guardian_phone, reason_for_care, general_notes), professionals(full_name, profile_id)")
            .eq("appointment_date", selected_date.isoformat())
            .order("start_time")
        )
        if professional_id:
            query = query.eq("professional_id", professional_id)
        rows = query.execute().data or []
        if self.profile and self.profile.get("role") == "professional":
            profs = self.professionals(active_only=False)
            allowed = {p["id"] for p in profs if p.get("profile_id") == self.profile["id"]}
            rows = [row for row in rows if row.get("professional_id") in allowed]
        return rows

    def dashboard_counts(self, selected_date: date) -> dict[str, int]:
        rows = self.appointments(selected_date)
        return {
            "Atendimentos": len(rows),
            "Confirmados": sum(1 for row in rows if row["status"] == "Confirmado"),
            "Atendidos": sum(1 for row in rows if row["status"] == "Atendido"),
            "Faltas": sum(1 for row in rows if row["status"] in {"Falta com aviso", "Falta sem aviso"}),
        }

    def assert_no_conflict(
        self,
        professional_id: str,
        appointment_date: str,
        start_time: str,
        end_time: str,
        ignore_id: str | None = None,
    ) -> None:
        rows = (
            self.client.table("appointments")
            .select("id")
            .eq("professional_id", professional_id)
            .eq("appointment_date", appointment_date)
            .neq("status", "Cancelado")
            .lt("start_time", end_time)
            .gt("end_time", start_time)
            .execute()
            .data
            or []
        )
        if ignore_id:
            rows = [row for row in rows if row["id"] != ignore_id]
        if rows:
            raise AppError("Este profissional já possui um atendimento neste horário.")

    def appointment_values(self, values: dict[str, Any]) -> dict[str, Any]:
        return {
            "patient_id": values["patient_id"],
            "professional_id": values["professional_id"],
            "appointment_date": values["appointment_date"],
            "start_time": values["start_time"],
            "end_time": values["end_time"],
            "status": values["status"],
        }

    def appointment_initial_financials(self, values: dict[str, Any]) -> dict[str, Any] | None:
        fee_text = str(values.get("consultation_fee", "")).strip()
        if not fee_text:
            return None
        return {
            "consultation_fee": fee_text,
            "payment_status": "Pendente",
            "payment_method": "",
        }

    def save_appointment(self, values: dict[str, Any], record_id: str | None = None) -> None:
        required = ["patient_id", "professional_id", "appointment_date", "start_time", "end_time", "status"]
        if any(not values.get(field) for field in required):
            raise AppError("Preencha paciente, profissional, data, horário e status.")
        if values["end_time"] <= values["start_time"]:
            raise AppError("O horário final deve ser maior que o horário inicial.")
        self.assert_no_conflict(
            values["professional_id"],
            values["appointment_date"],
            values["start_time"],
            values["end_time"],
            ignore_id=record_id,
        )
        appointment_values = self.appointment_values(values)
        if record_id:
            self.client.table("appointments").update(appointment_values).eq("id", record_id).execute()
        else:
            created = self.client.table("appointments").insert(appointment_values).execute().data[0]
            financials = self.appointment_initial_financials(values)
            if financials:
                self.save_appointment_financials(created["id"], financials)

    def update_appointment_status(self, appointment_id: str, status: str) -> None:
        if not status:
            raise AppError("Escolha um status antes de salvar.")
        self.client.table("appointments").update({"status": status}).eq("id", appointment_id).execute()

    def update_appointments_bulk(
        self,
        appointments: list[dict[str, Any]],
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        status: str | None = None,
        consultation_fee: Any | None = None,
    ) -> int:
        if not appointments:
            raise AppError("Selecione pelo menos um atendimento.")
        if not any((start_time, end_time, status, consultation_fee is not None)):
            raise AppError("Escolha pelo menos uma alteração para aplicar.")
        if bool(start_time) != bool(end_time):
            raise AppError("Informe os horários inicial e final.")
        if start_time and end_time and end_time <= start_time:
            raise AppError("O horário final deve ser maior que o horário inicial.")

        normalized_fee = None
        if consultation_fee is not None:
            normalized_fee = self.parse_consultation_fee(consultation_fee)
        selected_ids = {row["id"] for row in appointments}
        changes: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for row in appointments:
            target = {
                "appointment_date": row["appointment_date"],
                "start_time": start_time or row["start_time"],
                "end_time": end_time or row["end_time"],
                "status": status or row["status"],
            }
            payload: dict[str, Any] = {}
            if start_time and end_time:
                payload.update({"start_time": start_time, "end_time": end_time})
            if status:
                payload["status"] = status
            conflicts = (
                self.client.table("appointments")
                .select("id")
                .eq("professional_id", row["professional_id"])
                .eq("appointment_date", target["appointment_date"])
                .neq("status", "Cancelado")
                .lt("start_time", target["end_time"])
                .gt("end_time", target["start_time"])
                .execute()
                .data
                or []
            )
            if any(item["id"] not in selected_ids for item in conflicts):
                patient = (row.get("patients") or {}).get("full_name", "Paciente")
                raise AppError(
                    f"Conflito de agenda para {patient} em "
                    f"{date.fromisoformat(target['appointment_date']).strftime('%d/%m/%Y')} "
                    f"às {target['start_time'][:5]}. Nenhum atendimento foi alterado."
                )
            changes.append((row, payload, target))

        for index, (row, _payload, target) in enumerate(changes):
            if target["status"] == "Cancelado":
                continue
            for other_row, _other_payload, other_target in changes[index + 1 :]:
                if other_target["status"] == "Cancelado":
                    continue
                if (
                    row["professional_id"] == other_row["professional_id"]
                    and target["appointment_date"] == other_target["appointment_date"]
                    and target["start_time"] < other_target["end_time"]
                    and target["end_time"] > other_target["start_time"]
                ):
                    raise AppError("As alterações criariam conflito entre dois atendimentos selecionados.")

        fee_values = None
        if consultation_fee is not None:
            fee_values = {"consultation_fee": normalized_fee}
        for row, payload, _target in changes:
            if payload:
                self.client.table("appointments").update(payload).eq("id", row["id"]).execute()
            if fee_values is not None:
                existing = self.appointment_financials(row["id"])
                self.save_appointment_financials(
                    row["id"],
                    {
                        **fee_values,
                        "payment_status": (existing or {}).get("payment_status", "Pendente"),
                        "payment_method": (existing or {}).get("payment_method", ""),
                        "financial_notes": (existing or {}).get("financial_notes", ""),
                    },
                )
        return len(changes)

    def delete_appointment(self, appointment_id: str) -> None:
        if not appointment_id:
            raise AppError("Atendimento não encontrado para exclusão.")
        self.client.table("appointments").delete().eq("id", appointment_id).execute()

    def session_note_for(self, appointment_id: str) -> dict[str, Any] | None:
        rows = self.client.table("session_notes").select("*").eq("appointment_id", appointment_id).execute().data or []
        return rows[0] if rows else None

    def save_session_note(self, appointment: dict[str, Any], note: str) -> None:
        if self.profile is None:
            raise AppError("Usuário sem perfil ativo.")
        existing = self.session_note_for(appointment["id"])
        payload = {
            "appointment_id": appointment["id"],
            "patient_id": appointment["patient_id"],
            "professional_id": appointment["professional_id"],
            "note": note,
            "created_by": self.profile["id"],
        }
        if existing:
            self.client.table("session_notes").update({"note": note}).eq("id", existing["id"]).execute()
        else:
            self.client.table("session_notes").insert(payload).execute()
        self.client.table("appointments").update({"notes_summary": note[:160]}).eq("id", appointment["id"]).execute()

    def appointment_financials(self, appointment_id: str) -> dict[str, Any] | None:
        rows = self.client.table("appointment_financials").select("*").eq("appointment_id", appointment_id).execute().data or []
        return rows[0] if rows else None

    def financial_appointments(
        self,
        start_date: date | None,
        end_date: date | None,
        payment_status: str | None = None,
        patient_id: str | None = None,
        professional_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("appointments")
            .select("*, patients(full_name), professionals(full_name), appointment_financials(*)")
            .order("appointment_date")
            .order("start_time")
        )
        if start_date:
            query = query.gte("appointment_date", start_date.isoformat())
        if end_date:
            query = query.lte("appointment_date", end_date.isoformat())
        if patient_id:
            query = query.eq("patient_id", patient_id)
        if professional_id:
            query = query.eq("professional_id", professional_id)
        rows = query.execute().data or []
        if payment_status:
            rows = [row for row in rows if self.financial_row(row).get("payment_status") == payment_status]
        return rows

    def patient_payment_balance_appointments(
        self,
        patient_id: str | None = None,
        professional_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        balance_filter: str = "open",
        include_non_chargeable: bool = False,
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("appointments")
            .select("*, patients(full_name), professionals(full_name), appointment_financials(*)")
            .order("appointment_date")
            .order("start_time")
        )
        if patient_id:
            query = query.eq("patient_id", patient_id)
        if professional_id:
            query = query.eq("professional_id", professional_id)
        if start_date:
            query = query.gte("appointment_date", start_date.isoformat())
        if end_date:
            query = query.lte("appointment_date", end_date.isoformat())
        rows = query.execute().data or []
        appointment_ids = [row["id"] for row in rows]
        paid_by_appointment = {appointment_id: 0.0 for appointment_id in appointment_ids}
        if appointment_ids:
            for item in self.patient_payment_items_for_appointments(appointment_ids):
                appointment_id = item.get("appointment_id")
                paid_by_appointment[appointment_id] = paid_by_appointment.get(appointment_id, 0.0) + float(item.get("amount") or 0)

        balance_rows = []
        for row in rows:
            financial = self.financial_row(row)
            chargeable = (
                row.get("status") not in self.NON_CHARGEABLE_APPOINTMENT_STATUSES
                and financial.get("payment_status") not in self.NON_CHARGEABLE_FINANCIAL_STATUSES
            )
            if not include_non_chargeable and not chargeable:
                continue
            fee = round(float(financial.get("consultation_fee") or 0), 2)
            if not include_non_chargeable and fee <= 0:
                continue
            paid = paid_by_appointment.get(row["id"], 0.0)
            open_amount = round(max(fee - paid, 0), 2) if chargeable and fee > 0 else 0.0
            is_open = open_amount > 0.009
            if balance_filter == "open" and not is_open:
                continue
            if balance_filter == "closed" and is_open:
                continue
            row["_paid_amount"] = paid
            row["_open_amount"] = open_amount
            balance_rows.append(row)
        return balance_rows

    def overdue_financial_appointments(self, professional_id: str | None = None) -> list[dict[str, Any]]:
        cutoff = date.today() - timedelta(days=31)
        query = (
            self.client.table("appointments")
            .select("*, patients(full_name), professionals(full_name), appointment_financials(*)")
            .lte("appointment_date", cutoff.isoformat())
            .order("appointment_date")
            .order("start_time")
        )
        if professional_id:
            query = query.eq("professional_id", professional_id)
        rows = query.execute().data or []
        return [
            row
            for row in rows
            if row.get("status") not in self.NON_CHARGEABLE_APPOINTMENT_STATUSES
            and self.financial_row(row).get("payment_status") not in {"Pago", *self.NON_CHARGEABLE_FINANCIAL_STATUSES}
        ]

    def patient_payments(self, start_date: date | None, end_date: date | None) -> list[dict[str, Any]]:
        query = self.client.table("patient_payments").select("patient_id, amount")
        if start_date:
            query = query.gte("payment_date", start_date.isoformat())
        if end_date:
            query = query.lte("payment_date", end_date.isoformat())
        return query.execute().data or []

    def patient_payment_history(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        patient_id: str | None = None,
        professional_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("patient_payments")
            .select("*, patients(full_name), patient_payment_items(amount, appointments(patient_id, professional_id))")
            .order("payment_date", desc=True)
            .order("created_at", desc=True)
        )
        if start_date:
            query = query.gte("payment_date", start_date.isoformat())
        if end_date:
            query = query.lte("payment_date", end_date.isoformat())
        if patient_id:
            query = query.eq("patient_id", patient_id)
        rows = query.execute().data or []
        if professional_id:
            rows = [
                row for row in rows
                if any(
                    (item.get("appointments") or {}).get("professional_id") == professional_id
                    for item in row.get("patient_payment_items") or []
                )
            ]
        return rows

    def patient_payment_items_for_appointments(self, appointment_ids: list[str]) -> list[dict[str, Any]]:
        if not appointment_ids:
            return []
        rows: list[dict[str, Any]] = []
        for batch in self._batched_ids(appointment_ids):
            rows.extend(
                self.client.table("patient_payment_items")
                .select("appointment_id, amount")
                .in_("appointment_id", batch)
                .execute()
                .data
                or []
            )
        return rows

    def open_patient_payment_appointments(
        self,
        patient_id: str | None = None,
        professional_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.patient_payment_balance_appointments(
            patient_id=patient_id,
            professional_id=professional_id,
            balance_filter="open",
        )

    def patient_open_payment_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self.patient_payment_balance_appointments(
            patient_id=patient_id,
            balance_filter="all",
            include_non_chargeable=True,
        )
        visible_rows = []
        for row in rows:
            financial = self.financial_row(row)
            payment_status = str(financial.get("payment_status") or "").strip()
            fee = float(financial.get("consultation_fee") or 0)
            open_amount = float(row.get("_open_amount") or 0)
            is_paid = payment_status == "Pago" or (fee > 0 and open_amount <= 0.009)
            if not is_paid:
                visible_rows.append(row)
        return visible_rows

    def parse_money(self, value: Any, field_label: str = "valor") -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            amount = float(value)
        else:
            text = str(value or "").strip().replace("R$", "").replace(" ", "")
            if not text:
                raise AppError(f"Informe o {field_label}.")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            try:
                amount = float(text)
            except ValueError as exc:
                raise AppError(f"Informe um {field_label} válido.") from exc
        if amount <= 0:
            raise AppError(f"O {field_label} deve ser maior que zero.")
        return amount

    def parse_consultation_fee(self, value: Any) -> float | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            consultation_fee = float(value)
        else:
            fee_text = str(value).replace("R$", "").replace(" ", "").strip()
            if "," in fee_text:
                fee_text = fee_text.replace(".", "").replace(",", ".")
            try:
                consultation_fee = float(fee_text)
            except ValueError as exc:
                raise AppError("Informe um valor de consulta válido.") from exc
        if consultation_fee < 0:
            raise AppError("O valor da consulta não pode ser negativo.")
        return consultation_fee

    def save_patient_payment(self, values: dict[str, Any], items: list[dict[str, Any]]) -> None:
        if self.profile is None:
            raise AppError("Usuário sem perfil ativo.")
        patient_id = values.get("patient_id")
        if not patient_id:
            raise AppError("Escolha um paciente.")
        if not items:
            raise AppError("Selecione pelo menos um atendimento para receber.")
        current_rows = self.patient_payment_balance_appointments(
            patient_id=patient_id, balance_filter="all", include_non_chargeable=True
        )
        current_by_id = {row["id"]: row for row in current_rows}
        validated_items = []
        for item in items:
            row = current_by_id.get(item.get("appointment_id"))
            open_amount = round(float((row or {}).get("_open_amount") or 0), 2)
            if not row or open_amount <= 0:
                raise AppError("Um atendimento selecionado não pertence ao paciente ou não possui saldo para receber.")
            validated_items.append({"appointment_id": row["id"], "open_amount": open_amount})
        amount = self.parse_money(values.get("amount"), "valor recebido")
        selected_total = round(sum(item["open_amount"] for item in validated_items), 2)
        if amount > selected_total + 0.009:
            raise AppError("O valor recebido não pode ser maior que o saldo dos atendimentos selecionados.")

        self.client.rpc(
            "register_patient_payment",
            {
                "p_patient_id": patient_id,
                "p_payment_date": values.get("payment_date"),
                "p_amount": round(amount, 2),
                "p_payment_method": values.get("payment_method") or "Nao informado",
                "p_notes": values.get("notes", "").strip(),
                "p_appointment_ids": [item["appointment_id"] for item in validated_items],
            },
        ).execute()

    def professional_payouts(
        self,
        start_date: date | None,
        end_date: date | None,
        professional_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self.client.table("professional_payouts").select("professional_id, amount")
        if start_date:
            query = query.gte("payout_date", start_date.isoformat())
        if end_date:
            query = query.lte("payout_date", end_date.isoformat())
        if professional_id:
            query = query.eq("professional_id", professional_id)
        return query.execute().data or []

    def professional_payout_history(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        patient_id: str | None = None,
        professional_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("professional_payouts")
            .select("*, professionals(full_name), professional_payout_items(amount, appointments(patient_id, professional_id))")
            .order("payout_date", desc=True)
            .order("created_at", desc=True)
        )
        if start_date:
            query = query.gte("payout_date", start_date.isoformat())
        if end_date:
            query = query.lte("payout_date", end_date.isoformat())
        if professional_id:
            query = query.eq("professional_id", professional_id)
        rows = query.execute().data or []
        if patient_id:
            rows = [
                row for row in rows
                if any(
                    (item.get("appointments") or {}).get("patient_id") == patient_id
                    for item in row.get("professional_payout_items") or []
                )
            ]
        return rows

    def professional_payout_items_for_appointments(self, appointment_ids: list[str]) -> list[dict[str, Any]]:
        if not appointment_ids:
            return []
        rows: list[dict[str, Any]] = []
        for batch in self._batched_ids(appointment_ids):
            rows.extend(
                self.client.table("professional_payout_items")
                .select("appointment_id, amount")
                .in_("appointment_id", batch)
                .execute()
                .data
                or []
            )
        return rows

    def professional_open_payout_appointments(self, professional_id: str) -> list[dict[str, Any]]:
        rows = (
            self.client.table("appointments")
            .select("*, patients(full_name), appointment_financials(*)")
            .eq("professional_id", professional_id)
            .order("appointment_date")
            .order("start_time")
            .execute()
            .data
            or []
        )
        appointment_ids = [row["id"] for row in rows]
        paid_by_appointment = {appointment_id: 0.0 for appointment_id in appointment_ids}
        if appointment_ids:
            for item in self.professional_payout_items_for_appointments(appointment_ids):
                appointment_id = item.get("appointment_id")
                paid_by_appointment[appointment_id] = paid_by_appointment.get(appointment_id, 0.0) + float(item.get("amount") or 0)
        open_rows = []
        for row in rows:
            financial = self.financial_row(row)
            chargeable = (
                row.get("status") not in self.NON_CHARGEABLE_APPOINTMENT_STATUSES
                and financial.get("payment_status") not in self.NON_CHARGEABLE_FINANCIAL_STATUSES
            )
            fee = round(float(financial.get("consultation_fee") or 0), 2)
            payout_amount = round(fee * 0.70, 2) if chargeable else 0.0
            paid = paid_by_appointment.get(row["id"], 0.0)
            open_amount = round(max(payout_amount - paid, 0), 2)
            row["_gross_amount"] = fee if chargeable else 0.0
            row["_payout_amount"] = payout_amount
            row["_paid_payout_amount"] = round(paid, 2)
            row["_open_payout_amount"] = open_amount
            row["_is_chargeable"] = chargeable
            open_rows.append(row)
        return open_rows

    def save_professional_payout(self, values: dict[str, Any], items: list[dict[str, Any]]) -> None:
        if self.profile is None:
            raise AppError("Usuário sem perfil ativo.")
        professional_id = values.get("professional_id")
        if not professional_id:
            raise AppError("Escolha um funcionário.")
        if not items:
            raise AppError("Selecione pelo menos um atendimento para repassar.")
        current_rows = self.professional_open_payout_appointments(professional_id)
        current_by_id = {row["id"]: row for row in current_rows}
        validated_items = []
        for item in items:
            row = current_by_id.get(item.get("appointment_id"))
            open_amount = round(float((row or {}).get("_open_payout_amount") or 0), 2)
            if not row or open_amount <= 0:
                raise AppError("Um atendimento selecionado não pertence ao profissional ou não possui saldo para repassar.")
            validated_items.append({"appointment_id": row["id"], "open_amount": open_amount})
        amount = self.parse_money(values.get("amount"), "valor do repasse")
        selected_total = round(sum(item["open_amount"] for item in validated_items), 2)
        if amount > selected_total + 0.009:
            raise AppError("O valor do repasse não pode ser maior que o saldo dos atendimentos selecionados.")

        self.client.rpc(
            "register_professional_payout",
            {
                "p_professional_id": professional_id,
                "p_payout_date": values.get("payout_date"),
                "p_amount": round(amount, 2),
                "p_payment_method": values.get("payment_method") or "Nao informado",
                "p_notes": values.get("notes", "").strip(),
                "p_appointment_ids": [item["appointment_id"] for item in validated_items],
            },
        ).execute()

    def financial_row(self, appointment: dict[str, Any]) -> dict[str, Any]:
        financials = appointment.get("appointment_financials") or []
        if isinstance(financials, list):
            return financials[0] if financials else {}
        return financials

    def appointment_is_chargeable(self, appointment: dict[str, Any]) -> bool:
        financial = self.financial_row(appointment)
        return (
            appointment.get("status") not in self.NON_CHARGEABLE_APPOINTMENT_STATUSES
            and financial.get("payment_status") not in self.NON_CHARGEABLE_FINANCIAL_STATUSES
            and float(financial.get("consultation_fee") or 0) > 0
        )

    def professional_share(self, consultation_fee: Any) -> float:
        return round(float(consultation_fee or 0) * 0.70, 2)

    def save_appointment_financials(self, appointment_id: str, values: dict[str, Any]) -> None:
        consultation_fee = self.parse_consultation_fee(values.get("consultation_fee", ""))
        existing = self.appointment_financials(appointment_id)
        if consultation_fee is not None:
            payment_items = (
                self.client.table("patient_payment_items")
                .select("amount")
                .eq("appointment_id", appointment_id)
                .execute()
                .data
                or []
            )
            received = round(sum(float(item.get("amount") or 0) for item in payment_items), 2)
            if received > consultation_fee + 0.009:
                raise AppError(
                    f"O valor não pode ser menor que o total já recebido (R$ {received:.2f})."
                )
            payout_items = (
                self.client.table("professional_payout_items")
                .select("amount")
                .eq("appointment_id", appointment_id)
                .execute()
                .data
                or []
            )
            paid_payout = round(sum(float(item.get("amount") or 0) for item in payout_items), 2)
            payout_limit = round(consultation_fee * 0.70, 2)
            if paid_payout > payout_limit + 0.009:
                raise AppError(
                    f"O valor não pode gerar repasse menor que o total já pago (R$ {paid_payout:.2f})."
                )
        payload = {
            "appointment_id": appointment_id,
            "consultation_fee": consultation_fee,
            "payment_status": values.get("payment_status", existing.get("payment_status", "Nao informado") if existing else "Nao informado"),
            "payment_method": (values.get("payment_method") or "").strip(),
        }
        if "financial_notes" in values:
            payload["financial_notes"] = (values.get("financial_notes") or "").strip()
        if existing:
            self.client.table("appointment_financials").update(payload).eq("id", existing["id"]).execute()
        else:
            self.client.table("appointment_financials").insert(payload).execute()

    def save_recurring_schedule(self, values: dict[str, Any]) -> int:
        if values["end_time"] <= values["start_time"]:
            raise AppError("O horário final deve ser maior que o horário inicial.")
        if values["end_date"] < values["start_date"]:
            raise AppError("A data final deve ser maior ou igual a data inicial.")
        schedule_values = {key: value for key, value in values.items() if key != "consultation_fee"}
        financials = self.appointment_initial_financials(values)
        created = self.client.table("recurring_schedules").insert(schedule_values).execute().data[0]
        count = 0
        current = date.fromisoformat(values["start_date"])
        final = date.fromisoformat(values["end_date"])
        while current <= final:
            if current.weekday() == values["weekday"]:
                appointment = {
                    "patient_id": values["patient_id"],
                    "professional_id": values["professional_id"],
                    "recurring_schedule_id": created["id"],
                    "appointment_date": current.isoformat(),
                    "start_time": values["start_time"],
                    "end_time": values["end_time"],
                    "status": "Agendado",
                }
                self.assert_no_conflict(
                    appointment["professional_id"],
                    appointment["appointment_date"],
                    appointment["start_time"],
                    appointment["end_time"],
                )
                inserted = self.client.table("appointments").insert(appointment).execute().data[0]
                if financials:
                    self.save_appointment_financials(inserted["id"], financials)
                count += 1
            current += timedelta(days=1)
        return count

    def export_backup(self) -> dict[str, Any]:
        tables = [
            "profiles",
            "patients",
            "professionals",
            "recurring_schedules",
            "appointments",
            "session_notes",
            "appointment_financials",
            "patient_payments",
            "patient_payment_items",
            "professional_payouts",
            "professional_payout_items",
            "documents",
        ]
        return {table: self.client.table(table).select("*").execute().data or [] for table in tables}
