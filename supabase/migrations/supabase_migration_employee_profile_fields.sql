alter table public.profiles
  add column if not exists cpf text,
  add column if not exists rg text,
  add column if not exists phone text,
  add column if not exists zip_code text,
  add column if not exists street text,
  add column if not exists address_number text,
  add column if not exists address_complement text,
  add column if not exists neighborhood text,
  add column if not exists city text,
  add column if not exists state text,
  add column if not exists can_attend boolean not null default false,
  add column if not exists specialty text;

update public.profiles
set
  can_attend = true,
  specialty = coalesce(specialty, 'Outra')
where role = 'professional';
