# Clínica Agape Desktop

Aplicativo desktop para gestão de agenda terapêutica, pacientes, registros de sessão, permissões por perfil e controle financeiro de uma clínica.

O projeto foi desenvolvido como uma aplicação real de apoio operacional para a Clínica Agape, com foco em uma rotina simples para recepção, administração e profissionais de atendimento.

## Status Atual

O sistema está em fase de MVP funcional, com interface desktop em evolução e integração com Supabase para autenticação, banco de dados e funções administrativas.

Principais módulos disponíveis:

- Login com Supabase Auth e validação de perfil ativo.
- Tela inicial com indicadores e atendimentos do dia.
- Agenda com calendário, criação de atendimentos, agenda fixa, mudança rápida de status e detalhes da sessão.
- Pacientes com cadastro, edição, dados pessoais, endereço, motivo do atendimento e histórico.
- Exportação de sessões por paciente para apoio na criação de pareceres e relatórios.
- Financeiro com abas de resumo, recebimentos, repasses e atendimentos.
- Configurações para gerenciamento de funcionários, permissões, status ativo, especialidade e vínculo com agenda.
- Build Windows com PyInstaller.

## Tecnologias

- Python 3
- PySide6
- Supabase Auth
- Supabase Postgres
- Supabase Edge Functions
- python-dotenv
- PyInstaller

## Regras de Permissão

O sistema trabalha com três perfis principais:

| Perfil | Acesso |
| --- | --- |
| Administrador | Agenda, pacientes, financeiro, configurações e modo administrador |
| Recepção | Agenda geral, pacientes, financeiro e configurações operacionais |
| Profissional | Própria agenda, alteração de status e registro de sessão |

Observação importante: a interface esconde telas e ações conforme o perfil, mas a segurança definitiva deve ser mantida também pelas políticas de RLS no Supabase.

## Regras Financeiras Atuais

O financeiro considera o valor associado ao atendimento e separa os resultados entre clínica, pacientes e profissionais.

- Atendimentos cobrados: `Atendido` e `Falta sem aviso`.
- Atendimentos não cobrados: `Falta com aviso` e `Cancelado`.
- Atendimentos futuros: sessões ainda não realizadas.
- A clínica fica com 30% do valor do atendimento.
- O profissional recebe 70% do valor do atendimento realizado.
- Recebimentos de pacientes são registrados separadamente dos atendimentos.
- Repasses para profissionais são registrados em tabela própria.
- A tela financeira resume valores faturados, recebidos, em aberto, repasses pagos e valores a pagar.

## Estrutura do Projeto

```text
.
├── agape_app/
│   ├── app.py              # Interface principal e telas do sistema
│   ├── config.py           # Leitura das variáveis de ambiente
│   ├── permissions.py      # Perfis e permissões
│   ├── repository.py       # Comunicação com Supabase e regras de persistência
│   ├── theme.py            # Estilos visuais da aplicação
│   └── assets/             # Logos, ícones e imagens usadas no app
├── supabase/
│   └── functions/
│       └── reset-user-password/
│           └── index.ts    # Edge Function para redefinição de senha
├── supabase_schema.sql     # Schema base do banco
├── supabase_migration_*.sql# Migrações incrementais
├── build_windows.ps1       # Script de build para Windows
├── ClinicaAgape.spec       # Configuração do PyInstaller
├── main.py                 # Ponto de entrada da aplicação
├── requirements.txt        # Dependências Python
└── .env.example            # Exemplo de configuração local
```

## Configuração Local

Crie e ative um ambiente virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Crie um arquivo `.env` a partir do exemplo:

```powershell
Copy-Item .env.example .env
```

Preencha:

```env
SUPABASE_URL=https://seu-projeto.supabase.co
SUPABASE_ANON_KEY=sua-chave-publica-anon
```

Depois execute:

```powershell
python main.py
```

## Banco de Dados

O projeto usa Supabase Postgres. Para preparar uma base nova:

1. Execute o conteúdo de `supabase_schema.sql` no SQL Editor do Supabase.
2. Execute as migrações `supabase_migration_*.sql` conforme necessário.
3. Configure as políticas de RLS para respeitar os perfis do sistema.
4. Crie os usuários no Supabase Auth e mantenha o vínculo com `public.profiles`.

## Edge Function de Senha

A função `reset-user-password` permite que administradores e recepção redefinam senhas pelo aplicativo.

Ela fica em:

```text
supabase/functions/reset-user-password/index.ts
```

No Supabase, configure os secrets da função:

```text
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY
```

Nunca coloque `SUPABASE_SERVICE_ROLE_KEY` no `.env` do aplicativo desktop.

## Build para Windows

Para gerar o executável:

```powershell
.\build_windows.ps1
```

O executável será gerado em:

```text
dist\ClinicaAgape\ClinicaAgape.exe
```

A pasta `dist/` é artefato de build e não deve ser versionada.

## Atualizações automáticas

O executável consulta a versão mais recente publicada nos Releases do repositório
`HerickCallegari/Agape`. Quando existe uma versão superior, o aplicativo oferece o
download, valida o SHA-256 do instalador e inicia a atualização.

Para preparar uma versão:

1. Em **Settings > Secrets and variables > Actions**, configure uma única vez os
   secrets `SUPABASE_URL` e `SUPABASE_ANON_KEY`. Nunca configure a service role.
2. Altere `APP_VERSION` em `agape_app/version.py` usando o formato `X.Y.Z`.
3. Faça commit de todas as alterações.
4. Com a árvore de trabalho limpa, execute:

```powershell
.\publish.ps1 -Version 1.0.1
```

O script executa os testes e envia a tag. O workflow do GitHub Actions compila o
aplicativo no Windows, gera `ClinicaAgape-Setup.exe`, calcula seu checksum e cria o
GitHub Release automaticamente. Acompanhe a execução na aba **Actions** do GitHub.

O primeiro instalador deve ser instalado manualmente em cada computador. A partir
dele, as próximas versões são oferecidas pelo próprio aplicativo.

Não reutilize uma tag já publicada e nunca inclua `SUPABASE_SERVICE_ROLE_KEY` no
aplicativo ou nos arquivos da release.

## Segurança e Dados Sensíveis

- O arquivo `.env` não deve ser enviado ao Git.
- A chave `service_role` deve existir apenas no ambiente seguro do Supabase.
- Dados reais de pacientes, funcionários e finanças não devem ser commitados.
- Backups locais e exports gerados pelo usuário devem ficar fora do repositório.

## Roadmap Sugerido

- Melhorar testes automatizados das regras financeiras.
- Criar tela de auditoria para alterações sensíveis.
- Refinar políticas de RLS no Supabase.
- Criar instalador Windows assinado.
- Adicionar exportação de relatórios em PDF.
- Melhorar documentação visual com screenshots profissionais.

## Objetivo de Portfólio

Este projeto demonstra construção de um sistema desktop completo, com autenticação, controle de permissões, integração com backend gerenciado, regras de negócio reais, interface administrativa e fluxo financeiro aplicado ao contexto de clínica.

Ele pode ser apresentado como um case de produto interno para gestão clínica, destacando:

- Levantamento e evolução incremental de requisitos.
- Modelagem de dados no Supabase.
- Interface desktop com PySide6.
- Controle de acesso por perfil.
- Regras financeiras automatizadas.
- Build e distribuição para testes em Windows.
