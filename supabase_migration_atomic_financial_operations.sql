create or replace function public.register_patient_payment(
  p_patient_id uuid,
  p_payment_date date,
  p_amount numeric,
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
  v_paid numeric(10,2);
  v_open numeric(10,2);
  v_item_amount numeric(10,2);
  v_remaining numeric(10,2) := round(p_amount, 2);
  v_status text;
  v_financial_status text;
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para registrar recebimentos.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor recebido deve ser maior que zero.';
  end if;
  if coalesce(array_length(p_appointment_ids, 1), 0) = 0 then
    raise exception 'Selecione pelo menos um atendimento.';
  end if;

  insert into public.patient_payments
    (patient_id, payment_date, amount, payment_method, notes, created_by)
  values
    (p_patient_id, p_payment_date, round(p_amount, 2), coalesce(nullif(p_payment_method, ''), 'Nao informado'), p_notes, public.current_profile_id())
  returning id into v_payment_id;

  foreach v_appointment_id in array p_appointment_ids loop
    exit when v_remaining <= 0;
    select a.status, af.consultation_fee, af.payment_status
      into v_status, v_fee, v_financial_status
    from public.appointments a
    join public.appointment_financials af on af.appointment_id = a.id
    where a.id = v_appointment_id and a.patient_id = p_patient_id
    for update of af;

    if not found then
      raise exception 'Atendimento invalido para o paciente selecionado.';
    end if;
    if v_status in ('Cancelado', 'Falta com aviso') or v_financial_status in ('Cortesia', 'Cancelado') then
      raise exception 'Um atendimento selecionado nao e cobravel.';
    end if;

    select coalesce(round(sum(amount), 2), 0) into v_paid
    from public.patient_payment_items where appointment_id = v_appointment_id;
    v_open := greatest(round(coalesce(v_fee, 0) - v_paid, 2), 0);
    if v_open <= 0 then
      raise exception 'Um atendimento selecionado nao possui saldo.';
    end if;
    v_item_amount := least(v_open, v_remaining);
    insert into public.patient_payment_items (patient_payment_id, appointment_id, amount)
    values (v_payment_id, v_appointment_id, v_item_amount);
    v_remaining := round(v_remaining - v_item_amount, 2);

    update public.appointment_financials
    set payment_status = case when v_paid + v_item_amount >= coalesce(v_fee, 0) then 'Pago' else 'Pendente' end,
        payment_method = coalesce(nullif(p_payment_method, ''), 'Nao informado')
    where appointment_id = v_appointment_id;
  end loop;

  if v_remaining > 0 then
    raise exception 'O valor recebido e maior que o saldo dos atendimentos selecionados.';
  end if;
  return v_payment_id;
end;
$$;

create or replace function public.register_professional_payout(
  p_professional_id uuid,
  p_payout_date date,
  p_amount numeric,
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
  v_fee numeric(10,2);
  v_paid numeric(10,2);
  v_due numeric(10,2);
  v_open numeric(10,2);
  v_item_amount numeric(10,2);
  v_remaining numeric(10,2) := round(p_amount, 2);
  v_status text;
  v_financial_status text;
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para registrar repasses.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor do repasse deve ser maior que zero.';
  end if;
  if coalesce(array_length(p_appointment_ids, 1), 0) = 0 then
    raise exception 'Selecione pelo menos um atendimento.';
  end if;

  insert into public.professional_payouts
    (professional_id, payout_date, amount, payment_method, notes, created_by)
  values
    (p_professional_id, p_payout_date, round(p_amount, 2), coalesce(nullif(p_payment_method, ''), 'Nao informado'), p_notes, public.current_profile_id())
  returning id into v_payout_id;

  foreach v_appointment_id in array p_appointment_ids loop
    exit when v_remaining <= 0;
    select a.status, af.consultation_fee, af.payment_status
      into v_status, v_fee, v_financial_status
    from public.appointments a
    join public.appointment_financials af on af.appointment_id = a.id
    where a.id = v_appointment_id and a.professional_id = p_professional_id
    for update of af;

    if not found then
      raise exception 'Atendimento invalido para o profissional selecionado.';
    end if;
    if v_status in ('Cancelado', 'Falta com aviso') or v_financial_status in ('Cortesia', 'Cancelado') then
      raise exception 'Um atendimento selecionado nao gera repasse.';
    end if;

    v_due := round(coalesce(v_fee, 0) * 0.70, 2);
    select coalesce(round(sum(amount), 2), 0) into v_paid
    from public.professional_payout_items where appointment_id = v_appointment_id;
    v_open := greatest(round(v_due - v_paid, 2), 0);
    if v_open <= 0 then
      raise exception 'Um atendimento selecionado nao possui saldo de repasse.';
    end if;
    v_item_amount := least(v_open, v_remaining);
    insert into public.professional_payout_items (professional_payout_id, appointment_id, amount)
    values (v_payout_id, v_appointment_id, v_item_amount);
    v_remaining := round(v_remaining - v_item_amount, 2);
  end loop;

  if v_remaining > 0 then
    raise exception 'O valor do repasse e maior que o saldo selecionado.';
  end if;
  return v_payout_id;
end;
$$;
