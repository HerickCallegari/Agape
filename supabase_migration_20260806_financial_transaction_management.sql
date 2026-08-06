create or replace function public.update_patient_payment(
  p_payment_id uuid, p_payment_date date, p_amount numeric, p_payment_method text
) returns void language plpgsql security invoker set search_path = public as $$
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para alterar recebimentos.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor recebido deve ser maior que zero.';
  end if;
  update public.patient_payments set
    payment_date = p_payment_date,
    amount = round(p_amount, 2),
    payment_method = coalesce(nullif(p_payment_method, ''), 'Nao informado'),
    updated_at = now()
  where id = p_payment_id;
  if not found then raise exception 'Recebimento nao encontrado.'; end if;
  update public.appointment_financials af set
    payment_method = coalesce(nullif(p_payment_method, ''), 'Nao informado'),
    updated_at = now()
  where exists (
    select 1 from public.patient_payment_items ppi
    where ppi.patient_payment_id = p_payment_id and ppi.appointment_id = af.appointment_id
  );
end;
$$;

create or replace function public.delete_patient_payment(p_payment_id uuid)
returns void language plpgsql security invoker set search_path = public as $$
declare v_appointment_id uuid; v_appointment_ids uuid[];
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para excluir recebimentos.';
  end if;
  select array_agg(appointment_id) into v_appointment_ids
  from public.patient_payment_items where patient_payment_id = p_payment_id;
  delete from public.patient_payments where id = p_payment_id;
  if not found then raise exception 'Recebimento nao encontrado.'; end if;
  foreach v_appointment_id in array coalesce(v_appointment_ids, array[]::uuid[]) loop
    update public.appointment_financials af set
      payment_status = case when exists (
        select 1 from public.patient_payment_items ppi
        where ppi.appointment_id = v_appointment_id and coalesce(ppi.is_paid, true)
      ) then 'Pago' else 'Pendente' end,
      payment_method = case when exists (
        select 1 from public.patient_payment_items ppi
        where ppi.appointment_id = v_appointment_id and coalesce(ppi.is_paid, true)
      ) then af.payment_method else 'Nao informado' end,
      updated_at = now()
    where af.appointment_id = v_appointment_id;
  end loop;
end;
$$;

create or replace function public.update_professional_payout(
  p_payout_id uuid, p_payout_date date, p_amount numeric, p_payment_method text
) returns void language plpgsql security invoker set search_path = public as $$
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para alterar repasses.';
  end if;
  if p_amount is null or round(p_amount, 2) <= 0 then
    raise exception 'O valor repassado deve ser maior que zero.';
  end if;
  update public.professional_payouts set
    payout_date = p_payout_date,
    amount = round(p_amount, 2),
    payment_method = coalesce(nullif(p_payment_method, ''), 'Nao informado'),
    updated_at = now()
  where id = p_payout_id;
  if not found then raise exception 'Repasse nao encontrado.'; end if;
end;
$$;

create or replace function public.delete_professional_payout(p_payout_id uuid)
returns void language plpgsql security invoker set search_path = public as $$
declare v_appointment_id uuid; v_appointment_ids uuid[];
begin
  if public.current_profile_role() not in ('admin', 'reception') then
    raise exception 'Perfil sem permissao para excluir repasses.';
  end if;
  select array_agg(appointment_id) into v_appointment_ids
  from public.professional_payout_items where professional_payout_id = p_payout_id;
  delete from public.professional_payouts where id = p_payout_id;
  if not found then raise exception 'Repasse nao encontrado.'; end if;
  foreach v_appointment_id in array coalesce(v_appointment_ids, array[]::uuid[]) loop
    update public.appointment_financials af set
      professional_payout_status = case when exists (
        select 1 from public.professional_payout_items ppi
        where ppi.appointment_id = v_appointment_id and coalesce(ppi.is_repassed, true)
      ) then 'Repassado' else 'Pendente' end,
      updated_at = now()
    where af.appointment_id = v_appointment_id;
  end loop;
end;
$$;
