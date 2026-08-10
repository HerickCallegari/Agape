drop function if exists public.update_patient_payment(uuid, date, numeric, text);
create function public.update_patient_payment(
  p_payment_id uuid, p_payment_date date, p_amount numeric, p_payment_method text,
  p_appointment_ids uuid[]
) returns void language plpgsql security invoker set search_path = public as $$
declare v_patient_id uuid; v_id uuid; v_old_ids uuid[]; v_all_ids uuid[]; v_reference numeric(10,2) := 0;
begin
  if public.current_profile_role() not in ('admin', 'reception') then raise exception 'Perfil sem permissao.'; end if;
  if p_amount is null or round(p_amount,2) <= 0 then raise exception 'Valor invalido.'; end if;
  if coalesce(array_length(p_appointment_ids,1),0) = 0 then raise exception 'Selecione atendimentos.'; end if;
  select patient_id into v_patient_id from public.patient_payments where id=p_payment_id for update;
  if not found then raise exception 'Recebimento nao encontrado.'; end if;
  select array_agg(appointment_id) into v_old_ids from public.patient_payment_items where patient_payment_id=p_payment_id;
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.appointments where id=v_id and patient_id=v_patient_id) then
      raise exception 'Atendimento invalido para o paciente.';
    end if;
    v_reference := v_reference + coalesce((select round(consultation_fee,2)
      from public.appointment_financials where appointment_id=v_id),0);
  end loop;
  update public.patient_payments set payment_date=p_payment_date, amount=round(p_amount,2),
    reference_amount=v_reference, payment_method=coalesce(nullif(p_payment_method,''),'Nao informado'), updated_at=now()
  where id=p_payment_id;
  delete from public.patient_payment_items where patient_payment_id=p_payment_id and not (appointment_id=any(p_appointment_ids));
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.patient_payment_items where patient_payment_id=p_payment_id and appointment_id=v_id) then
      insert into public.patient_payment_items(patient_payment_id,appointment_id,is_paid) values(p_payment_id,v_id,true);
    end if;
  end loop;
  select array_agg(distinct x) into v_all_ids from unnest(coalesce(v_old_ids,array[]::uuid[]) || p_appointment_ids) x;
  foreach v_id in array coalesce(v_all_ids,array[]::uuid[]) loop
    update public.appointment_financials af set
      payment_status=case when exists(select 1 from public.patient_payment_items i where i.appointment_id=v_id and coalesce(i.is_paid,true)) then 'Pago' else 'Pendente' end,
      payment_method=case when v_id=any(p_appointment_ids) then coalesce(nullif(p_payment_method,''),'Nao informado') else 'Nao informado' end,
      updated_at=now() where appointment_id=v_id;
  end loop;
end; $$;

drop function if exists public.update_professional_payout(uuid, date, numeric, text);
create function public.update_professional_payout(
  p_payout_id uuid, p_payout_date date, p_amount numeric, p_payment_method text,
  p_appointment_ids uuid[]
) returns void language plpgsql security invoker set search_path = public as $$
declare v_professional_id uuid; v_id uuid; v_old_ids uuid[]; v_all_ids uuid[]; v_reference numeric(10,2) := 0;
begin
  if public.current_profile_role() not in ('admin', 'reception') then raise exception 'Perfil sem permissao.'; end if;
  if p_amount is null or round(p_amount,2) <= 0 then raise exception 'Valor invalido.'; end if;
  if coalesce(array_length(p_appointment_ids,1),0) = 0 then raise exception 'Selecione atendimentos.'; end if;
  select professional_id into v_professional_id from public.professional_payouts where id=p_payout_id for update;
  if not found then raise exception 'Repasse nao encontrado.'; end if;
  select array_agg(appointment_id) into v_old_ids from public.professional_payout_items where professional_payout_id=p_payout_id;
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.appointments where id=v_id and professional_id=v_professional_id) then
      raise exception 'Atendimento invalido para o profissional.';
    end if;
    v_reference := v_reference + coalesce((select round(consultation_fee*0.70,2)
      from public.appointment_financials where appointment_id=v_id),0);
  end loop;
  update public.professional_payouts set payout_date=p_payout_date, amount=round(p_amount,2),
    reference_amount=v_reference, payment_method=coalesce(nullif(p_payment_method,''),'Nao informado'), updated_at=now()
  where id=p_payout_id;
  delete from public.professional_payout_items where professional_payout_id=p_payout_id and not (appointment_id=any(p_appointment_ids));
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.professional_payout_items where professional_payout_id=p_payout_id and appointment_id=v_id) then
      insert into public.professional_payout_items(professional_payout_id,appointment_id,is_repassed) values(p_payout_id,v_id,true);
    end if;
  end loop;
  select array_agg(distinct x) into v_all_ids from unnest(coalesce(v_old_ids,array[]::uuid[]) || p_appointment_ids) x;
  foreach v_id in array coalesce(v_all_ids,array[]::uuid[]) loop
    update public.appointment_financials af set professional_payout_status=
      case when exists(select 1 from public.professional_payout_items i where i.appointment_id=v_id and coalesce(i.is_repassed,true)) then 'Repassado' else 'Pendente' end,
      updated_at=now() where appointment_id=v_id;
  end loop;
end; $$;
