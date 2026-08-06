alter table public.patient_payment_items
  alter column amount drop not null,
  add column if not exists is_paid boolean not null default true;

alter table public.professional_payout_items
  alter column amount drop not null,
  add column if not exists is_repassed boolean not null default true;

comment on column public.patient_payment_items.amount is
  'Campo legado. Novos recebimentos usam apenas is_paid e o vinculo com patient_payments.';
comment on column public.professional_payout_items.amount is
  'Campo legado. Novos repasses usam apenas is_repassed e o vinculo com professional_payouts.';

create or replace function public.register_patient_payment(
  p_patient_id uuid,
  p_payment_date date,
  p_amount numeric,
  p_reference_amount numeric,
  p_discount_amount numeric,
  p_surcharge_amount numeric,
  p_payment_method text,
  p_notes text,
  p_appointment_ids uuid[]
) returns uuid
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_payment_id uuid;
  v_appointment_id uuid;
  v_fee numeric(10,2);
  v_reference numeric(10,2) := 0;
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para registrar recebimentos.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor recebido deve ser maior que zero.';
  end if;
  if coalesce(p_discount_amount, 0) < 0 or coalesce(p_surcharge_amount, 0) < 0 then
    raise exception 'Descontos e acrescimos nao podem ser negativos.';
  end if;
  if coalesce(array_length(p_appointment_ids, 1), 0) = 0 then
    raise exception 'Selecione pelo menos um atendimento.';
  end if;

  foreach v_appointment_id in array p_appointment_ids loop
    select round(coalesce(af.consultation_fee, 0), 2) into v_fee
    from public.appointments a
    left join public.appointment_financials af on af.appointment_id = a.id
    where a.id = v_appointment_id and a.patient_id = p_patient_id
    for update of a;
    if not found then
      raise exception 'Atendimento invalido para o paciente selecionado.';
    end if;
    v_reference := round(v_reference + v_fee, 2);
  end loop;

  if round(coalesce(p_reference_amount, 0), 2) <> v_reference then
    raise exception 'O valor de referencia mudou. Atualize a busca e tente novamente.';
  end if;

  insert into public.patient_payments
    (patient_id, payment_date, amount, reference_amount, discount_amount, surcharge_amount,
     payment_method, notes, created_by)
  values
    (p_patient_id, p_payment_date, round(p_amount, 2), v_reference,
     round(coalesce(p_discount_amount, 0), 2), round(coalesce(p_surcharge_amount, 0), 2),
     coalesce(nullif(p_payment_method, ''), 'Nao informado'), p_notes, public.current_profile_id())
  returning id into v_payment_id;

  foreach v_appointment_id in array p_appointment_ids loop
    insert into public.patient_payment_items (patient_payment_id, appointment_id, is_paid)
    values (v_payment_id, v_appointment_id, true);

    insert into public.appointment_financials (appointment_id, payment_status, payment_method)
    values (v_appointment_id, 'Pago', coalesce(nullif(p_payment_method, ''), 'Nao informado'))
    on conflict (appointment_id) do update
      set payment_status = 'Pago', payment_method = excluded.payment_method, updated_at = now();
  end loop;
  return v_payment_id;
end;
$$;

create or replace function public.register_professional_payout(
  p_professional_id uuid,
  p_payout_date date,
  p_amount numeric,
  p_reference_amount numeric,
  p_discount_amount numeric,
  p_surcharge_amount numeric,
  p_payment_method text,
  p_notes text,
  p_appointment_ids uuid[]
) returns uuid
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_payout_id uuid;
  v_appointment_id uuid;
  v_due numeric(10,2);
  v_reference numeric(10,2) := 0;
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para registrar repasses.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor do repasse deve ser maior que zero.';
  end if;
  if coalesce(p_discount_amount, 0) < 0 or coalesce(p_surcharge_amount, 0) < 0 then
    raise exception 'Descontos e acrescimos nao podem ser negativos.';
  end if;
  if coalesce(array_length(p_appointment_ids, 1), 0) = 0 then
    raise exception 'Selecione pelo menos um atendimento.';
  end if;

  foreach v_appointment_id in array p_appointment_ids loop
    select round(coalesce(af.consultation_fee, 0) * 0.70, 2) into v_due
    from public.appointments a
    left join public.appointment_financials af on af.appointment_id = a.id
    where a.id = v_appointment_id and a.professional_id = p_professional_id
    for update of a;
    if not found then
      raise exception 'Atendimento invalido para o profissional selecionado.';
    end if;
    v_reference := round(v_reference + v_due, 2);
  end loop;

  if round(coalesce(p_reference_amount, 0), 2) <> v_reference then
    raise exception 'O valor de referencia mudou. Atualize a busca e tente novamente.';
  end if;

  insert into public.professional_payouts
    (professional_id, payout_date, amount, reference_amount, discount_amount, surcharge_amount,
     payment_method, notes, created_by)
  values
    (p_professional_id, p_payout_date, round(p_amount, 2), v_reference,
     round(coalesce(p_discount_amount, 0), 2), round(coalesce(p_surcharge_amount, 0), 2),
     coalesce(nullif(p_payment_method, ''), 'Nao informado'), p_notes, public.current_profile_id())
  returning id into v_payout_id;

  foreach v_appointment_id in array p_appointment_ids loop
    insert into public.professional_payout_items (professional_payout_id, appointment_id, is_repassed)
    values (v_payout_id, v_appointment_id, true);

    insert into public.appointment_financials (appointment_id, professional_payout_status)
    values (v_appointment_id, 'Repassado')
    on conflict (appointment_id) do update
      set professional_payout_status = 'Repassado', updated_at = now();
  end loop;
  return v_payout_id;
end;
$$;
