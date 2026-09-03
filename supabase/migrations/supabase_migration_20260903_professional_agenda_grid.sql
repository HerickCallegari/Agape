-- Configuração aditiva da grade diária dos profissionais.
-- Os atendimentos e todos os seus relacionamentos permanecem inalterados.

alter table public.professionals
  add column if not exists agenda_start_time time not null default '08:00:00',
  add column if not exists agenda_end_time time not null default '18:00:00',
  add column if not exists agenda_slot_minutes integer not null default 60,
  add column if not exists agenda_step_minutes integer not null default 60;

-- Usa somente horários agregados para preparar uma configuração inicial por
-- profissional. Nenhum atendimento é atualizado, removido ou recriado.
with appointment_ranges as (
  select
    professional_id,
    min(start_time) as first_start_time,
    max(end_time) as last_end_time
  from public.appointments
  group by professional_id
), duration_counts as (
  select
    professional_id,
    round(extract(epoch from (end_time - start_time)) / 60)::integer as duration_minutes,
    count(*) as uses,
    max(appointment_date) as most_recent_date
  from public.appointments
  where end_time > start_time
  group by professional_id, round(extract(epoch from (end_time - start_time)) / 60)::integer
), ranked_durations as (
  select
    *,
    row_number() over (
      partition by professional_id
      order by uses desc, most_recent_date desc, duration_minutes asc
    ) as position
  from duration_counts
  where duration_minutes between 5 and 480
), ordered_starts as (
  select
    professional_id,
    appointment_date,
    start_time,
    lead(start_time) over (
      partition by professional_id, appointment_date
      order by start_time
    ) as next_start_time
  from public.appointments
), step_counts as (
  select
    professional_id,
    round(extract(epoch from (next_start_time - start_time)) / 60)::integer as step_minutes,
    count(*) as uses
  from ordered_starts
  where next_start_time > start_time
  group by professional_id, round(extract(epoch from (next_start_time - start_time)) / 60)::integer
), ranked_steps as (
  select
    *,
    row_number() over (
      partition by professional_id
      order by uses desc, step_minutes asc
    ) as position
  from step_counts
  where step_minutes between 5 and 480
), inferred_settings as (
  select
    ranges.professional_id,
    ranges.first_start_time,
    ranges.last_end_time,
    coalesce(duration.duration_minutes, 60) as duration_minutes,
    step.step_minutes
  from appointment_ranges as ranges
  left join ranked_durations as duration
    on duration.professional_id = ranges.professional_id
    and duration.position = 1
  left join ranked_steps as step
    on step.professional_id = ranges.professional_id
    and step.position = 1
)
update public.professionals as professional
set
  agenda_start_time = inferred.first_start_time,
  agenda_end_time = inferred.last_end_time,
  agenda_slot_minutes = inferred.duration_minutes,
  -- Na ausência de exemplos suficientes, arredonda a distância entre inícios
  -- para o próximo bloco de 30 minutos: 40/50 minutos começam a cada 60.
  agenda_step_minutes = greatest(
    inferred.duration_minutes,
    least(
      coalesce(
        inferred.step_minutes,
        least(480, ((inferred.duration_minutes + 29) / 30) * 30)
      ),
      least(480, ((inferred.duration_minutes + 29) / 30) * 30)
    )
  )
from inferred_settings as inferred
where professional.id = inferred.professional_id
  -- Se a migração for executada novamente, uma configuração já personalizada
  -- não é substituída pelos valores históricos.
  and professional.agenda_start_time = '08:00:00'
  and professional.agenda_end_time = '18:00:00'
  and professional.agenda_slot_minutes = 60
  and professional.agenda_step_minutes = 60;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'professionals_agenda_time_range_check'
      and conrelid = 'public.professionals'::regclass
  ) then
    alter table public.professionals
      add constraint professionals_agenda_time_range_check
      check (agenda_end_time > agenda_start_time) not valid;
    alter table public.professionals
      validate constraint professionals_agenda_time_range_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'professionals_agenda_slot_minutes_check'
      and conrelid = 'public.professionals'::regclass
  ) then
    alter table public.professionals
      add constraint professionals_agenda_slot_minutes_check
      check (agenda_slot_minutes between 5 and 480) not valid;
    alter table public.professionals
      validate constraint professionals_agenda_slot_minutes_check;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'professionals_agenda_step_minutes_check'
      and conrelid = 'public.professionals'::regclass
  ) then
    alter table public.professionals
      add constraint professionals_agenda_step_minutes_check
      check (agenda_step_minutes between agenda_slot_minutes and 480) not valid;
    alter table public.professionals
      validate constraint professionals_agenda_step_minutes_check;
  end if;
end
$$;
