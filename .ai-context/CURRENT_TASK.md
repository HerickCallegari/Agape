# Tarefa atual

Refazer os diálogos de recebimento de pacientes e repasse a profissionais.

## Escopo

- Filtros compactos no topo, com período padrão igual ao mês corrente completo.
- Busca por intervalo livre, inclusive múltiplos meses.
- Tabela central com scroll, todos os status e valor original do atendimento.
- Seleção livre inclusive para cancelados, com indicação visual informativa.
- Rodapé com forma de pagamento, total de referência, descontos, acréscimos e valor efetivo editável.
- Persistência atômica e histórica via RPC para pagamento/repasse e itens vinculados.
- Filtros combináveis de paciente e profissional e seleção automática dos resultados.
- Itens guardam somente vínculo e flag de pago/repassado; o valor agregado fica no cabeçalho.
- Painel financeiro e diálogos abrem no mês corrente; paciente e profissional permanecem filtros opcionais combináveis.
- Formulário simplificado sem desconto, acréscimo, observação ou filtro superior por forma registrada.
- Resumo financeiro com faturamento das consultas, recebido real, parte da clínica,
  total destinado aos funcionários e valor efetivamente repassado.
- Clique nas listas do resumo abre recebimento ou repasse herdando período, situação
  e filtros combinados, com o paciente/profissional da linha já selecionado.
- Histórico é a segunda aba e permite editar data, valor e forma ou excluir a
  movimentação via RPC atômica, recalculando as flags dos atendimentos vinculados.
- Rodapé do repasse separa total bruto, parte calculada do funcionário (70%) e valor
  efetivamente repassado, mantendo os dois primeiros somente para referência.
- Listas do resumo exigem duplo clique para abrir movimentações e distribuem nome e
  valores sem barra horizontal, com linhas mais altas para leitura.
- Detalhes da agenda priorizam o relatório; edição administrativa de data, horários,
  valor, status e exclusão fica em popup exclusivo para administração e recepção.
  Profissionais mantêm status junto do salvamento do relatório, sem botão de edição.
- Alterações em `agape_app/app.py`, `agape_app/repository.py`, migração SQL e testes.

## Arquivos relacionados

- `agape_app/app.py`: `PatientReceiptDialog` e `ProfessionalPayoutDialog`.
- `agape_app/repository.py`: consultas de atendimentos e métodos de gravação financeira.
- `supabase_schema.sql` e migrações financeiras: tabelas e RPCs existentes.
- `tests/test_repository_values.py`: testes de regras do repositório.

## Restrições

- UI não monta queries.
- Mudanças no banco entram em migração versionada.
- A operação composta precisa ser tudo-ou-nada.
- Registros anteriores não podem ser sobrescritos ou apagados.
- Cancelamento é informação visual, não bloqueio de seleção.

## Prompt de implementação

Implemente os dois fluxos financeiros com filtros compactos, intervalo mensal padrão mas livre, tabela rolável com todos os atendimentos e seleção irrestrita, cálculo visual de total e ajustes, valor efetivo livre e RPCs transacionais que preservem histórico. Cubra regras e persistência com testes automatizados.

## Estado

Implementado na versão 1.0.3. As duas migrações SQL de 2026-08-06 precisam ser
aplicadas no Supabase, na ordem, antes da publicação do executável.
