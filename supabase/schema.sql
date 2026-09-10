create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id uuid primary key default gen_random_uuid(),
  auth_user_id uuid unique not null references auth.users(id) on delete cascade,
  full_name text not null,
  email text,
  role text not null check (role in ('admin', 'reception', 'professional')),
  cpf text,
  rg text,
  phone text,
  zip_code text,
  street text,
  address_number text,
  address_complement text,
  neighborhood text,
  city text,
  state text,
  can_attend boolean not null default false,
  specialty text,
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.patients (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  birth_date date,
  document text,
  guardian_name text,
  guardian_phone text,
  patient_phone text,
  email text,
  zip_code text,
  street text,
  address_number text,
  address_complement text,
  neighborhood text,
  city text,
  state text,
  reason_for_care text,
  general_notes text,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.professionals (
  id uuid primary key default gen_random_uuid(),
  profile_id uuid references public.profiles(id) on delete set null,
  full_name text not null,
  specialty text not null,
  phone text,
  agenda_start_time time not null default '08:00:00',
  agenda_end_time time not null default '18:00:00',
  agenda_periods jsonb not null default '[{"start":"08:00:00","end":"11:00:00"},{"start":"13:00:00","end":"18:00:00"}]'::jsonb,
  agenda_slot_minutes integer not null default 60,
  agenda_step_minutes integer not null default 60,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (agenda_end_time > agenda_start_time),
  check (agenda_slot_minutes between 5 and 480),
  check (agenda_step_minutes between agenda_slot_minutes and 480)
);

create table if not exists public.recurring_schedules (
  id uuid primary key default gen_random_uuid(),
  patient_id uuid not null references public.patients(id),
  professional_id uuid not null references public.professionals(id),
  weekday int not null check (weekday between 0 and 6),
  start_time time not null,
  end_time time not null,
  start_date date not null,
  end_date date not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (end_time > start_time),
  check (end_date >= start_date)
);

create table if not exists public.appointments (
  id uuid primary key default gen_random_uuid(),
  patient_id uuid not null references public.patients(id),
  professional_id uuid not null references public.professionals(id),
  recurring_schedule_id uuid references public.recurring_schedules(id) on delete set null,
  appointment_date date not null,
  start_time time not null,
  end_time time not null,
  status text not null default 'Agendado' check (status in ('Agendado', 'Confirmado', 'Atendido', 'Falta com aviso', 'Falta sem aviso', 'Cancelado')),
  notes_summary text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (end_time > start_time)
);

create table if not exists public.session_notes (
  id uuid primary key default gen_random_uuid(),
  appointment_id uuid not null unique references public.appointments(id) on delete cascade,
  patient_id uuid not null references public.patients(id),
  professional_id uuid not null references public.professionals(id),
  note text not null default '',
  created_by uuid not null references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.appointment_financials (
  id uuid primary key default gen_random_uuid(),
  appointment_id uuid not null unique references public.appointments(id) on delete cascade,
  consultation_fee numeric(10, 2),
  payment_status text not null default 'Nao informado' check (payment_status in ('Nao informado', 'Pendente', 'Pago', 'Cortesia', 'Cancelado')),
  payment_method text,
  financial_notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (consultation_fee is null or consultation_fee >= 0)
);

create table if not exists public.patient_payments (
  id uuid primary key default gen_random_uuid(),
  patient_id uuid not null references public.patients(id),
  payment_date date not null default current_date,
  amount numeric(10, 2) not null check (amount > 0),
  payment_method text not null default 'Nao informado',
  notes text,
  created_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.patient_payment_items (
  id uuid primary key default gen_random_uuid(),
  patient_payment_id uuid not null references public.patient_payments(id) on delete cascade,
  appointment_id uuid not null references public.appointments(id),
  amount numeric(10, 2) not null check (amount > 0),
  created_at timestamptz not null default now()
);

create table if not exists public.professional_payouts (
  id uuid primary key default gen_random_uuid(),
  professional_id uuid not null references public.professionals(id),
  payout_date date not null default current_date,
  amount numeric(10, 2) not null check (amount > 0),
  payment_method text not null default 'Nao informado',
  notes text,
  created_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.professional_payout_items (
  id uuid primary key default gen_random_uuid(),
  professional_payout_id uuid not null references public.professional_payouts(id) on delete cascade,
  appointment_id uuid not null references public.appointments(id),
  amount numeric(10, 2) not null check (amount > 0),
  created_at timestamptz not null default now()
);

create table if not exists public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.profiles(id) on delete set null,
  action text not null,
  table_name text not null,
  record_id uuid,
  old_data jsonb,
  new_data jsonb,
  created_at timestamptz not null default now()
);

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_patients_touch on public.patients;
create trigger trg_patients_touch before update on public.patients
for each row execute function public.touch_updated_at();

drop trigger if exists trg_professionals_touch on public.professionals;
create trigger trg_professionals_touch before update on public.professionals
for each row execute function public.touch_updated_at();

drop trigger if exists trg_recurring_touch on public.recurring_schedules;
create trigger trg_recurring_touch before update on public.recurring_schedules
for each row execute function public.touch_updated_at();

drop trigger if exists trg_appointments_touch on public.appointments;
create trigger trg_appointments_touch before update on public.appointments
for each row execute function public.touch_updated_at();

drop trigger if exists trg_session_notes_touch on public.session_notes;
create trigger trg_session_notes_touch before update on public.session_notes
for each row execute function public.touch_updated_at();

drop trigger if exists trg_appointment_financials_touch on public.appointment_financials;
create trigger trg_appointment_financials_touch before update on public.appointment_financials
for each row execute function public.touch_updated_at();

drop trigger if exists trg_patient_payments_touch on public.patient_payments;
create trigger trg_patient_payments_touch before update on public.patient_payments
for each row execute function public.touch_updated_at();

drop trigger if exists trg_professional_payouts_touch on public.professional_payouts;
create trigger trg_professional_payouts_touch before update on public.professional_payouts
for each row execute function public.touch_updated_at();

create unique index if not exists idx_appointments_professional_slot
on public.appointments (professional_id, appointment_date, start_time, end_time)
where status <> 'Cancelado';

create unique index if not exists uq_professionals_profile_id
on public.professionals (profile_id)
where profile_id is not null;

create index if not exists idx_patient_payments_patient_date
on public.patient_payments (patient_id, payment_date);

create index if not exists idx_patient_payment_items_appointment
on public.patient_payment_items (appointment_id);

create index if not exists idx_professional_payouts_professional_date
on public.professional_payouts (professional_id, payout_date);

create index if not exists idx_professional_payout_items_appointment
on public.professional_payout_items (appointment_id);

alter table public.profiles enable row level security;
alter table public.patients enable row level security;
alter table public.professionals enable row level security;
alter table public.recurring_schedules enable row level security;
alter table public.appointments enable row level security;
alter table public.session_notes enable row level security;
alter table public.appointment_financials enable row level security;
alter table public.patient_payments enable row level security;
alter table public.patient_payment_items enable row level security;
alter table public.professional_payouts enable row level security;
alter table public.professional_payout_items enable row level security;
alter table public.audit_logs enable row level security;

create or replace function public.current_profile_role()
returns text language sql stable security definer set search_path = public as $$
  select role from public.profiles
  where auth_user_id = auth.uid() and is_active = true
  limit 1
$$;

create or replace function public.current_profile_id()
returns uuid language sql stable security definer set search_path = public as $$
  select id from public.profiles
  where auth_user_id = auth.uid() and is_active = true
  limit 1
$$;

create policy "profiles read own or admin" on public.profiles
for select using (auth_user_id = auth.uid() or public.current_profile_role() = 'admin');

create policy "profiles admin write" on public.profiles
for all using (public.current_profile_role() = 'admin')
with check (public.current_profile_role() = 'admin');

create policy "patients read authenticated" on public.patients
for select using (auth.role() = 'authenticated');

create policy "patients admin reception write" on public.patients
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "professionals read authenticated" on public.professionals
for select using (auth.role() = 'authenticated');

create policy "professionals admin write" on public.professionals
for all using (public.current_profile_role() = 'admin')
with check (public.current_profile_role() = 'admin');

create policy "recurring read authenticated" on public.recurring_schedules
for select using (auth.role() = 'authenticated');

create policy "recurring admin reception write" on public.recurring_schedules
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "appointments read authenticated" on public.appointments
for select using (auth.role() = 'authenticated');

create policy "appointments admin reception write" on public.appointments
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "appointments professional update status" on public.appointments
for update using (
  public.current_profile_role() = 'professional'
  and professional_id in (select id from public.professionals where profile_id = public.current_profile_id())
) with check (
  public.current_profile_role() = 'professional'
  and professional_id in (select id from public.professionals where profile_id = public.current_profile_id())
);

create policy "notes read authenticated" on public.session_notes
for select using (auth.role() = 'authenticated');

create policy "notes admin professional write" on public.session_notes
for all using (public.current_profile_role() in ('admin', 'professional'))
with check (public.current_profile_role() in ('admin', 'professional'));

create policy "financials admin reception read" on public.appointment_financials
for select using (public.current_profile_role() in ('admin', 'reception'));

create policy "financials admin reception write" on public.appointment_financials
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "patient payments admin reception read" on public.patient_payments
for select using (public.current_profile_role() in ('admin', 'reception'));

create policy "patient payments admin reception write" on public.patient_payments
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "patient payment items admin reception read" on public.patient_payment_items
for select using (public.current_profile_role() in ('admin', 'reception'));

create policy "patient payment items admin reception write" on public.patient_payment_items
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "professional payouts admin reception read" on public.professional_payouts
for select using (public.current_profile_role() in ('admin', 'reception'));

create policy "professional payouts admin reception write" on public.professional_payouts
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "professional payout items admin reception read" on public.professional_payout_items
for select using (public.current_profile_role() in ('admin', 'reception'));

create policy "professional payout items admin reception write" on public.professional_payout_items
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

create policy "audit admin read" on public.audit_logs
for select using (public.current_profile_role() = 'admin');
