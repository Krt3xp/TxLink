# TaxLink NFS-e Collector

Servico Python independente para consultar NFS-e e eventos no Ambiente de Dados
Nacional (ADN), manter um banco SQLite autocontido e publicar alteracoes em uma
outbox que sera consumida pelo sistema PHP de contratos.

## Arquitetura Ubuntu em desenvolvimento

A versao 0.2 adiciona a base da operacao no Ubuntu Linux:

- API FastAPI autenticada por Bearer token;
- fila persistente de coletas com `execution_id` UUID;
- scheduler diario com APScheduler;
- certificado A1 PFX ou PEM registrado no SQLite;
- validade e thumbprint extraidos do certificado de arquivo;
- snapshot consistente pela API nativa de backup do SQLite;
- envio SFTP com Paramiko e renomeacao atomica no Windows OpenSSH;
- unidade de servico systemd em `packaging/taxlink-collector.service`.

O catalogo de contratos ainda nao faz parte do coletor. `contract_id` e
`contract_number` permanecem nulos ate a definicao da integracao com o PHP.

Para iniciar a API, scheduler e worker:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export TAXLINK_API_TOKEN='substitua-por-um-token-forte'
taxlink-nfse --config config.toml init-db
taxlink-nfse --config config.toml serve
```

Rotas principais:

```text
POST /api/v1/coleta/executar
GET  /api/v1/coleta/status/{execution_id}
POST /api/v1/sincronizacao/executar
GET  /api/v1/sincronizacao/status/{sync_id}
GET  /api/v1/health
```

O SFTP permanece desabilitado no exemplo ate que host, usuario, `known_hosts`
e credencial do Windows Server sejam preenchidos. A aplicacao exige suporte a
`posix-rename` no servidor SFTP e falha de forma segura se a troca atomica nao
estiver disponivel.

A view `vw_certificados_digitais` apresenta o caminho do certificado no
servidor Ubuntu, thumbprint, inicio/fim da validade e situacao. Para PFX e PEM,
`store_location` permanece nulo; esse campo existe apenas para o provedor
Windows legado.

O projeto de referencia em `C:\Users\edrma\TaxLink\python-taxlink` foi usado
somente para confirmar o fluxo de certificado Windows e os enderecos da API.
Nenhum arquivo da referencia e modificado ou importado por esta aplicacao.

## Escopo deste incremento

- multiplas unidades e um cursor NSU independente por unidade;
- certificado A1 instalado no repositorio do Windows, selecionado por thumbprint;
- alternativa PFX cuja senha vem de variavel de ambiente;
- consulta `GET /DFe/{NSU}` com `cnpjConsulta` e modo de lote configuravel;
- XML original comprimido dentro do SQLite;
- dados de cabecalho e servico normalizados;
- gravacao atomica de documento, NFS-e, outbox e proximo NSU;
- idempotencia e deteccao de um mesmo NSU com conteudo divergente;
- execucao unica, continua ou diagnostica;
- testes sem acesso ao ambiente fiscal real.

O coletor nao filtra por fornecedor ou contrato. Todos os DF-e entregues para a
unidade sao preservados; a conciliacao contratual pertence ao sistema PHP.

## Requisitos

- Python 3.11 ou superior;
- Windows Server para uso do certificado instalado;
- certificado A1 com chave privada acessivel pela conta que executara o servico;
- acesso HTTPS a `adn.nfse.gov.br`;
- para PFX, `requests` e `cryptography`.

## Configuracao

Copie `config.example.toml` para `config.toml` e preencha:

- `tax_id` da unidade;
- `system_unit_id` correspondente no sistema PHP;
- ambiente `restricted` durante a homologacao;
- thumbprint do certificado;
- local do certificado (`Auto`, `LocalMachine` ou `CurrentUser`).

A senha de um PFX nunca deve aparecer no TOML. Configure a variavel de ambiente
indicada em `password_env`.

## Execucao em desenvolvimento

```powershell
python -m pip install -e .
taxlink-nfse --config config.toml doctor
taxlink-nfse --config config.toml init-db
taxlink-nfse --config config.toml once --force
taxlink-nfse --config config.toml status
taxlink-nfse --config config.toml run
```

O comando `run` e de longa duracao e pode ser hospedado pelo Agendador de
Tarefas, WinSW ou outro gerenciador de servicos. O empacotamento definitivo e a
instalacao como servico Windows serao adicionados depois da prova com o
certificado real.

## Geracao do executavel

Com as dependencias instaladas:

```powershell
.\scripts\build.ps1
```

O build produz dois arquivos:

- `dist\taxlink-nfse.exe`, com console, para `doctor`, `status` e operacao manual;
- `dist\taxlink-nfse-service.exe`, sem janela, para execucao continua em segundo plano.

A aplicacao continua exigindo um `config.toml` externo para que unidades,
caminhos e certificados possam ser alterados sem gerar outro executavel.

## Execucao automatica no Windows

O A1 instalado em `CurrentUser\My` somente e acessivel pela mesma conta do
Windows. Instale a tarefa enquanto estiver conectado nessa conta:

```powershell
.\scripts\install-task.ps1 -StartNow
```

A tarefa inicia no logon, reinicia o coletor em caso de falha e impede duas
instancias simultaneas. Para substituir uma tarefa existente, acrescente
`-Force`. Para remover:

```powershell
.\scripts\uninstall-task.ps1
```

Em um servidor sem logon interativo, prefira instalar o A1 em
`LocalMachine\My` com permissao de chave privada para uma conta de servico e
ajustar a tarefa para essa conta.

## Monitor grafico

O monitor Windows acompanha o coletor e o SQLite sem fazer gravacoes diretas no
banco. Ele mostra certificado e validade, cursor NSU, ultima consulta, totais de
NFS-e/XML/PDF/contratos, notas coletadas, historico de ciclos e o log em tempo
real.

As datas na tela filtram as NFS-e que ja estao no SQLite. Como o ADN distribui
documentos por NSU, o botao `Buscar periodo` executa um backfill idempotente
desde o NSU 1 ate o cursor corrente e, em seguida, o intervalo selecionado passa
a exibir as notas historicas encontradas.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\monitor.ps1
```

Os botoes `Iniciar` e `Interromper` controlam apenas o processo aberto pela
propria janela. Se o coletor tiver sido iniciado por uma tarefa agendada, o
monitor o identifica como executando, mas nao encerra esse processo externo.

## Banco de dados

As tabelas centrais sao:

- `fiscal_unit`;
- `distribution_cursor`;
- `collection_run`;
- `dfe_artifact`;
- `invoice` e `invoice_item`;
- `fiscal_event`;
- `integration_outbox`.

O PHP devera ler a outbox em ordem crescente, guardar seu ultimo ID consumido
no MySQL e fazer operacoes idempotentes pela chave de acesso.

A view `vw_invoice_outbox` entrega a interface inicial de integracao, incluindo
`system_unit_id`, emitente, tomador, competencia, valores, NSU e hash do XML. O
PHP deve abrir o SQLite em modo somente leitura e consultar
`WHERE outbox_id > :ultimo_id ORDER BY outbox_id`.

A view `vw_notas_fiscais` apresenta a consulta operacional consolidada com:

- contrato e ID do contrato, quando conciliados pelo sistema PHP;
- ID interno da NFS-e;
- CNPJ da unidade e do fornecedor;
- emissao, valor e competencia;
- XML nacional sem compactacao e DANFSe em PDF, ambos como BLOB;
- chave de acesso, NSU e origem/estado do DANFSe: `BAIXADO_OFICIAL` quando
  retornado pelo ADN ou `GERADO_DO_XML` quando o PDF oficial estiver
  indisponivel.

Exemplo sem imprimir os arquivos binarios no terminal:

```powershell
sqlite3 -readonly -header -column data\taxlink-nfse.sqlite3 "SELECT [ID], [Contrato], [Contrato ID], [Unidade CNPJ], [Fornecedor CNPJ], [Data de Emissao], printf('%.2f', [Valor]) AS [Valor], [Competencia], length([XML]) AS [XML bytes], length([DANFe PDF]) AS [PDF bytes], [Status DANFe PDF], [NSU] FROM vw_notas_fiscais ORDER BY [NSU];"
```

O ADN nao possui o contrato administrativo. Por isso `Contrato` e `Contrato ID`
permanecem nulos ate o sistema PHP fazer uma correspondencia unica e segura por
unidade, CNPJ do fornecedor e vigencia contratual.

## Documentacao fiscal

- [Manual dos Contribuintes - APIs do ADN](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/manual-contribuintes-apis-adn-sistema-nacional-nfse.pdf)
- [Documentacao atual de producao](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual/documentacao-atual)
- Swagger de producao restrita: `https://adn.producaorestrita.nfse.gov.br/contribuintes/docs/index.html`
