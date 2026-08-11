alter table public.patient_payments
  add column if not exists professional_id uuid references public.professionals(id),
  add column if not exists payout_percentage numeric(5,2) not null default 70,
  add column if not exists payout_amount numeric(10,2) not null default 0,
  add column if not exists is_repassed boolean not null default false;

alter table public.patient_payments
  drop constraint if exists patient_payments_payout_percentage_check;
alter table public.patient_payments
  add constraint patient_payments_payout_percentage_check
  check (payout_percentage between 0 and 100);

create or replace function public.register_patient_payment(
  p_patient_id uuid, p_professional_id uuid, p_payment_date date, p_amount numeric,
  p_reference_amount numeric, p_discount_amount numeric, p_surcharge_amount numeric,
  p_payment_method text, p_notes text, p_appointment_ids uuid[],
  p_payout_percentage numeric, p_is_repassed boolean
) returns uuid language plpgsql security invoker set search_path = public as $$
declare v_payment_id uuid; v_appointment_id uuid; v_fee numeric(10,2); v_reference numeric(10,2) := 0;
begin
  if public.current_profile_role() not in ('admin', 'reception') then raise exception 'Perfil sem permissao.'; end if;
  if p_patient_id is null or p_professional_id is null then raise exception 'Paciente e profissional sao obrigatorios.'; end if;
  if p_amount is null or round(p_amount,2) <= 0 then raise exception 'Valor recebido invalido.'; end if;
  if coalesce(p_payout_percentage,-1) < 0 or p_payout_percentage > 100 then raise exception 'Percentual de repasse invalido.'; end if;
  if coalesce(array_length(p_appointment_ids,1),0)=0 then raise exception 'Selecione atendimentos.'; end if;
  foreach v_appointment_id in array p_appointment_ids loop
    select round(coalesce(af.consultation_fee,0),2) into v_fee
    from public.appointments a left join public.appointment_financials af on af.appointment_id=a.id
    where a.id=v_appointment_id and a.patient_id=p_patient_id and a.professional_id=p_professional_id
    for update of a;
    if not found then raise exception 'Atendimento invalido para paciente/profissional.'; end if;
    v_reference := round(v_reference + v_fee,2);
  end loop;
  if round(coalesce(p_reference_amount,0),2) <> v_reference then raise exception 'Valor de referencia mudou.'; end if;
  insert into public.patient_payments
    (patient_id,professional_id,payment_date,amount,reference_amount,discount_amount,surcharge_amount,
     payment_method,notes,payout_percentage,payout_amount,is_repassed,created_by)
  values
    (p_patient_id,p_professional_id,p_payment_date,round(p_amount,2),v_reference,
     round(coalesce(p_discount_amount,0),2),round(coalesce(p_surcharge_amount,0),2),
     coalesce(nullif(p_payment_method,''),'Nao informado'),p_notes,round(p_payout_percentage,2),
     round(p_amount*p_payout_percentage/100,2),coalesce(p_is_repassed,false),public.current_profile_id())
  returning id into v_payment_id;
  foreach v_appointment_id in array p_appointment_ids loop
    insert into public.patient_payment_items(patient_payment_id,appointment_id,is_paid)
    values(v_payment_id,v_appointment_id,true);
    insert into public.appointment_financials(appointment_id,payment_status,payment_method)
    values(v_appointment_id,'Pago',coalesce(nullif(p_payment_method,''),'Nao informado'))
    on conflict(appointment_id) do update set payment_status='Pago',payment_method=excluded.payment_method,updated_at=now();
  end loop;
  return v_payment_id;
end; $$;

create or replace function public.update_patient_payment(
  p_payment_id uuid, p_payment_date date, p_amount numeric, p_payment_method text,
  p_appointment_ids uuid[], p_professional_id uuid, p_payout_percentage numeric,
  p_is_repassed boolean
) returns void language plpgsql security invoker set search_path = public as $$
declare v_patient_id uuid; v_id uuid; v_old_ids uuid[]; v_all_ids uuid[]; v_reference numeric(10,2):=0;
begin
  if public.current_profile_role() not in ('admin','reception') then raise exception 'Perfil sem permissao.'; end if;
  if p_amount is null or round(p_amount,2)<=0 then raise exception 'Valor invalido.'; end if;
  if p_professional_id is null then raise exception 'Profissional obrigatorio.'; end if;
  if coalesce(p_payout_percentage,-1)<0 or p_payout_percentage>100 then raise exception 'Percentual invalido.'; end if;
  if coalesce(array_length(p_appointment_ids,1),0)=0 then raise exception 'Selecione atendimentos.'; end if;
  select patient_id into v_patient_id from public.patient_payments where id=p_payment_id for update;
  if not found then raise exception 'Recebimento nao encontrado.'; end if;
  select array_agg(appointment_id) into v_old_ids from public.patient_payment_items where patient_payment_id=p_payment_id;
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.appointments where id=v_id and patient_id=v_patient_id and professional_id=p_professional_id)
      then raise exception 'Atendimento invalido para paciente/profissional.'; end if;
    v_reference:=v_reference+coalesce((select round(consultation_fee,2) from public.appointment_financials where appointment_id=v_id),0);
  end loop;
  update public.patient_payments set payment_date=p_payment_date,amount=round(p_amount,2),reference_amount=v_reference,
    payment_method=coalesce(nullif(p_payment_method,''),'Nao informado'),professional_id=p_professional_id,
    payout_percentage=round(p_payout_percentage,2),payout_amount=round(p_amount*p_payout_percentage/100,2),
    is_repassed=coalesce(p_is_repassed,false),updated_at=now() where id=p_payment_id;
  delete from public.patient_payment_items where patient_payment_id=p_payment_id and not (appointment_id=any(p_appointment_ids));
  foreach v_id in array p_appointment_ids loop
    if not exists(select 1 from public.patient_payment_items where patient_payment_id=p_payment_id and appointment_id=v_id)
      then insert into public.patient_payment_items(patient_payment_id,appointment_id,is_paid) values(p_payment_id,v_id,true); end if;
  end loop;
  select array_agg(distinct x) into v_all_ids from unnest(coalesce(v_old_ids,array[]::uuid[]) || p_appointment_ids) x;
  foreach v_id in array coalesce(v_all_ids,array[]::uuid[]) loop
    update public.appointment_financials set
      payment_status=case when exists(select 1 from public.patient_payment_items i where i.appointment_id=v_id and coalesce(i.is_paid,true)) then 'Pago' else 'Pendente' end,
      payment_method=case when v_id=any(p_appointment_ids) then coalesce(nullif(p_payment_method,''),'Nao informado') else 'Nao informado' end,
      updated_at=now() where appointment_id=v_id;
  end loop;
end; $$;
