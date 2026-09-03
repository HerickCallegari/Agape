alter table public.patients
add column if not exists birth_date date,
add column if not exists document text,
add column if not exists patient_phone text,
add column if not exists email text,
add column if not exists zip_code text,
add column if not exists street text,
add column if not exists address_number text,
add column if not exists address_complement text,
add column if not exists neighborhood text,
add column if not exists city text,
add column if not exists state text;
