# Clinica Agape Desktop

Primeira versao funcional do sistema desktop da Clinica Agape, feito em Python com PySide6 e Supabase.

## Configuracao

1. Crie um ambiente virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Instale as dependencias:

```powershell
pip install -r requirements.txt
```

3. Crie o banco no Supabase usando `supabase_schema.sql`.

4. Copie `.env.example` para `.env` e preencha:

```powershell
Copy-Item .env.example .env
```

5. Execute:

```powershell
python main.py
```

## Gerar executavel Windows

```powershell
.\build_windows.ps1
```

O executavel sera criado em `dist\ClinicaAgape\ClinicaAgape.exe`.

## Observacoes de seguranca

- Use somente a chave publica/anonima do Supabase no aplicativo.
- Nunca coloque `service_role`, chaves administrativas ou segredos no cliente desktop.
- As permissoes de seguranca ficam nas policies RLS do Supabase e nos perfis do aplicativo.

## Perfis

- `admin`
- `reception`
- `professional`

Depois de criar usuarios no Supabase Auth, vincule cada usuario a um registro em `profiles.auth_user_id`.

## Usuarios do sistema

Administradores podem criar usuarios em **Configuracoes > Novo usuario**. O aplicativo cria o login no Supabase Auth usando a chave publica anonima e, em seguida, cria o perfil em `profiles` com a permissao escolhida.

Se a confirmacao de e-mail estiver ativa no Supabase, o novo usuario precisa confirmar o e-mail antes de conseguir entrar.

## Atualizacao de paciente e custos

Para bancos que ja foram criados antes desta versao, rode no SQL Editor do Supabase:

```sql
-- arquivo supabase_migration_patient_financials.sql
```

Essa migracao adiciona o campo de motivo do atendimento no paciente e cria a tabela `appointment_financials`, visivel apenas para `admin` e `reception`.

Para usar os novos campos de dados pessoais e endereco do paciente, rode tambem:

```sql
-- arquivo supabase_migration_patient_profile_fields.sql
```
# Agape
