alter table public.patients
add column if not exists reason_for_care text;

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

drop trigger if exists trg_appointment_financials_touch on public.appointment_financials;
create trigger trg_appointment_financials_touch before update on public.appointment_financials
for each row execute function public.touch_updated_at();

alter table public.appointment_financials enable row level security;

drop policy if exists "financials admin reception read" on public.appointment_financials;
create policy "financials admin reception read" on public.appointment_financials
for select using (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "financials admin reception write" on public.appointment_financials;
create policy "financials admin reception write" on public.appointment_financials
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));
