# Decisões estruturais

## 2026-08-06 — Transações financeiras com valor efetivo separado da referência

### Decisão

Recebimentos e repasses armazenam separadamente:

- `amount`: valor efetivamente recebido ou repassado.
- `reference_amount`: soma original dos atendimentos selecionados.
- `discount_amount`: desconto informado.
- `surcharge_amount`: acréscimo informado.

Os itens vinculados preservam apenas a referência ao atendimento e uma flag booleana
(`is_paid` ou `is_repassed`), sem ratear o valor efetivo por atendimento. As RPCs
`register_patient_payment` e `register_professional_payout` validam novamente
proprietário e valores no banco e gravam cabeçalho, itens e situação financeira em
uma única transação.

### Motivo

O valor efetivo pode divergir da soma dos atendimentos. Usar um único valor para caixa
e cobertura dos itens tornava impossível representar acordos, descontos e acréscimos
sem perder rastreabilidade.

### Consequências

- As migrações `supabase_migration_20260806_financial_transaction_adjustments.sql` e
  `supabase_migration_20260806_financial_item_status_flags.sql` devem ser aplicadas,
  nessa ordem, antes de publicar a versão 1.0.3.
- As colunas antigas `amount` dos itens ficam opcionais e depreciadas para preservar
  compatibilidade com dados legados; novos registros não as preenchem.
- Atendimentos de qualquer status continuam selecionáveis; cancelamento é informativo.
- Registros anteriores permanecem imutáveis e novas operações criam histórico adicional.
- Relatórios futuros devem distinguir valor efetivo de valor de referência.

## 2026-08-06 — Período inicial mensal sem limitar a operação

Os diálogos abrem no primeiro e último dia do mês corrente, mas a consulta aceita
qualquer intervalo e uma única operação pode vincular atendimentos de meses distintos.

## 2026-08-06 — Forma de pagamento com seleção única

A interface usa um campo dropdown e permite uma única forma por operação. Pagamento
misto exige valores separados por forma; sem esse rateio, armazenar várias opções
seria ambíguo e prejudicaria os relatórios de caixa.

## 2026-08-06 — Formulário financeiro operacional simplificado

Desconto, acréscimo e observação não são solicitados na interface. A camada de UI envia
zero, zero e texto vazio para manter compatibilidade com a RPC e o histórico existentes.
O painel financeiro ativa o período automaticamente e usa o mês corrente completo.
O período é obrigatório no painel: não existe mais opção de consultar todas as datas
sem limite. Para ampliar a consulta, o usuário altera explicitamente início e fim.

Recebido e repassado são valores de caixa: vêm do `amount` digitado no cabeçalho da
operação. As flags dos itens indicam apenas se o atendimento foi incluído e não devem
ser usadas para reconstruir ou ratear o valor efetivamente recebido/repassado.

A tabela de seleção financeira não usa a seleção nativa do `QTableWidget`. O checkbox
é a fonte única de estado; cada alteração repinta somente sua linha e ajusta o total
incrementalmente, evitando redesenhar milhares de células.

Alteração e exclusão de recebimentos/repasses usam RPCs. A edição preserva os itens
vinculados e altera data, valor efetivo e forma. A exclusão apaga o cabeçalho e itens
em cascata e recalcula `payment_status`/`professional_payout_status` considerando
eventuais outras movimentações que ainda referenciem o atendimento.

## 2026-09-23 — Atendimento com recebimento não pode ser excluído

A exclusão individual ou em lote consulta primeiro os itens de recebimento vinculados.
Se algum atendimento possuir recebimento registrado, toda a exclusão é interrompida e
o usuário é orientado a excluir ou estornar o recebimento no Financeiro. A restrição de
chave estrangeira permanece como proteção adicional, com tradução para mensagem amigável.
