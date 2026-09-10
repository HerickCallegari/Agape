-- Permite várias faixas de atendimento no mesmo dia sem alterar consultas existentes.
alter table public.professionals
  add column if not exists agenda_periods jsonb;

update public.professionals
set agenda_periods = jsonb_build_array(
  jsonb_build_object('start', agenda_start_time::text, 'end', agenda_end_time::text)
)
where agenda_periods is null;

alter table public.professionals
  alter column agenda_periods set default '[{"start":"08:00:00","end":"11:00:00"},{"start":"13:00:00","end":"18:00:00"}]'::jsonb,
  alter column agenda_periods set not null;

alter table public.professionals
  drop constraint if exists professionals_agenda_periods_shape_check;

alter table public.professionals
  add constraint professionals_agenda_periods_shape_check check (
    jsonb_typeof(agenda_periods) = 'array'
    and jsonb_array_length(agenda_periods) > 0
  );
