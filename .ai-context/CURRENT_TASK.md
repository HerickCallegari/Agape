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
- Resumo financeiro limitado a recebido real, valor efetivamente repassado e
  quantidade de atendimentos resultante dos filtros ativos.
- Listas do resumo exibem somente paciente/funcionário e quantidade de agendas,
  respeitam todos os filtros ativos, mantêm cadastrados sem agendas com contagem zero
  e são ordenadas alfabeticamente pelo nome.
- Filtro financeiro de situação inicia em `Todos`.
- Navegação financeira mantém somente as abas `Resumo` e `Histórico`; as telas,
  tabelas e atualizações redundantes de recebimentos, repasses e atendimentos foram removidas.
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
- Edição do histórico reutiliza a tela completa da movimentação, preenchendo filtros,
  itens, data, valor e forma; a RPC atualiza cabeçalho e vínculos atomicamente.
- Cada recebimento pertence obrigatoriamente a um paciente e um profissional e guarda
  percentual de repasse (0 a 100), valor calculado sobre o recebido e flag de repassado.
  O histórico de recebimentos exibe ambos os envolvidos.
- A antiga tela de histórico de repasses foi removida; a aba `Histórico` passou a se
  chamar `Financeiro` e mostra diretamente os recebimentos, sem subabas.
- Rodapé do recebimento mantém todos os campos e ações em uma única faixa horizontal,
  usando larguras compactas para valores, percentual, forma e data.
- Valor recebido, forma de pagamento e valor do repasse usam larguras reduzidas;
  percentual é um campo numérico simples de 0 a 100 e situação é um dropdown.
- Recebimento e repasse formam uma única operação financeira. Os KPIs consideram
  somente `amount` e `payout_amount` gravados nessa operação; o repasse entra no KPI
  apenas quando a operação estiver marcada como repassada.
- Resumo exibe recebido, repassado e parte da clínica; a parte da clínica é calculada
  como valor recebido menos valor efetivamente repassado.
- Listas do resumo mantêm larguras estáveis após atualizar e exibem valor recebido
  por paciente e valor efetivamente repassado por funcionário.
- Edição de atendimentos em lote permite excluir definitivamente as linhas marcadas,
  com confirmação antes da operação.
- Mudança rápida de status na agenda atualiza apenas o card e os indicadores, preserva
  scroll e ordem, bloqueia cliques durante o salvamento e usa desempates estáveis na consulta.
- Scroll do mouse nunca interage com o status, mesmo com o dropdown aberto; a roda
  permanece exclusiva para navegar pela agenda.
- Aba Financeiro não exibe texto explicativo nem faixa vazia; ações de editar e
  excluir ficam dentro do cartão de operações.
- Aba Financeiro oferece `Novo repasse`, abrindo a operação unificada com os filtros
  atuais de período, paciente e profissional.
- Janela da operação financeira abre em 1300 × 760 px.
- Popups abertos pelas listas do Resumo ignoram situação financeira/status e exibem
  todos os atendimentos do período, paciente e profissional selecionados.
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
