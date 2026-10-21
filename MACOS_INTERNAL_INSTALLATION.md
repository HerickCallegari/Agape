# Instalação interna no macOS

Esta distribuição é destinada aos computadores autorizados da Clínica Ágape. Ela
não é publicada na Mac App Store e não possui assinatura Developer ID nem
notarização da Apple.

## Escolher o pacote

- Mac com processador Apple M1, M2, M3, M4 ou posterior:
  `ClinicaAgape-macOS-arm64.dmg` (macOS 14 ou posterior).
- Mac com processador Intel: `ClinicaAgape-macOS-x64.dmg` (macOS 15 ou posterior).

Em **Menu Apple > Sobre Este Mac**, o campo Chip mostra Apple quando o computador é
Apple Silicon; modelos Intel exibem Processador Intel.

## Verificar o download

Baixe também o arquivo `.sha256` correspondente. No Terminal, entre na pasta dos
downloads e execute, trocando o nome conforme a arquitetura:

```bash
cd ~/Downloads
shasum -a 256 -c ClinicaAgape-macOS-arm64.dmg.sha256
```

Continue somente se o resultado terminar em `OK`.

## Instalar pela primeira vez

1. Abra o arquivo DMG.
2. Arraste **ClinicaAgape** para o atalho **Aplicativos**.
3. Abra a pasta Aplicativos e tente iniciar Clínica Agape.
4. Se o macOS bloquear o aplicativo, abra **Ajustes do Sistema > Privacidade e
   Segurança**.
5. Localize o aviso referente a Clínica Agape, clique em **Abrir Mesmo Assim** e
   confirme conscientemente a abertura.

Não desative globalmente o Gatekeeper e não execute comandos encontrados em páginas
de terceiros para remover a segurança do computador.

## Atualizar

Quando uma nova versão for oferecida, o sistema baixa o DMG correto, valida o
SHA-256 e o abre. Arraste novamente Clínica Agape para Aplicativos, confirme a
substituição e reabra o programa. Uma nova versão pode exigir outra autorização em
Privacidade e Segurança.

## Limitações desta distribuição gratuita

- O macOS não consegue associar o aplicativo a uma identidade Apple verificada.
- Políticas corporativas de gerenciamento do Mac podem impedir **Abrir Mesmo Assim**.
- A instalação e a substituição do aplicativo são manuais.
- Para remover essas limitações, será necessário assinar com Developer ID e
  notarizar os pacotes em uma conta Apple Developer paga.
