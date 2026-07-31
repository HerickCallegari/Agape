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

create index if not exists idx_patient_payments_patient_date
on public.patient_payments (patient_id, payment_date);

create index if not exists idx_patient_payment_items_appointment
on public.patient_payment_items (appointment_id);

create index if not exists idx_professional_payouts_professional_date
on public.professional_payouts (professional_id, payout_date);

create index if not exists idx_professional_payout_items_appointment
on public.professional_payout_items (appointment_id);

drop trigger if exists trg_patient_payments_touch on public.patient_payments;
create trigger trg_patient_payments_touch before update on public.patient_payments
for each row execute function public.touch_updated_at();

drop trigger if exists trg_professional_payouts_touch on public.professional_payouts;
create trigger trg_professional_payouts_touch before update on public.professional_payouts
for each row execute function public.touch_updated_at();

alter table public.patient_payments enable row level security;
alter table public.patient_payment_items enable row level security;
alter table public.professional_payouts enable row level security;
alter table public.professional_payout_items enable row level security;

drop policy if exists "patient payments admin reception read" on public.patient_payments;
create policy "patient payments admin reception read" on public.patient_payments
for select using (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "patient payments admin reception write" on public.patient_payments;
create policy "patient payments admin reception write" on public.patient_payments
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "patient payment items admin reception read" on public.patient_payment_items;
create policy "patient payment items admin reception read" on public.patient_payment_items
for select using (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "patient payment items admin reception write" on public.patient_payment_items;
create policy "patient payment items admin reception write" on public.patient_payment_items
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "professional payouts admin reception read" on public.professional_payouts;
create policy "professional payouts admin reception read" on public.professional_payouts
for select using (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "professional payouts admin reception write" on public.professional_payouts;
create policy "professional payouts admin reception write" on public.professional_payouts
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "professional payout items admin reception read" on public.professional_payout_items;
create policy "professional payout items admin reception read" on public.professional_payout_items
for select using (public.current_profile_role() in ('admin', 'reception'));

drop policy if exists "professional payout items admin reception write" on public.professional_payout_items;
create policy "professional payout items admin reception write" on public.professional_payout_items
for all using (public.current_profile_role() in ('admin', 'reception'))
with check (public.current_profile_role() in ('admin', 'reception'));
