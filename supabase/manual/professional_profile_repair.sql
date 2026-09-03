begin;

do $migration$
declare
  v_history_professional_id uuid := '539b1b32-5ea6-4e15-b554-1ad305b872e5';
  v_empty_professional_id uuid := '4ff3a064-97d9-4ddd-915b-0dd08dc687ab';
  v_profile_id uuid := '5b153a28-f10c-4a55-bafe-ada37a0c9c77';
begin
  if not exists (
    select 1 from public.professionals
    where id = v_history_professional_id
      and profile_id is null
      and lower(trim(full_name)) = lower('Thiago Silva')
  ) then
    raise exception 'O registro histórico do profissional não está no estado esperado.';
  end if;

  if not exists (
    select 1 from public.professionals
    where id = v_empty_professional_id
      and profile_id = v_profile_id
      and lower(trim(full_name)) = lower('Thiago Silva')
  ) then
    raise exception 'O registro duplicado do profissional não está no estado esperado.';
  end if;

  if exists (
    select 1 from public.appointments where professional_id = v_empty_professional_id
    union all
    select 1 from public.recurring_schedules where professional_id = v_empty_professional_id
    union all
    select 1 from public.session_notes where professional_id = v_empty_professional_id
    union all
    select 1 from public.professional_payouts where professional_id = v_empty_professional_id
  ) then
    raise exception 'O registro duplicado passou a possuir dados e precisa de revisão manual.';
  end if;

  update public.professionals
  set profile_id = null,
      is_active = false,
      updated_at = now()
  where id = v_empty_professional_id;

  update public.professionals
  set profile_id = v_profile_id,
      is_active = true,
      updated_at = now()
  where id = v_history_professional_id;
end
$migration$;

-- Um perfil de login pode representar somente um registro profissional.
create unique index if not exists uq_professionals_profile_id
on public.professionals (profile_id)
where profile_id is not null;

commit;
